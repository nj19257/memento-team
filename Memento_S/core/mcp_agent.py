"""MCP Agent - LangChain agent powered by FastMCP tools.

Uses ``create_agent()`` from ``langchain.agents`` to build a tool-calling
agent graph that dispatches to the in-process FastMCP server via
LangChain ``StructuredTool`` instances.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, AsyncGenerator, Callable

from fastmcp import FastMCP
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
        "You are Memento-S, a worker in a multi-agent team. Other workers "
        "are running in parallel on related subtasks.\n"
        "\n"
        "## Shared Workboard\n"
        "A shared workboard coordinates the team. It is a **live document** "
        "that changes as workers update it. You should:\n"
        "- **Start** by calling `read_board` to see the task plan, your "
        "assigned subtask, and any shared context\n"
        "- **During** your work, call `read_board` again to check for new "
        "updates from other workers. Call `edit_board` or `append_board` to "
        "post your own findings, progress, or information that other workers "
        "might need\n"
        "- **When done**, call `edit_board` to mark your checklist item as "
        "complete (e.g. replace `- [ ] 1: ...` with `- [x] 1: ...`)\n"
        "\n"
        "## Tools\n"
        "**Workboard:** `read_board`, `edit_board(old_text, new_text, reason)`, "
        "`append_board(text, reason)`\n"
        "**Files:** `bash_tool`, `file_create`, `str_replace`, `view`\n"
        "**Skills:** `list_local_skills`, `search_cloud_skills`, `read_skill` — "
        "use when the task requires external data or capabilities you don't have. "
        "Find a skill, read it, then run its scripts via `bash_tool`.\n"
        "\n"
        "Be concise but thorough."
    )

    def __init__(
        self,
        *,
        model: BaseChatModel,
        system_prompt: str | None = None,
        base_dir: Path | None = None,
        recursion_limit: int = 150,
        extra_servers: list[tuple[FastMCP, Callable | None]] | None = None,
    ) -> None:
        """
        Args:
            model: LangChain chat model (ChatOpenAI, ChatAnthropic, etc.)
            system_prompt: Custom system prompt for the agent
            base_dir: Working directory for MCP tools
            recursion_limit: Max agent loop iterations
            extra_servers: Additional FastMCP servers to connect to
                (e.g. workboard server). Each entry is
                ``(FastMCP_instance, configure_fn | None)``.
        """
        self.model = model
        self._system_prompt = system_prompt or self.DEFAULT_SYSTEM_PROMPT
        self._base_dir = base_dir
        self._recursion_limit = recursion_limit
        self._tool_manager = MCPToolManager(extra_servers=extra_servers)
        self._agent_graph: Any = None

    async def start(self) -> None:
        """Start the MCP server and build the agent graph.

        Must be called before ``run()`` or ``stream()``.
        """
        await self._tool_manager.start(base_dir=self._base_dir)
        tools = self._tool_manager.get_langchain_tools()
        logger.info(f"MCPAgent: loaded tools: {[t.name for t in tools]}")

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

        result = await self._agent_graph.ainvoke(
            {"messages": messages},
            config={"recursion_limit": self._recursion_limit},
        )
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
        await self._tool_manager.shutdown()
        self._agent_graph = None

    @property
    def tool_manager(self) -> MCPToolManager:
        """Access the underlying MCPToolManager."""
        return self._tool_manager

    @property
    def tool_names(self) -> list[str]:
        """Return the names of all loaded MCP tools."""
        return [t.name for t in self._tool_manager.get_langchain_tools()]
