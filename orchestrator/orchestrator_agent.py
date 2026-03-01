"""OrchestratorAgent — multi-phase orchestrator with workboard approval flow.

Replaces the old LangChain ``create_agent`` + MCP subprocess approach.
Workers run as asyncio tasks in the same process.  The orchestrator LLM
is "held" during worker execution: each workboard edit request becomes a
conversation turn that the LLM reviews inline.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ===================================================================
# Structured review model
# ===================================================================

class ReviewDecision(BaseModel):
    status: Literal["success", "failure"] = Field(
        description="'success' to approve, 'failure' to reject"
    )
    feedback: str = Field(
        default="", description="Reason for rejection, or empty if approved"
    )


REVIEW_SYSTEM_PROMPT = (
    "You are reviewing a workboard edit request from a worker.\n"
    "Respond with ONLY a JSON decision. Approve if the edit is reasonable "
    "and consistent with the task plan.\n"
    "Reject if the edit would break the workboard structure or contradicts "
    "the task requirements."
)


# Workspace default — resolved relative to project root at import time
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_WORKSPACE = _PROJECT_ROOT / "Memento_S" / "workspace"
_LOGS_DIR = _PROJECT_ROOT / "logs"


def _save_trajectory(
    prefix: str,
    events: list[dict],
    *,
    header_extra: dict[str, Any] | None = None,
) -> Path | None:
    """Write a trajectory to ``logs/<prefix>-<timestamp>.jsonl``."""
    try:
        _LOGS_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        filename = f"{prefix}-{ts}.jsonl"
        path = _LOGS_DIR / filename
        header: dict[str, Any] = {
            "type": "header",
            "total_events": len(events),
        }
        if header_extra:
            header.update(header_extra)
        with path.open("w", encoding="utf-8") as f:
            f.write(json.dumps(header, ensure_ascii=False) + "\n")
            for ev in events:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        return path
    except Exception as exc:
        logger.warning(f"Failed to save trajectory {prefix}: {exc}")
        return None


def _save_worker_trajectory(
    idx: int,
    subtask: str,
    events: list[dict],
    result_preview: str = "",
    time_taken: float = 0.0,
) -> Path | None:
    """Write a worker trajectory to a JSONL file with a header record."""
    return _save_trajectory(
        f"worker-{idx}",
        events,
        header_extra={
            "worker_index": idx,
            "subtask": subtask,
            "time_taken_seconds": round(time_taken, 2),
            "result_preview": (result_preview or "")[:300],
        },
    )


# ===================================================================
# WorkerJob
# ===================================================================

@dataclass
class WorkerJob:
    subtasks: list[str]
    tasks: list[asyncio.Task] = field(default_factory=list)
    results: dict[int, Any] = field(default_factory=dict)
    errors: dict[int, str] = field(default_factory=dict)

    def all_done(self) -> bool:
        return all(t.done() for t in self.tasks)

    def set_result(self, idx: int, result: Any) -> None:
        self.results[idx] = result

    def set_error(self, idx: int, error: str) -> None:
        self.errors[idx] = error


# ===================================================================
# Tool input schemas
# ===================================================================

class ReadFilesInput(BaseModel):
    paths: list[str] = Field(description="List of file paths to read")
    max_lines: int = Field(default=500, description="Max lines per file (0 = unlimited)")


class RunCommandInput(BaseModel):
    command: str = Field(description="Shell command to execute")
    working_dir: str = Field(default="", description="Working directory (default: workspace)")
    timeout: int = Field(default=30, description="Max seconds to wait")


# ===================================================================
# OrchestratorAgent
# ===================================================================

class OrchestratorAgent:
    """Multi-phase orchestrator with inline workboard approval.

    Phases:
        1. **PLAN** — LLM decomposes user task into subtasks (JSON block)
        2. **EXECUTE + REVIEW** — workers run; orchestrator reviews edit requests
        3. **AGGREGATE** — LLM synthesises final response (can verify & fix)
    """

    def __init__(
        self,
        *,
        model: BaseChatModel,
        system_message: str | None = None,
        workspace_dir: Path | None = None,
    ) -> None:
        self.model = model
        self._system_message = system_message or self._build_default_system_message()
        self._workspace_dir = workspace_dir or _DEFAULT_WORKSPACE
        self._tools = self._build_tools()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self, query: str) -> dict[str, Any]:
        """Execute the full plan → execute → aggregate loop."""
        import sys
        _memento_s_dir = str(_PROJECT_ROOT / "Memento_S")
        if _memento_s_dir not in sys.path:
            sys.path.insert(0, _memento_s_dir)
        from core.utils.logging_utils import (
            log_event as _log,
            start_trajectory_async,
            collect_trajectory_async,
        )

        start_trajectory_async("orchestrator")
        _log("orchestrator_start", query=query[:500])
        t0 = time.monotonic()
        turn = 0

        messages: list = [
            SystemMessage(content=self._system_message),
            HumanMessage(content=query),
        ]

        try:
            while True:
                turn += 1
                _log("llm_turn_start", turn=turn)
                response = await self.model.bind_tools(self._tools).ainvoke(messages)
                messages.append(response)

                # A) Tool calls (read_files, run_command) → execute and loop
                if response.tool_calls:
                    for tc in response.tool_calls:
                        _log("orch_tool_call", turn=turn, tool_name=tc["name"], arguments=tc.get("args", {}))
                        result = await self._execute_tool(tc)
                        _log("orch_tool_result", turn=turn, tool_name=tc["name"], result=result[:500])
                        messages.append(
                            ToolMessage(content=result, tool_call_id=tc["id"])
                        )
                    continue

                # B) Subtask plan detected → start workers + review loop
                text = response.content if isinstance(response.content, str) else str(response.content)
                plan = self._extract_plan(text)
                if plan:
                    subtasks = plan["subtasks"]
                    _log("plan_extracted", turn=turn, num_subtasks=len(subtasks),
                         subtasks=[s[:200] for s in subtasks])
                    logger.info(
                        f"[Orchestrator] Plan: {len(subtasks)} subtasks"
                    )
                    job = await self._start_workers(
                        subtasks, plan.get("workboard", "")
                    )
                    _log("workers_dispatched", turn=turn, num_workers=len(subtasks))
                    _log("review_loop_start", turn=turn)
                    await self._review_loop(job, messages)
                    _log("review_loop_end", turn=turn)
                    self._update_workboard(job)

                    # Summarise worker outcomes
                    _log("workers_completed", turn=turn,
                         results={str(k): str(v)[:200] for k, v in job.results.items()},
                         errors=dict(job.errors))

                    messages.append(
                        HumanMessage(content=self._format_results(job))
                    )
                    continue

                # C) No tools, no plan → final answer
                output = text
                _log("orchestrator_end", turn=turn, elapsed_seconds=round(time.monotonic() - t0, 2),
                     output_preview=output[:300])
                logger.info(f"[Orchestrator] Final: {output[:300]}...")
                return {"output": output, "messages": messages}
        finally:
            elapsed = time.monotonic() - t0
            events = collect_trajectory_async()
            _save_trajectory(
                "orchestrator",
                events,
                header_extra={
                    "query": query[:300],
                    "time_taken_seconds": round(elapsed, 2),
                },
            )

    # ------------------------------------------------------------------
    # Phase 2: Workers + Review
    # ------------------------------------------------------------------

    async def _start_workers(
        self, subtasks: list[str], workboard: str
    ) -> WorkerJob:
        """Launch MCPAgent workers as asyncio tasks."""
        import sys
        _memento_s_dir = str(_PROJECT_ROOT / "Memento_S")
        if _memento_s_dir not in sys.path:
            sys.path.insert(0, _memento_s_dir)

        from core.workboard_mcp import (
            mcp as wb_mcp,
            configure as wb_configure,
            write_board_sync,
        )

        if workboard.strip():
            write_board_sync(workboard)

        job = WorkerJob(subtasks=subtasks)

        for idx, subtask in enumerate(subtasks):
            task = asyncio.create_task(
                self._run_single_worker(idx, subtask, job)
            )
            job.tasks.append(task)

        return job

    async def _run_single_worker(
        self, idx: int, subtask: str, job: WorkerJob
    ) -> None:
        """Run one MCPAgent worker for a single subtask."""
        import sys
        _memento_s_dir = str(_PROJECT_ROOT / "Memento_S")
        if _memento_s_dir not in sys.path:
            sys.path.insert(0, _memento_s_dir)

        from core.workboard_mcp import (
            mcp as wb_mcp,
            configure as wb_configure,
            set_worker_context,
        )
        from core.mcp_agent import MCPAgent
        from core.model_factory import build_chat_model
        from core.utils.logging_utils import (
            log_event,
            start_trajectory_async,
            collect_trajectory_async,
        )

        start_trajectory_async(f"worker-{idx}")
        log_event("worker_start", worker_idx=idx, subtask=subtask)
        t0 = time.monotonic()

        set_worker_context(worker_idx=idx)

        worker_model = build_chat_model()
        agent = MCPAgent(
            model=worker_model,
            base_dir=self._workspace_dir,
            extra_servers=[(wb_mcp, wb_configure)],
        )
        await agent.start()
        result_preview = ""
        try:
            result = await agent.run(subtask)
            # Log LLM conversation turns from result messages
            if isinstance(result, dict):
                msgs = result.get("messages")
                if isinstance(msgs, (list, tuple)):
                    for msg in msgs:
                        if isinstance(msg, AIMessage):
                            if getattr(msg, "tool_calls", None):
                                for tc in msg.tool_calls:
                                    log_event(
                                        "llm_tool_call",
                                        worker_idx=idx,
                                        tool_name=tc.get("name", "?"),
                                        arguments=tc.get("args", {}),
                                    )
                            elif getattr(msg, "content", None):
                                log_event(
                                    "llm_response_text",
                                    worker_idx=idx,
                                    content=str(msg.content)[:500],
                                )
                        elif isinstance(msg, ToolMessage):
                            log_event(
                                "tool_message",
                                worker_idx=idx,
                                tool_call_id=getattr(msg, "tool_call_id", ""),
                                content=str(getattr(msg, "content", ""))[:500],
                            )

            # Extract text from result
            if isinstance(result, dict):
                msgs = result.get("messages")
                if isinstance(msgs, (list, tuple)) and msgs:
                    last = msgs[-1]
                    content = getattr(last, "content", None)
                    if content:
                        result_preview = str(content)
                        job.set_result(idx, result_preview)
                        return
                if "output" in result:
                    result_preview = str(result["output"])
                    job.set_result(idx, result_preview)
                    return
            result_preview = str(result)
            job.set_result(idx, result_preview)
        except Exception as e:
            logger.error(f"[Worker {idx}] Error: {e}\n{traceback.format_exc()}")
            job.set_error(idx, str(e))
            result_preview = f"ERROR: {e}"
        finally:
            elapsed = time.monotonic() - t0
            log_event("worker_end", worker_idx=idx, elapsed_seconds=round(elapsed, 2))
            events = collect_trajectory_async()
            _save_worker_trajectory(
                idx, subtask, events,
                result_preview=result_preview,
                time_taken=elapsed,
            )
            await agent.close()

    async def _review_loop(self, job: WorkerJob, messages: list) -> None:
        """Process edit requests from workers using structured LLM output."""
        import sys
        _memento_s_dir = str(_PROJECT_ROOT / "Memento_S")
        if _memento_s_dir not in sys.path:
            sys.path.insert(0, _memento_s_dir)

        from core.workboard_mcp import get_pending_requests, resolve_request
        from core.utils.logging_utils import log_event

        review_model = self.model.with_structured_output(ReviewDecision)

        while not job.all_done():
            pending = await get_pending_requests()

            for request in pending:
                review_prompt = self._format_edit_request(request)
                messages.append(HumanMessage(content=review_prompt))

                # Build review-specific message list with review system prompt
                review_messages = [SystemMessage(content=REVIEW_SYSTEM_PROMPT)] + messages[1:]
                decision = await review_model.ainvoke(review_messages)

                approved = decision.status == "success"
                log_event(
                    "workboard_review",
                    worker_idx=request.worker_idx,
                    edit_type=request.edit_type,
                    approved=approved,
                    feedback=decision.feedback,
                    reason=request.reason,
                )

                # Store the decision as an AIMessage in main conversation
                messages.append(AIMessage(content=json.dumps(
                    {"status": decision.status, "feedback": decision.feedback}
                )))

                await resolve_request(
                    request,
                    approved=approved,
                    feedback=decision.feedback,
                )

            if not pending and not job.all_done():
                await asyncio.sleep(0.5)

    def _update_workboard(self, job: WorkerJob) -> None:
        """Mechanically update the workboard after all workers complete.

        - Checks off ``- [ ]`` items for each finished worker
        - Appends a ``## Results`` section with worker result summaries
        """
        import sys
        _memento_s_dir = str(_PROJECT_ROOT / "Memento_S")
        if _memento_s_dir not in sys.path:
            sys.path.insert(0, _memento_s_dir)

        from core.workboard_mcp import read_board_sync, write_board_sync

        content = read_board_sync()
        if not content:
            return

        # Check off completed items
        for idx in range(len(job.subtasks)):
            if idx in job.results or idx in job.errors:
                match = re.search(r"- \[ \]", content)
                if match:
                    content = content[: match.start()] + "- [x]" + content[match.end() :]

        # Append results section
        results_match = re.search(r"^## Results", content, re.MULTILINE)
        if not results_match:
            content = content.rstrip() + "\n\n## Results\n"

        for idx, subtask in enumerate(job.subtasks):
            header = f"### Subtask {idx + 1}"
            if header in content:
                continue  # worker already posted results
            if idx in job.results:
                summary = str(job.results[idx])
                entry = f"\n{header}\n{summary.strip()}\n"
            elif idx in job.errors:
                entry = f"\n{header}\nERROR: {job.errors[idx]}\n"
            else:
                entry = f"\n{header}\nNo result\n"
            content = content.rstrip() + "\n" + entry

        write_board_sync(content)

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    async def _execute_tool(self, tool_call: dict) -> str:
        """Execute a tool call (read_files or run_command)."""
        name = tool_call["name"]
        args = tool_call.get("args", {})

        for tool in self._tools:
            if tool.name == name:
                try:
                    result = await tool.ainvoke(args)
                    if isinstance(result, dict):
                        return json.dumps(result, indent=2, default=str)
                    return str(result)
                except Exception as e:
                    return f"Tool error: {e}"

        return f"Unknown tool: {name}"

    # ------------------------------------------------------------------
    # Plan extraction
    # ------------------------------------------------------------------

    def _extract_plan(self, text: str) -> dict | None:
        """Extract a subtask plan from a JSON code block in LLM output.

        Looks for ````` ```json { "subtasks": [...] } ``` ````` blocks.
        """
        # Try fenced code blocks first
        pattern = r"```(?:json)?\s*(\{[\s\S]*?\})\s*```"
        for match in re.finditer(pattern, text):
            try:
                data = json.loads(match.group(1))
                if isinstance(data.get("subtasks"), list) and data["subtasks"]:
                    return data
            except (json.JSONDecodeError, KeyError):
                continue

        # Fallback: try to find bare JSON with "subtasks"
        pattern2 = r'\{[^{}]*"subtasks"\s*:\s*\[[\s\S]*?\]\s*(?:,\s*"workboard"\s*:\s*"[\s\S]*?")?\s*\}'
        for match in re.finditer(pattern2, text):
            try:
                data = json.loads(match.group(0))
                if isinstance(data.get("subtasks"), list) and data["subtasks"]:
                    return data
            except (json.JSONDecodeError, KeyError):
                continue

        return None

    # ------------------------------------------------------------------
    # Formatting helpers
    # ------------------------------------------------------------------

    def _format_edit_request(self, request) -> str:
        """Format an edit request for the orchestrator LLM to review."""
        if request.edit_type == "edit":
            return (
                f"Worker {request.worker_idx} requests a workboard edit:\n"
                f"- **Old text**: `{request.params['old_text']}`\n"
                f"- **New text**: `{request.params['new_text']}`\n"
                f"- **Reason**: {request.reason}\n\n"
                f"Current workboard:\n```\n{request.board_snapshot}\n```"
            )
        else:  # append
            return (
                f"Worker {request.worker_idx} requests to append to the workboard:\n"
                f"- **Text**: `{request.params['text']}`\n"
                f"- **Reason**: {request.reason}\n\n"
                f"Current workboard:\n```\n{request.board_snapshot}\n```"
            )

    def _format_results(self, job: WorkerJob) -> str:
        """Format worker results for the orchestrator LLM."""
        parts = ["All workers completed. Results:\n"]
        for idx, subtask in enumerate(job.subtasks):
            if idx in job.results:
                result = str(job.results[idx])
                parts.append(f"**Worker {idx}** ({subtask[:100]}):\n{result}\n")
            elif idx in job.errors:
                parts.append(
                    f"**Worker {idx}** ({subtask[:100]}): ERROR: {job.errors[idx]}\n"
                )
            else:
                parts.append(
                    f"**Worker {idx}** ({subtask[:100]}): No result\n"
                )
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Tools (read_files, run_command)
    # ------------------------------------------------------------------

    def _build_tools(self) -> list[StructuredTool]:
        """Build utility tools for the orchestrator (read_files, run_command)."""
        workspace = self._workspace_dir

        async def read_files(paths: list[str], max_lines: int = 500) -> dict:
            """Read one or more files and return their contents."""
            results = {}
            for path in paths:
                try:
                    p = Path(path)
                    if not p.exists():
                        results[path] = {"error": f"File not found: {path}"}
                        continue
                    if not p.is_file():
                        results[path] = {"error": f"Not a file: {path}"}
                        continue
                    text = p.read_text(encoding="utf-8")
                    if max_lines > 0:
                        lines = text.splitlines(keepends=True)
                        if len(lines) > max_lines:
                            text = "".join(lines[:max_lines])
                            text += f"\n... ({len(lines) - max_lines} more lines truncated)"
                    results[path] = {"content": text, "lines": len(text.splitlines())}
                except Exception as e:
                    results[path] = {"error": f"{type(e).__name__}: {e}"}
            return {"files": results}

        async def run_command(
            command: str, working_dir: str = "", timeout: int = 30
        ) -> dict:
            """Run a shell command and return the output."""
            if working_dir:
                cwd = Path(working_dir)
                if not cwd.is_absolute():
                    cwd = workspace / working_dir
            else:
                cwd = workspace

            if not cwd.exists():
                return {
                    "exit_code": -1,
                    "stdout": "",
                    "stderr": f"Directory not found: {cwd}",
                }

            logger.info(f"[run_command] {command}  (cwd={cwd})")
            try:
                proc = subprocess.run(
                    command,
                    shell=True,
                    cwd=str(cwd),
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                return {
                    "exit_code": proc.returncode,
                    "stdout": (proc.stdout or "").strip()[:5000],
                    "stderr": (proc.stderr or "").strip()[:5000],
                }
            except subprocess.TimeoutExpired:
                return {
                    "exit_code": -1,
                    "stdout": "",
                    "stderr": f"Command timed out after {timeout}s",
                }
            except Exception as exc:
                return {"exit_code": -1, "stdout": "", "stderr": str(exc)}

        read_files_tool = StructuredTool(
            name="read_files",
            description=(
                "Read one or more files and return their contents. "
                "Use for reviewing code written by workers or inspecting existing code."
            ),
            coroutine=read_files,
            func=None,
            args_schema=ReadFilesInput,
        )

        run_command_tool = StructuredTool(
            name="run_command",
            description=(
                "Run a shell command from workspace dir. Returns exit_code/stdout/stderr. "
                "Always run code after implementation to catch runtime errors."
            ),
            coroutine=run_command,
            func=None,
            args_schema=RunCommandInput,
        )

        return [read_files_tool, run_command_tool]

    # ------------------------------------------------------------------
    # System prompt
    # ------------------------------------------------------------------

    def _build_default_system_message(self) -> str:
        return """You are an Orchestrator that decomposes complex tasks and dispatches them to parallel Memento-S workers.

## CORE RULES
- Workers are STATELESS and run IN PARALLEL. They cannot see each other's work.
- Every subtask must be 100% self-contained: exact file paths, full code/content, all context needed.
- Workers auto-resolve paths to `workspace/` — never add `workspace/` prefix yourself.
- ALL output files go inside ONE project directory (e.g. `my_project/main.py`). Never workspace root, never /tmp.

## HOW TO DISPATCH

Think step-by-step before creating subtasks:
1. **Analyze** the user's request — what is the end goal?
2. **Identify boundaries** — what can be done independently in parallel?
3. **Define interfaces** — if parallel pieces must fit together, specify the exact contracts (function signatures, data formats, file paths, import names).
4. **Write subtasks** — each one self-contained with all context embedded.

Then output a JSON code block:

```json
{
  "subtasks": [
    "...",
    "..."
  ],
  "workboard": "..."
}
```

## DECOMPOSITION STRATEGIES

### Coding tasks — split by module/responsibility

Each subtask creates one logical unit. Include the FULL specification inline:
- Exact file path
- All function/class signatures with types
- How it connects to other files (import paths, shared data structures)
- Edge cases and error handling requirements

**Example — web app with 4 files:**
```json
{
  "subtasks": [
    "Create file `todo_app/models.py`:\n- class TodoItem with fields: id (int, auto-increment), title (str), done (bool, default False), created_at (datetime)\n- class TodoDB:\n  - __init__(self, path: str) — loads/creates JSON file at path\n  - add(self, title: str) -> TodoItem\n  - toggle(self, id: int) -> TodoItem\n  - list_all(self) -> list[TodoItem]\n  - delete(self, id: int) -> None\n  All methods persist to disk immediately.",

    "Create file `todo_app/app.py`:\n- Flask app with routes:\n  - GET / → render index.html with all todos from TodoDB('./todos.json')\n  - POST /add → form field 'title', redirects to /\n  - POST /toggle/<int:id> → toggles done, redirects to /\n  - POST /delete/<int:id> → deletes item, redirects to /\n- Import TodoDB from models.py\n- Run on port 5000",

    "Create file `todo_app/templates/index.html`:\n- Jinja2 template showing all todos in a list\n- Each item: checkbox (POST to /toggle/id), title (strikethrough if done), delete button (POST to /delete/id)\n- Form at top: text input + 'Add' button (POST to /add)\n- Minimal CSS inline: clean sans-serif font, centered max-width 600px container",

    "Create file `todo_app/requirements.txt` with:\nFlask==3.0.0"
  ],
  "workboard": "# Todo App\n## Architecture\n- models.py: TodoItem + TodoDB (JSON persistence)\n- app.py: Flask routes (imports TodoDB from models)\n- templates/index.html: Jinja2 UI\n- requirements.txt: Flask==3.0.0\n\n## Subtasks\n- [ ] 1: models.py (data layer)\n- [ ] 2: app.py (routes)\n- [ ] 3: templates/index.html (UI)\n- [ ] 4: requirements.txt\n\n## Shared Contracts\n- TodoDB interface: add(title) -> TodoItem, toggle(id) -> TodoItem, list_all() -> list[TodoItem], delete(id) -> None\n- TodoItem fields: id, title, done, created_at\n- DB path: ./todos.json"
}
```

Key principles:
- **Shared interfaces go in EVERY subtask that uses them** (workers can't read the workboard before starting)
- **One worker per file or per tightly-coupled file group** — never split a single file across workers
- **Simple projects (≤3 files)**: use ONE subtask — one worker creates everything

### Research tasks — split by information need

Each subtask is a focused research question. Avoid overlap by giving each worker a distinct scope.

**Example — market analysis:**
```json
{
  "subtasks": [
    "Research the current market size and growth rate of the global EV battery market (2023-2024 data). Include: total market value in USD, year-over-year growth %, top 3 market segments by size. Use web search. Return findings as a structured summary with sources.",

    "Research the top 5 EV battery manufacturers by market share (2024). For each: company name, headquarters country, market share %, key battery technology (NMC/LFP/solid-state), major automotive customers. Use web search. Return as a comparison table.",

    "Research recent technological breakthroughs in EV batteries (2023-2024). Focus on: solid-state batteries, silicon anodes, sodium-ion alternatives, fast-charging advances. For each breakthrough: what it is, which company/lab, expected commercialization timeline. Use web search."
  ],
  "workboard": "# EV Battery Market Analysis\n## Subtasks\n- [ ] 1: Market size & growth\n- [ ] 2: Top manufacturers comparison\n- [ ] 3: Technology breakthroughs\n\n## Synthesis Notes\n(orchestrator will combine results here)"
}
```

Key principles:
- **Each subtask covers a distinct angle** — no two workers research the same thing
- **Specify the output format** you want (table, bullet list, structured summary)
- **Include "use web search"** so the worker knows to use the web-search skill
- **Ask for sources** so results are verifiable

### Mixed tasks — combine strategies

For tasks that need both code and research (e.g. "build an app that shows real-time weather"), split research from implementation, then run implementation after research results are in.

## WORKBOARD DESIGN

The workboard is a shared markdown file for worker coordination. Design it with:
1. **Architecture overview** — how pieces fit together (for coding tasks)
2. **Subtask checklist** — `- [ ]` items workers can track
3. **Shared contracts** — interfaces, data formats, constants that multiple workers need
4. **Results section** — where workers post findings (for research tasks)

Workers can read the workboard and request edits (which you approve/reject).

## AFTER WORKERS COMPLETE

You will receive all worker results. Then:
1. **Coding tasks**: Use `read_files` to inspect output, `run_command` to test. If broken, dispatch a FIX round (max 2 rounds).
2. **Research tasks**: Synthesize worker results into a comprehensive final answer. Preserve ALL structured data (tables, lists, numbers) — never truncate or omit.
3. **Final answer**: Output plain text (no JSON block) to end.

## TOOLS

- **read_files**: Read files directly — instant, no worker overhead.
- **run_command**: Run a shell command. Returns exit_code/stdout/stderr. Always run code after implementation.

## COMMON MISTAKES TO AVOID
- Writing subtasks like "Create the backend" — too vague, no file paths or specs
- Splitting one file across multiple workers — they'll overwrite each other
- Assuming workers can see each other's output — they can't, embed all context
- Forgetting interface contracts — if file A imports file B, both subtasks need the shared API
- Dispatching 5+ research subtasks with overlapping scope — consolidate to avoid redundancy
"""
