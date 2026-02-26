"""Orchestrator MCP Server — wraps Memento-S worker pool with workboard support."""

import json
import os
import re
import asyncio
import sys
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List


def _env_truthy(name: str) -> bool:
    return str(os.getenv(name, "")).strip().lower() in {"1", "true", "yes", "on"}


QUIET_STDERR = _env_truthy("MCP_QUIET_STDERR")
if QUIET_STDERR:
    # Suppress third-party startup banners/logs that can corrupt TUI rendering.
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

# Set up imports from Memento-S directory
_MEMENTO_S_DIR = str(Path(__file__).resolve().parent.parent / "Memento-S")
sys.path.insert(0, _MEMENTO_S_DIR)
os.chdir(_MEMENTO_S_DIR)

from fastmcp import FastMCP

from core.mcp_agent import MCPAgent
from core.model_factory import build_chat_model

import logging

logger = logging.getLogger(__name__)

MAX_POOL_SIZE = 5
WORKSPACE_DIR = (Path(_MEMENTO_S_DIR) / "workspace").resolve()
WORKBOARD_PATH = WORKSPACE_DIR / ".workboard.md"


def _stderr_print(*args: Any, **kwargs: Any) -> None:
    """Print to stderr unless MCP quiet mode is enabled."""
    if QUIET_STDERR:
        return
    kwargs.setdefault("file", sys.stderr)
    print(*args, **kwargs)

mcp = FastMCP("MementoSWorkerPool")

_semaphore = asyncio.Semaphore(MAX_POOL_SIZE)


def _workboard_write(content: str) -> Path:
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    WORKBOARD_PATH.write_text(content, encoding="utf-8")
    return WORKBOARD_PATH


def _workboard_read() -> str:
    if not WORKBOARD_PATH.exists():
        return "(no workboard exists)"
    return WORKBOARD_PATH.read_text(encoding="utf-8")


def _workboard_check_off_item(index_1_based: int) -> str:
    if not WORKBOARD_PATH.exists():
        return "check_off_item ERR: workboard does not exist"
    content = WORKBOARD_PATH.read_text(encoding="utf-8")
    pattern = re.compile(rf"^(\s*-\s)\[ \](\s+{index_1_based}\b)", re.MULTILINE)
    new_content, n = pattern.subn(r"\1[x]\2", content, count=1)
    if n == 0:
        return f"check_off_item SKIP: item {index_1_based} not found or already checked"
    WORKBOARD_PATH.write_text(new_content, encoding="utf-8")
    return f"check_off_item OK: item {index_1_based}"


def _workboard_append_result(index_1_based: int, text: str) -> str:
    if not WORKBOARD_PATH.exists():
        return "append_result ERR: workboard does not exist"
    content = WORKBOARD_PATH.read_text(encoding="utf-8")
    one_line = " ".join(str(text).split())[:200]
    result_line = f"- Task {index_1_based}: {one_line}"
    marker = "## Results"
    marker_idx = content.find(marker)
    if marker_idx == -1:
        content = content.rstrip() + f"\n\n{marker}\n{result_line}\n"
    else:
        marker_line_end = content.find("\n", marker_idx)
        if marker_line_end == -1:
            content += f"\n{result_line}\n"
        else:
            insert_pos = len(content)
            next_section = content.find("\n##", marker_line_end + 1)
            if next_section != -1:
                insert_pos = next_section + 1
            content = content[:insert_pos].rstrip() + f"\n{result_line}\n" + content[insert_pos:]
    WORKBOARD_PATH.write_text(content, encoding="utf-8")
    return f"append_result OK: task {index_1_based}"


def _extract_agent_output(result: dict[str, Any] | Any) -> str:
    """Best-effort extraction of final assistant text from MCPAgent.run()."""
    if isinstance(result, dict):
        messages = result.get("messages")
        if isinstance(messages, (list, tuple)) and messages:
            last = messages[-1]
            content = getattr(last, "content", None)
            if isinstance(content, str) and content.strip():
                return content
            if isinstance(content, list):
                # LangChain may return structured content blocks.
                parts = []
                for item in content:
                    if isinstance(item, str):
                        parts.append(item)
                    elif isinstance(item, dict) and item.get("text"):
                        parts.append(str(item.get("text")))
                    else:
                        parts.append(str(item))
                text = "\n".join(p for p in parts if p).strip()
                if text:
                    return text
        if isinstance(result.get("output"), str) and result.get("output").strip():
            return str(result["output"])
    return str(result)

EXECUTE_SUBTASKS_DESCRIPTION = f"""
Execute 1-{MAX_POOL_SIZE} independent subtasks in parallel using Memento-S agent workers.

CRITICAL: Maximum {MAX_POOL_SIZE} subtasks per call. Split larger batches into multiple calls.

CAPABILITIES:
- Each worker is a Memento-S agent powered by Agent Skills — capable of handling most tasks
- Workers automatically select the best skill for each subtask via semantic routing
- Workers can dynamically acquire new skills on demand for specialized tasks
- Each worker handles complex tasks iteratively through multi-round execution
- Workers are STATELESS and ISOLATED — cannot see other workers' results

SUBTASK DESIGN RULES:
1. SELF-CONTAINED: Each subtask must be fully independent with complete context
   - GOOD: "Read /path/to/config.py and extract the database URL"
   - BAD: "Read the config file mentioned earlier"
2. EXPLICIT: Always include full file paths, entity names, details
3. NATURAL LANGUAGE: Write clear directives
4. ATOMIC: One focused task per subtask

Args:
  subtasks: List[str]
    List of 1 to {MAX_POOL_SIZE} fully self-contained task descriptions.
  workboard: str (RECOMMENDED — always provide)
    Markdown content for a shared workboard file that lists the subtasks
    and provides a Results section. Workers receive it as read-only context.
    The orchestrator updates the workboard after each worker finishes.
"""


async def _execute_single_subtask(subtask: str) -> str:
    """Run a single subtask using the new Memento-S MCPAgent."""
    execution_text = subtask
    board_content = _workboard_read()
    if board_content and board_content != "(no workboard exists)":
        execution_text = (
            f"{subtask}\n\n"
            "## Active Workboard (read-only context)\n"
            "A shared workboard exists. Treat it as context only; do not attempt "
            "to edit it. The orchestrator will update the board after you finish.\n\n"
            f"```markdown\n{board_content}\n```"
        )

    agent = MCPAgent(model=build_chat_model(), base_dir=WORKSPACE_DIR)
    await agent.start()
    try:
        result = await agent.run(execution_text)
        return _extract_agent_output(result).strip()
    finally:
        await agent.close()


# ---------------------------------------------------------------------------
# Trajectory persistence & formatting
# ---------------------------------------------------------------------------
TRAJECTORY_LOG_DIR = Path(os.getenv(
    "TRAJECTORY_LOG_DIR",
    str(Path(__file__).resolve().parent.parent / "logs"),
))
_TRAJECTORY_FILE_LOCK = threading.Lock()


def _append_live_trajectory_event(path: Path, event: dict) -> None:
    """Append one JSON event to a live worker trajectory file."""
    try:
        line = json.dumps(event, ensure_ascii=False)
        with _TRAJECTORY_FILE_LOCK:
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        # Best-effort only; final save still writes full trajectory.
        pass


def _execute_single_subtask_with_trajectory(
    subtask: str,
    idx: int,
    live_path: Path | None = None,
) -> tuple[str, list[dict]]:
    """Legacy compatibility wrapper (trajectory hooks removed in new Memento-S)."""
    _ = (subtask, idx, live_path)
    return "", []


def _create_live_trajectory(idx: int, subtask: str) -> Path | None:
    """Create a per-worker trajectory file immediately with status=live."""
    try:
        TRAJECTORY_LOG_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        filename = f"worker-{idx}-{ts}.jsonl"
        path = TRAJECTORY_LOG_DIR / filename
        with path.open("w", encoding="utf-8") as f:
            header = {
                "type": "header",
                "worker_index": idx,
                "subtask": subtask,
                "status": "live",
                "result_preview": "",
                "time_taken_seconds": 0.0,
                "total_events": 0,
                "ts": ts,
            }
            f.write(json.dumps(header, ensure_ascii=False) + "\n")
        return path
    except Exception as exc:
        _stderr_print(f"[warn] failed to create live trajectory for worker {idx}: {exc}")
        return None


def _save_trajectory(
    idx: int,
    subtask: str,
    trajectory: list[dict],
    result: str,
    elapsed: float,
    *,
    status: str = "finished",
    path: Path | None = None,
) -> Path | None:
    """Write final trajectory state to JSONL file (finished/failed)."""
    try:
        TRAJECTORY_LOG_DIR.mkdir(parents=True, exist_ok=True)
        if path is None:
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            filename = f"worker-{idx}-{ts}.jsonl"
            path = TRAJECTORY_LOG_DIR / filename
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        with path.open("w", encoding="utf-8") as f:
            header = {
                "type": "header",
                "worker_index": idx,
                "subtask": subtask,
                "status": status,
                "result_preview": result[:500],
                "time_taken_seconds": elapsed,
                "total_events": len(trajectory),
                "ts": ts,
            }
            f.write(json.dumps(header, ensure_ascii=False) + "\n")
            for event in trajectory:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        return path
    except Exception as exc:
        _stderr_print(f"[warn] failed to save trajectory for worker {idx}: {exc}")
        return None


def _short(text: str, max_len: int = 80) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


def _print_trajectory(idx: int, events: list[dict]) -> None:
    """Print a concise per-worker trajectory to stderr."""
    _stderr_print(f"\n{'─' * 60}")
    _stderr_print(f"  Worker {idx + 1} Trajectory")
    _stderr_print(f"{'─' * 60}")
    for e in events:
        event = e.get("event", "?")
        ts = e.get("ts", "")
        if event == "run_one_skill_loop_start":
            _stderr_print(f"  [{ts}] START  skill={e.get('skill_name')}  task={_short(e.get('user_text', ''))}")
        elif event == "run_one_skill_loop_round_plan":
            plan = e.get("plan", {})
            ops = plan.get("ops", []) if isinstance(plan, dict) else []
            op_types = [str(o.get("type", "?")) for o in ops if isinstance(o, dict)]
            _stderr_print(f"  [{ts}] PLAN   round={e.get('round')}  ops={op_types}")
        elif event == "execute_skill_plan_output":
            result = str(e.get("result", ""))[:120]
            _stderr_print(f"  [{ts}] EXEC   skill={e.get('skill_name')}  result={result}")
        elif event == "run_one_skill_loop_continue":
            _stderr_print(f"  [{ts}] CONTINUE  round={e.get('round')}")
        elif event == "run_one_skill_loop_end":
            _stderr_print(f"  [{ts}] END    round={e.get('round')}  mode={e.get('mode')}")
    _stderr_print(f"{'─' * 60}\n")


@mcp.tool(description=EXECUTE_SUBTASKS_DESCRIPTION)
async def execute_subtasks(subtasks: List[str], workboard: str = "") -> dict:
    """Execute subtasks in parallel on Memento-S agent workers."""
    try:
        _stderr_print(f"\n{'=' * 80}")
        _stderr_print(
            f"[MementoSWorkerPool] execute_subtasks called with {len(subtasks)} subtask(s)"
        )
        _stderr_print(f"{'=' * 80}")
        for i, st in enumerate(subtasks):
            _stderr_print(f"  Subtask {i + 1}: {st}")
        _stderr_print("")

        if not subtasks or len(subtasks) < 1:
            raise ValueError("Must provide at least 1 subtask")
        if len(subtasks) > MAX_POOL_SIZE:
            raise ValueError(
                f"Too many subtasks ({len(subtasks)}) — max is {MAX_POOL_SIZE}"
            )

        # Write workboard if provided (workers receive a snapshot in their prompt)
        if workboard and workboard.strip():
            board_path = _workboard_write(workboard)
            _stderr_print(f"  [Workboard] Created at {board_path}")

        async def run_one(subtask: str, idx: int) -> Dict[str, Any]:
            max_retries = 3
            start_time = time.perf_counter()
            live_traj_path = _create_live_trajectory(idx, subtask)

            for attempt in range(max_retries):
                try:
                    async with _semaphore:
                        result = await _execute_single_subtask(subtask)
                        trajectory = []
                    elapsed = round(time.perf_counter() - start_time, 2)
                    logger.info(
                        f"[MementoSWorkerPool] Subtask [{idx}] completed in {elapsed}s"
                    )
                    if workboard and workboard.strip():
                        _workboard_check_off_item(idx + 1)
                        summary = result.strip().split("\n")[0][:200] if result.strip() else "completed"
                        _workboard_append_result(idx + 1, summary)
                    _print_trajectory(idx, trajectory)
                    traj_path = _save_trajectory(
                        idx,
                        subtask,
                        trajectory,
                        result,
                        elapsed,
                        status="finished",
                        path=live_traj_path,
                    )
                    if traj_path:
                        _stderr_print(f"  [Worker {idx + 1}] Trajectory saved → {traj_path}")
                    return {
                        "subtask_index": idx,
                        "subtask": subtask,
                        "result": result,
                        "time_taken_seconds": elapsed,
                    }
                except Exception as e:
                    elapsed = round(time.perf_counter() - start_time, 2)
                    if attempt < max_retries - 1:
                        logger.info(
                            f"[MementoSWorkerPool] Subtask [{idx}] attempt {attempt + 1}/{max_retries} "
                            f"failed after {elapsed}s: {type(e).__name__}: {str(e)[:200]}"
                        )
                        await asyncio.sleep(1)
                    else:
                        error_msg = f"{type(e).__name__}: {e}"
                        logger.error(
                            f"[MementoSWorkerPool] Subtask [{idx}] failed after {max_retries} attempts ({elapsed}s): {error_msg}"
                        )
                        _save_trajectory(
                            idx,
                            subtask,
                            [],
                            error_msg,
                            elapsed,
                            status="failed",
                            path=live_traj_path,
                        )
                        raise RuntimeError(error_msg) from e

        tasks = [run_one(st, i) for i, st in enumerate(subtasks)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        successful = []
        failed = []
        for result in results:
            if isinstance(result, Exception):
                failed.append({"error": str(result)})
            else:
                successful.append(result)

        # Summary
        _stderr_print(f"\n{'=' * 80}")
        _stderr_print(f"[MementoSWorkerPool] All subtasks completed")
        _stderr_print(f"  Successful: {len(successful)}/{len(subtasks)}")
        _stderr_print(f"  Failed: {len(failed)}/{len(subtasks)}")
        _stderr_print(f"{'=' * 80}\n")

        for r in successful:
            idx = r.get("subtask_index", "?")
            t = r.get("time_taken_seconds", 0)
            preview = r.get("result", "")[:200]
            _stderr_print(f"  Result {idx + 1} ({t}s): {preview}")

        return {
            "results": successful,
            "failed": failed,
            "subtasks_count": len(subtasks),
        }

    except Exception as e:
        logger.error(
            f"[MementoSWorkerPool] Error: {type(e).__name__}: {e}", exc_info=True
        )
        return {
            "results": [],
            "failed": [{"error": f"{type(e).__name__}: {e}"}],
            "subtasks_count": len(subtasks) if subtasks else 0,
        }


if __name__ == "__main__":
    mcp.run()
