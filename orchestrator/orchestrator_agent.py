"""OrchestratorAgent — LangChain orchestrator that decomposes tasks and dispatches to Memento-S workers."""

from __future__ import annotations

import logging
import os
import sys
import traceback
from typing import Any, AsyncGenerator, Mapping, Sequence

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_mcp_adapters.client import MultiServerMCPClient

logger = logging.getLogger(__name__)


class OrchestratorAgent:
    """
    LangChain orchestrator agent that decomposes tasks into subtasks
    and dispatches them to Memento-S workers via MCP.

    Architecture:
    - Uses LangChain BaseChatModel for LLM interactions
    - Connects to Memento-S MCP server for parallel task execution
    - Uses create_agent() to build the agent graph
    - Supports both streaming and non-streaming execution

    Usage:
        orchestrator = OrchestratorAgent(model=ChatOpenAI(model="gpt-4o"))
        await orchestrator.start()
        result = await orchestrator.run("Build a web scraper for news articles")
        await orchestrator.close()
    """

    DEFAULT_COMMAND = sys.executable
    DEFAULT_ARGS: Sequence[str] = ("orchestrator/mcp_server.py",)

    def __init__(
        self,
        *,
        name: str = "orchestrator",
        model: BaseChatModel,
        description: str | None = None,
        command: str | None = None,
        args: Sequence[str] | None = None,
        env: Mapping[str, str] | None = None,
        system_message: str | None = None,
        self_evolve: bool = True,
    ) -> None:
        self.name = name
        self.model = model
        self._self_evolve = self_evolve
        self._description = description or (
            "Decomposes complex tasks into subtasks and dispatches "
            "them to Memento-S worker agents for parallel execution."
        )
        self._command = command or self.DEFAULT_COMMAND
        self._args = list(args) if args is not None else list(self.DEFAULT_ARGS)
        self._env = dict(os.environ if env is None else env)
        self._system_message = system_message or self._build_default_system_message()
        self._mcp_client: MultiServerMCPClient | None = None
        self._agent_graph: Any = None

    def _build_default_system_message(self) -> str:
        base = """You are an Orchestrator Agent coordinating a pool of Memento-S workers.

## YOUR JOB
1. Receive a task from the user.
2. Call `list_local_skills()`, then `read_skill("task-router")` to identify the task type and load decomposition principles.
3. Based on the router's recommendation, call `read_skill("decompose-<type>")` to load the specialized strategy.
4. Call `read_skill("workboard-protocol")` to load the workboard format.
5. Decompose the task and call `execute_subtasks` with the subtask list and a workboard."""

        if self._self_evolve:
            base += """
6. **Self-Reflect:** Call `read_skill("self-evolve")` and review worker trajectories. If everything looks good, simply move on. Each result dict includes a `trajectory_file` path you can `view`."""

        base += """
{synth_step}. Synthesize the worker results into a clear final response.

**Steps 2-4 are mandatory.** Always read the relevant skills before decomposing — they contain lessons learned from past failures.

## OUTPUT
- **CRITICAL: When synthesizing table data, CONCATENATE all worker rows directly. Do NOT summarize, deduplicate, or omit any rows. Every row from every worker must appear in the final table. If the table is large, output ALL rows — never truncate with "..." or "and X more rows".**
- Synthesize into a clear final response.
""".format(synth_step=7 if self._self_evolve else 6)
        return base

    async def start(self) -> None:
        """Initialize MCP connection to worker pool and build the agent graph."""
        env = dict(self._env)

        mcp_servers = {
            "memento_worker_pool": {
                "command": self._command,
                "args": self._args,
                "env": env,
                "transport": "stdio",
            }
        }

        self._mcp_client = MultiServerMCPClient(mcp_servers)
        tools = await self._mcp_client.get_tools()

        self._agent_graph = create_agent(
            model=self.model,
            tools=tools,
            system_prompt=self._system_message,
        )

    async def run(self, query: str | list[dict]) -> dict[str, Any]:
        """Execute the orchestrator agent and return the complete result."""
        self._ensure_started()

        if isinstance(query, str):
            query_preview = query[:200] + "..." if len(query) > 200 else query
            logger.info(f"[Orchestrator] Query: {query_preview}")
            messages = [{"role": "user", "content": query}]
        else:
            messages = query

        try:
            result = await self._agent_graph.ainvoke({"messages": messages})
            output = self._extract_output(result)
            logger.info(f"[Orchestrator] Result: {output[:300]}...")
            return {"output": output, "raw": result}
        except Exception as e:
            logger.error(f"[Orchestrator] Error: {e}\n{traceback.format_exc()}")
            raise

    async def stream(
        self, query: str | list[dict]
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Execute the orchestrator agent and stream updates."""
        self._ensure_started()

        if isinstance(query, str):
            messages = [{"role": "user", "content": query}]
        else:
            messages = query

        async for chunk in self._agent_graph.astream(
            {"messages": messages},
            stream_mode="updates",
            config={"recursion_limit": 50},
        ):
            yield chunk

    async def close(self) -> None:
        """Close MCP connections and cleanup."""
        self._mcp_client = None
        self._agent_graph = None

    def _ensure_started(self) -> None:
        if self._agent_graph is None:
            raise RuntimeError("OrchestratorAgent not started. Call start() first.")

    @staticmethod
    def _extract_output(result: Any) -> str:
        """Best-effort extraction of the final AI answer from LangChain agent results."""
        if isinstance(result, dict):
            messages = result.get("messages")
            if isinstance(messages, (list, tuple)) and messages:
                # Walk backwards to find the last AIMessage with text content
                for msg in reversed(messages):
                    # Skip tool messages and human messages
                    msg_type = getattr(msg, "type", None)
                    if msg_type not in ("ai", None):
                        continue
                    content = getattr(msg, "content", None)
                    if not content:
                        continue
                    # content can be a string or a list of content blocks
                    if isinstance(content, str):
                        if content.strip():
                            return content
                    elif isinstance(content, list):
                        # Extract text from content blocks
                        parts = []
                        for block in content:
                            if isinstance(block, str):
                                parts.append(block)
                            elif isinstance(block, dict) and block.get("type") == "text":
                                parts.append(block.get("text", ""))
                        text = "\n".join(p for p in parts if p.strip())
                        if text.strip():
                            return text
            if "output" in result and result["output"]:
                return str(result["output"])
        return str(result)
