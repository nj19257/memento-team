"""MCP Agent - LangChain agent powered by FastMCP tools.

Uses ``create_agent()`` from ``langchain.agents`` to build a tool-calling
agent graph that dispatches to the in-process FastMCP server via
LangChain ``StructuredTool`` instances.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator, Callable

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage

from core.mcp_client import MCPToolManager

logger = logging.getLogger(__name__)


def _to_lc_messages(
    messages: list[dict],
) -> list[HumanMessage | AIMessage]:
    """Convert plain ``{"role": ..., "content": ...}`` dicts to LangChain messages."""
    out: list[HumanMessage | AIMessage] = []
    for m in messages:
        content = str(m.get("content", ""))
        if m.get("role") == "user":
            out.append(HumanMessage(content=content))
        else:
            out.append(AIMessage(content=content))
    return out


class MCPAgent:
    """
    LangChain agent backed by the FastMCP tool server.

    Wraps ``MCPToolManager`` tools into a LangChain agent graph via
    ``create_agent()``.  Supports both one-shot ``run()`` and
    streaming ``stream()`` execution.

    Usage::

        from langchain_openai import ChatOpenAI

        agent = MCPAgent(model=ChatOpenAI(model="gpt-4o"))
        await agent.start()
        result = await agent.run("Read the file pyproject.toml")
        await agent.close()
    """

    DEFAULT_SYSTEM_PROMPT = (
        "You are Memento-S, an intelligent assistant with a skill system.\n"
        "\n"
        "## Core tools\n"
        "You have tools for: bash commands, editing files, creating files, "
        "viewing files/directories, and shared workboard coordination "
        "(`read_workboard`, `edit_workboard`).\n"
        "\n"
        "## Skill discovery & execution\n"
        "You also have skill-discovery tools: `list_local_skills`, "
        "`search_cloud_skills`, and `read_skill`.\n"
        "Skills are self-contained packages that extend your capabilities "
        "(e.g. web search, data analysis, API integrations).\n"
        "\n"
        "**When you cannot answer from your own knowledge or the task "
        "requires external data, follow this workflow:**\n"
        "1. Use `list_local_skills` or `search_cloud_skills` to find a "
        "relevant skill.\n"
        "2. Use `read_skill` to learn how the skill works.\n"
        "3. Execute the skill's scripts/commands via `bash_tool`.\n"
        "\n"
        "## Efficiency rules\n"
        "- **Search then Fetch**: When searching the web, search ONCE with "
        "a focused query, then use `fetch.sh` to get full content from the "
        "best URL. Do NOT repeat searches with slightly different queries.\n"
        "- **Use snippets**: If search result snippets already contain the "
        "information you need, use them directly instead of fetching.\n"
        "- **Max 3 search calls**: Limit web searches to 3 per task. "
        "Prefer fetching a URL over searching again.\n"
        "- **Workboard updates**: Only write to your status tag ONCE when "
        "done (with result). Do NOT write intermediate statuses like "
        "'Searching...' or 'In progress...'.\n"
        "- **Write result FIRST, then status**: Always fill your result "
        "tag before marking status as complete.\n"
        "- **Do NOT read workboard after writing your result**: Once you "
        "have written your result and status, end immediately.\n"
        "\n"
        "## Workboard coordination\n"
        "When a workboard is provided in the task context, use "
        "`read_workboard` to inspect it and `edit_workboard(tag, content)` "
        "to fill only your assigned tagged sections.\n"
        "\n"
        "## Saving new skills\n"
        "When asked to save a pipeline or workflow as a reusable skill, "
        "always create it under the `skill_extra/` directory (relative to "
        "the project root). Use the `skill-creator` skill for guidance. "
        "Example: `skill_extra/my-new-skill/SKILL.md`.\n"
        "\n"
        "Use the tools to accomplish the user's request. "
        "Be concise but thorough."
    )

    def __init__(
        self,
        *,
        model: BaseChatModel,
        system_prompt: str | None = None,
        base_dir: Path | None = None,
        recursion_limit: int = 150,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """
        Args:
            model: LangChain chat model (ChatOpenAI, ChatAnthropic, etc.)
            system_prompt: Custom system prompt for the agent
            base_dir: Working directory for MCP tools
            recursion_limit: Max agent loop iterations
        """
        self.model = model
        self._system_prompt = system_prompt or self.DEFAULT_SYSTEM_PROMPT
        self._base_dir = base_dir
        self._recursion_limit = recursion_limit
        self._event_sink = event_sink
        self._tool_manager = MCPToolManager(event_sink=event_sink)
        self._agent_graph: Any = None

    def _emit_event(self, event: str, **fields: Any) -> None:
        if self._event_sink is None:
            return
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": str(event),
        }
        payload.update(fields)
        try:
            self._event_sink(payload)
        except Exception:
            pass

    async def start(self) -> None:
        """Start the MCP server and build the agent graph.

        Must be called before ``run()`` or ``stream()``.
        """
        self._emit_event("agent_start", base_dir=str(self._base_dir or ""), recursion_limit=self._recursion_limit)
        await self._tool_manager.start(base_dir=self._base_dir)
        tools = self._tool_manager.get_langchain_tools()
        logger.info(f"MCPAgent: loaded tools: {[t.name for t in tools]}")
        self._emit_event("agent_tools_loaded", tool_names=[t.name for t in tools], tool_count=len(tools))

        self._agent_graph = create_agent(
            model=self.model,
            tools=tools,
            system_prompt=self._system_prompt,
        )

    async def run(self, query: str | list[dict]) -> dict[str, Any]:
        """Execute the agent and return the complete result.

        Args:
            query: A string prompt or a list of message dicts
                   (``{"role": "user"|"assistant", "content": ...}``).

        Returns:
            Dict with ``messages`` key containing the full conversation.
        """
        if not self._agent_graph:
            raise RuntimeError("Agent not started. Call start() first.")

        if isinstance(query, str):
            messages: list[HumanMessage | AIMessage] = [HumanMessage(content=query)]
        else:
            messages = _to_lc_messages(query)

        self._emit_event(
            "agent_run_start",
            input_type="str" if isinstance(query, str) else "messages",
            message_count=len(messages),
        )
        result = await self._agent_graph.ainvoke(
            {"messages": messages},
            config={"recursion_limit": self._recursion_limit},
        )
        self._emit_event("agent_run_end", result_type=type(result).__name__)
        return result

    async def stream(
        self,
        query: str | list[dict],
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Execute the agent and stream intermediate updates.

        Args:
            query: A string prompt or a list of message dicts.

        Yields:
            Stream of update dicts containing intermediate steps.
        """
        if not self._agent_graph:
            raise RuntimeError("Agent not started. Call start() first.")

        if isinstance(query, str):
            messages: list[HumanMessage | AIMessage] = [HumanMessage(content=query)]
        else:
            messages = _to_lc_messages(query)

        async for chunk in self._agent_graph.astream(
            {"messages": messages},
            stream_mode="updates",
        ):
            yield chunk

    async def close(self) -> None:
        """Shutdown the MCP server and release resources."""
        self._emit_event("agent_close_start")
        await self._tool_manager.shutdown()
        self._agent_graph = None
        self._emit_event("agent_close_end")

    @property
    def tool_manager(self) -> MCPToolManager:
        """Access the underlying MCPToolManager."""
        return self._tool_manager

    @property
    def tool_names(self) -> list[str]:
        """Return the names of all loaded MCP tools."""
        return [t.name for t in self._tool_manager.get_langchain_tools()]
