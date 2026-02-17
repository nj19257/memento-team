"""Orchestrator MCP Server — wraps Memento-S worker pool with workboard support."""

import json
import os
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

from agent import (
    run_one_skill_loop,
    route_skill,
    load_available_skills_block,
    parse_available_skills,
    has_local_skill_dir,
    ensure_skill_available,
)
from core.workboard import write_board, read_board
from core.utils.logging_utils import start_trajectory, collect_trajectory

import logging

logger = logging.getLogger(__name__)

MAX_POOL_SIZE = 5


def _stderr_print(*args: Any, **kwargs: Any) -> None:
    """Print to stderr unless MCP quiet mode is enabled."""
    if QUIET_STDERR:
        return
    kwargs.setdefault("file", sys.stderr)
    print(*args, **kwargs)

mcp = FastMCP("MementoSWorkerPool")

_semaphore = asyncio.Semaphore(MAX_POOL_SIZE)

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
    and provides a Results section for workers to fill in. Workers can
    read and edit it during execution via read_workboard/edit_workboard.
"""


def _load_skills_catalog() -> tuple[list[dict], str, set[str]]:
    """Load skills catalog from AGENTS.md. Returns (skills, skills_xml, skill_names)."""
    skills_xml = load_available_skills_block()
    skills = parse_available_skills(skills_xml)
    skill_names = {
        s.get("name") for s in skills if isinstance(s, dict) and s.get("name")
    }
    return skills, skills_xml, skill_names


def _execute_single_subtask(subtask: str) -> str:
    """Run a single subtask through Memento-S routing and execution. (sync)"""
    skills, skills_xml, skill_names = _load_skills_catalog()

    decision = route_skill(subtask, skills, skills_xml)
    action = decision.get("action", "none")

    if action == "next_step":
        skill_name = decision.get("name", "")
        if not isinstance(skill_name, str) or not skill_name.strip():
            return f"Error: router returned next_step but no skill name. Decision: {decision}"
        skill_name = skill_name.strip()

        # Ensure skill is available (dynamic fetch if needed)
        if skill_name not in skill_names and not has_local_skill_dir(skill_name):
            ok, fetch_msg = ensure_skill_available(skill_name)
            if not ok:
                return f"Error: skill {skill_name!r} not found. {fetch_msg}"

        # Enrich subtask with workboard context for the execution loop
        execution_text = subtask
        board_content = read_board()
        if board_content and board_content != "(no workboard exists)":
            execution_text = (
                f"{subtask}\n\n"
                "## Active Workboard\n"
                "A shared workboard exists with the following content. "
                "After completing your primary task, you MUST update the workboard "
                "to mark your subtask as done and record your results.\n\n"
                f"```markdown\n{board_content}\n```\n\n"
                "Include edit_workboard ops in your plan to update it."
            )
        return run_one_skill_loop(execution_text, skill_name)
    else:
        return decision.get("reason", "No action taken.")


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
    """Wrap _execute_single_subtask with per-worker trajectory collection."""
    if live_path is not None:
        start_trajectory(
            f"worker-{idx}",
            event_sink=lambda e, p=live_path: _append_live_trajectory_event(p, e),
        )
    else:
        start_trajectory(f"worker-{idx}")
    result = _execute_single_subtask(subtask)
    trajectory = collect_trajectory()
    return result, trajectory


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

        # Write workboard if provided (workers discover it on their own)
        if workboard and workboard.strip():
            board_path = write_board(workboard)
            _stderr_print(f"  [Workboard] Created at {board_path}")

        async def run_one(subtask: str, idx: int) -> Dict[str, Any]:
            max_retries = 3
            start_time = time.perf_counter()
            live_traj_path = _create_live_trajectory(idx, subtask)

            for attempt in range(max_retries):
                try:
                    async with _semaphore:
                        result, trajectory = await asyncio.to_thread(
                            _execute_single_subtask_with_trajectory, subtask, idx, live_traj_path
                        )
                    elapsed = round(time.perf_counter() - start_time, 2)
                    logger.info(
                        f"[MementoSWorkerPool] Subtask [{idx}] completed in {elapsed}s"
                    )
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
