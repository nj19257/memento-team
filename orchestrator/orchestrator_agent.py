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
    ) -> None:
        self.name = name
        self.model = model
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
        max_workers = self._env.get("MAX_WORKERS", "10")
        return f"""You are an Orchestrator Agent coordinating a pool of up to {max_workers} Memento-S workers.

## YOUR JOB

1. Receive a task from the user.
2. **Before decomposing, ALWAYS call `list_orchestrator_skills()` first, then call `read_orchestrator_skill("task-router")` to identify the task type.**
3. Based on the router's recommendation, call `read_orchestrator_skill("decompose-<type>")` to load strategy guidance.
4. Call `read_orchestrator_skill("search-strategy")` to determine the right search approach for workers.
5. Decompose the task using your own judgment. Include the search strategy hint in each subtask description.
6. Call `execute_subtasks` with the list of subtask strings.
7. Synthesize the worker results into a final response.

**Important: Steps 2-4 are mandatory.**

## DECOMPOSITION RULES
- Workers are **STATELESS** — each subtask must be fully self-contained with all context, constraints, and format specs. Never reference other subtasks.
- **Split aggressively**: you have up to {max_workers} workers. Target 10-20 data items per worker. Use as many workers as needed — do NOT under-split. When a task has multiple dimensions, split across ALL dimensions to maximize parallelism, not just one.
- Each subtask MUST enumerate specific sub-categories to cover. Do NOT write "search for all X" — list each sub-category explicitly, plus "...and any other variants not listed above".
- Copy ALL original query constraints into EVERY subtask: inclusion/exclusion filters, column definitions, value formats, terminology.
- Include a "Format Example" row in each subtask showing exact columns and value conventions.

## WORKER CAPABILITIES
Workers are Memento-S agents with web search, file ops, shell commands, and 8000+ cloud skills.
They auto-select the best skill via semantic routing. Focus on clear decomposition — workers handle execution.

## WORKBOARD (WORKER COORDINATION) — REQUIRED
When calling execute_subtasks, you MUST always include a `workboard` parameter.
The workboard is a markdown string shared with workers. Workers can read and edit it via
`read_workboard` and `edit_workboard(tag, content)` by filling tagged sections assigned to them.

Always create a workboard that:
1. Lists every subtask with its index and a status checkbox (e.g. `- [ ] 1 (t1): ...`)
2. Assigns a subtask ID to each worker (`t1`, `t2`, ...)
3. Includes empty tagged slots for each worker (e.g. `<t1_result></t1_result>`)
4. Provides any shared context workers might need
5. Includes a "Results" section (optional summary area for the manager)

Example workboard format:
```
# Task Board
## Subtasks
- [ ] 1 (t1): Search for X and summarize findings
- [ ] 2 (t2): Search for Y and summarize findings
## Shared Context
<any relevant context the workers should know>
## Worker Slots
### t1
<t1_status></t1_status>
<t1_result></t1_result>
### t2
<t2_status></t2_status>
<t2_result></t2_result>
## Results
(manager may summarize here)
```

## OUTPUT
- Verify completeness: check row count, column coverage, and data consistency before producing the final response.
- If gaps are found, dispatch targeted follow-up subtasks to fill them before finalizing.
- **CRITICAL: When synthesizing table data, CONCATENATE all worker rows directly. Do NOT summarize, deduplicate, or omit any rows. Every row from every worker must appear in the final table. If the table is large, output ALL rows — never truncate with "..." or "and X more rows".**
- Synthesize into a clear final response.
"""

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
            result = await self._agent_graph.ainvoke(
                {"messages": messages},
                config={"recursion_limit": 50},
            )
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
