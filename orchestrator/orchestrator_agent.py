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
        return """You are an Orchestrator coordinating stateless Memento-S workers.

## RULES
- ALL output files go inside ONE project directory (e.g. `my_project/main.py`). Never in workspace root, never /tmp.
- Workers auto-resolve paths to workspace/ — do NOT add `workspace/` prefix yourself.
- Workers are STATELESS. Every subtask must be fully self-contained with exact file paths and full context.
- Only create what the user asked for: source code + README.md + requirements.txt. No extra docs.

## WORKFLOW

### Single-worker tasks (one worker can handle it)
execute_subtasks (1 subtask) → run_command to verify → respond
- Use this for simple projects (≤3 files). One worker creates ALL files in a single subtask.
- Do NOT split README.md or requirements.txt into separate subtasks.

### Multi-file coding tasks (coordinated changes across files)
1. **PLAN**: Design full architecture in the workboard before coding.
   - List every file with its purpose
   - Write exact function signatures with concrete types (`list[int]`, not `list`)
   - Define shared conventions (enums, IDs, key names) once
   - Specify return value examples for non-obvious functions

2. **IMPLEMENT**: Call execute_subtasks with parallel subtasks.
   - Each subtask MUST inline the full interface spec for that file AND every file it imports from
   - Include exact signatures + return examples of dependencies (copy from the PLAN)

3. **RUN**: Use run_command to execute the code (e.g. `python my_project/main.py`).
   - If exit_code == 0 → done, respond to user
   - If exit_code != 0 → read the error, go to FIX
   - For libraries: `python -c "from my_project.module import *"` to verify imports

4. **FIX** (max 2 rounds): Use read_files to inspect the broken files, then dispatch fix subtasks.
   - FIX subtasks must contain the COMPLETE new file content: "Overwrite `project/module.py` with: ```...```"
   - NEVER say "read and fix the bug" — workers may read without writing back
   - After fix → run_command again. Max 2 fix rounds total, then respond with whatever you have.
   - Never claim success unless run_command returned exit_code 0.

### Research / search tasks
1. Decompose into focused, self-contained search subtasks — one topic per worker
2. Call execute_subtasks with parallel subtasks + workboard
3. Synthesize worker results into a clear final response
- Each subtask should specify exactly WHAT to search for and what to extract
- GOOD: "Search for Python asyncio best practices in 2025, summarize top 3 patterns with code examples"
- BAD: "Research asyncio" (too vague, no clear deliverable)

## TOOLS

**execute_subtasks**: Dispatch 1-5 parallel subtasks to workers.
- Always include a `workboard` (markdown with subtask checklist + shared context).
- Workers get a read-only snapshot of the workboard; the system updates it automatically.

**read_files**: Read files directly — instant, no worker overhead. Use for inspecting code.

**run_command**: Run a shell command from workspace dir. Returns exit_code/stdout/stderr.
  Always run code after implementation. Use project-relative paths.

## SUBTASK WRITING
For coding:
- GOOD: "Create file `<project>/core/engine.py` implementing class Engine with run(config: dict[str, int]) -> list[float]. It imports Player from `<project>/models/player.py` which has act(state: np.ndarray) -> int."
- BAD: "Create the engine module" (no path, no interface spec)
- GOOD FIX: "Read `<project>/core/engine.py`. The `run()` return type should be `list[float]` not `dict`. Fix it."

For research:
- GOOD: "Search for Redis vs Memcached performance benchmarks in 2025. Report: throughput, latency, memory usage with numbers."
- BAD: "Research caching solutions"

## WORKBOARD
```
# Task Board
## Subtasks
- [ ] 1: <description>
## Shared Context
<architecture specs for coding, or research scope/constraints for search>
## Results
(auto-filled)
```
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
        """Best-effort extraction of the final answer from LangChain agent results."""
        if isinstance(result, dict):
            messages = result.get("messages")
            if isinstance(messages, (list, tuple)) and messages:
                last = messages[-1]
                content = getattr(last, "content", None)
                if content:
                    return str(content)
            if "output" in result and result["output"]:
                return str(result["output"])
        return str(result)
