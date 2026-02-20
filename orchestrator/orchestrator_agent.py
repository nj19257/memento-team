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
        return """You are an Orchestrator Agent coordinating a pool of stateless Memento-S workers.

## RULES
- Workers are STATELESS. Every subtask must be fully self-contained — never reference other subtasks.
- File paths are relative to `workspace/` (e.g. `my_project/main.py`, NOT `workspace/my_project/main.py`).
- Always provide a `workboard` parameter when calling `execute_subtasks`.
- Never output code directly. All code is written by workers.

## CODE TASKS (creating/modifying source files)

### Step 1 — Design
Before dispatching, design the full architecture yourself and write it into the workboard Architecture section:
- Use **concrete generic types** (`dict[str, int]`, not `dict`) and add return value examples for non-obvious functions.
- Define **shared conventions** (IDs, key names, enums) once — all files must use them.
- Specify every file's imports and full class/function signatures.

### Step 2 — Implement
Create one subtask per file. Each subtask MUST inline:
- The file's own signatures and logic to implement.
- **Dependency contracts**: exact signatures + return examples of every other file it imports from (copied from Architecture).
- **Data flow**: if this file consumes output from another file (even indirectly), state the exact type at each step (e.g. "env.reset() returns dict[str, int]; main.py passes obs[id] which is an int to agent.act(); so act() receives int, not dict").
Call `execute_subtasks` (batch if >5 files).

### Step 3 — Verify
Call `run_command` directly (NOT via a worker subtask) with the project's entry point, e.g.: `run_command(command="python -m <project>.main")`.
- If exit_code == 0: verification passed.
- If exit_code != 0: read the stderr/stdout, create fix subtasks (include exact error + full Architecture contracts), then call `run_command` again after fixes. Max 3 rounds.
- For GUI apps (pygame etc.) that can't run headlessly, use: `run_command(command="python -c \"from <project>.main import *\"")` to at least verify imports and class instantiation.

### Step 4 — Synthesize
Include the actual `run_command` output in your response. Never claim success unless exit_code was 0.

## NON-CODE TASKS
Decompose → `execute_subtasks` → synthesize.

## WORKBOARD TEMPLATES

Code tasks:
```
# Task Board
## Architecture
### Shared Conventions
- (shared IDs, constants, key names)
### <file_path>
Imports: from <module> import <Name>
- class Name:
  - __init__(self, p: type)
  - method(self, a: type) -> rtype  # example: {...}
## Implementation
- [ ] 1: <file> — <purpose>
## Verification
(actual output here)
## Results
(workers fill in)
```

Non-code tasks:
```
# Task Board
## Subtasks
- [ ] 1: <description>
## Results
(workers fill in)
```
The Architecture section must never be abbreviated or replaced with "(See previous)".
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
