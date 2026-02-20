"""Orchestrator MCP Server — wraps Memento-S worker pool with workboard support."""

import json
import os
import asyncio
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

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
    CLI_CREATE_ON_MISS,
    create_skill_on_miss,
)
from core.workboard import write_board, check_off_item, append_result
from core.utils.logging_utils import start_trajectory, collect_trajectory

import logging

logger = logging.getLogger(__name__)

MAX_POOL_SIZE = 10

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
- Workers can run shell commands for code validation (python -c "import ast; ast.parse(...)", python script.py, pytest)

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

        return run_one_skill_loop(subtask, skill_name)

    # Self-evolve: create a new skill on the fly when no skill matches
    if action == "none" and CLI_CREATE_ON_MISS:
        available_skill_names_list = sorted(
            {
                str(s.get("name") or "").strip()
                for s in skills
                if isinstance(s, dict) and str(s.get("name") or "").strip()
            }
        )
        created, created_skill_name, create_report = create_skill_on_miss(
            subtask,
            router_reason=str(decision.get("reason") or "").strip() or None,
            available_skill_names=available_skill_names_list,
        )
        logger.info(
            f"[Worker] create_on_miss: created={created}, skill={created_skill_name}, report={create_report[:200]}"
        )
        if created and isinstance(created_skill_name, str) and created_skill_name.strip():
            skill_name = created_skill_name.strip()
            return run_one_skill_loop(subtask, skill_name)

    return decision.get("reason", "No action taken.")


# ---------------------------------------------------------------------------
# Trajectory persistence & formatting
# ---------------------------------------------------------------------------
TRAJECTORY_LOG_DIR = Path(os.getenv(
    "TRAJECTORY_LOG_DIR",
    str(Path(__file__).resolve().parent.parent / "logs"),
))


def _execute_single_subtask_with_trajectory(subtask: str, idx: int) -> tuple[str, list[dict]]:
    """Wrap _execute_single_subtask with per-worker trajectory collection."""
    start_trajectory(f"worker-{idx}")
    result = _execute_single_subtask(subtask)
    trajectory = collect_trajectory()
    return result, trajectory


def _save_trajectory(idx: int, subtask: str, trajectory: list[dict], result: str, elapsed: float) -> Path | None:
    """Write a worker's trajectory to a JSONL file in TRAJECTORY_LOG_DIR."""
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
        print(f"[warn] failed to save trajectory for worker {idx}: {exc}", file=sys.stderr)
        return None


def _short(text: str, max_len: int = 80) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


def _print_trajectory(idx: int, events: list[dict]) -> None:
    """Print a concise per-worker trajectory to stderr."""
    print(f"\n{'─' * 60}", file=sys.stderr)
    print(f"  Worker {idx + 1} Trajectory", file=sys.stderr)
    print(f"{'─' * 60}", file=sys.stderr)
    for e in events:
        event = e.get("event", "?")
        ts = e.get("ts", "")
        if event == "run_one_skill_loop_start":
            print(f"  [{ts}] START  skill={e.get('skill_name')}  task={_short(e.get('user_text', ''))}", file=sys.stderr)
        elif event == "run_one_skill_loop_round_plan":
            plan = e.get("plan", {})
            ops = plan.get("ops", []) if isinstance(plan, dict) else []
            op_types = [str(o.get("type", "?")) for o in ops if isinstance(o, dict)]
            print(f"  [{ts}] PLAN   round={e.get('round')}  ops={op_types}", file=sys.stderr)
        elif event == "execute_skill_plan_output":
            result = str(e.get("result", ""))[:120]
            print(f"  [{ts}] EXEC   skill={e.get('skill_name')}  result={result}", file=sys.stderr)
        elif event == "run_one_skill_loop_continue":
            print(f"  [{ts}] CONTINUE  round={e.get('round')}", file=sys.stderr)
        elif event == "run_one_skill_loop_end":
            print(f"  [{ts}] END    round={e.get('round')}  mode={e.get('mode')}", file=sys.stderr)
    print(f"{'─' * 60}\n", file=sys.stderr)


@mcp.tool(description=EXECUTE_SUBTASKS_DESCRIPTION)
async def execute_subtasks(subtasks: List[str], workboard: str = "") -> dict:
    """Execute subtasks in parallel on Memento-S agent workers."""
    try:
        print(f"\n{'=' * 80}", file=sys.stderr)
        print(
            f"[MementoSWorkerPool] execute_subtasks called with {len(subtasks)} subtask(s)",
            file=sys.stderr,
        )
        print(f"{'=' * 80}", file=sys.stderr)
        for i, st in enumerate(subtasks):
            print(f"  Subtask {i + 1}: {st}", file=sys.stderr)
        print(file=sys.stderr)

        if not subtasks or len(subtasks) < 1:
            raise ValueError("Must provide at least 1 subtask")
        if len(subtasks) > MAX_POOL_SIZE:
            raise ValueError(
                f"Too many subtasks ({len(subtasks)}) — max is {MAX_POOL_SIZE}"
            )

        # Write workboard if provided (workers discover it on their own)
        if workboard and workboard.strip():
            board_path = write_board(workboard)
            print(f"  [Workboard] Created at {board_path}", file=sys.stderr)

        async def run_one(subtask: str, idx: int) -> Dict[str, Any]:
            max_retries = 3
            start_time = time.perf_counter()

            for attempt in range(max_retries):
                try:
                    async with _semaphore:
                        result, trajectory = await asyncio.to_thread(
                            _execute_single_subtask_with_trajectory, subtask, idx
                        )
                    elapsed = round(time.perf_counter() - start_time, 2)
                    logger.info(
                        f"[MementoSWorkerPool] Subtask [{idx}] completed in {elapsed}s"
                    )
                    # Auto-check-off workboard item and record result (1-indexed)
                    check_off_item(idx + 1)
                    summary = result.strip().split("\n")[0][:200] if result.strip() else "completed"
                    append_result(idx + 1, summary)
                    _print_trajectory(idx, trajectory)
                    traj_path = _save_trajectory(idx, subtask, trajectory, result, elapsed)
                    if traj_path:
                        print(f"  [Worker {idx + 1}] Trajectory saved → {traj_path}", file=sys.stderr)
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
        print(f"\n{'=' * 80}", file=sys.stderr)
        print(f"[MementoSWorkerPool] All subtasks completed", file=sys.stderr)
        print(f"  Successful: {len(successful)}/{len(subtasks)}", file=sys.stderr)
        print(f"  Failed: {len(failed)}/{len(subtasks)}", file=sys.stderr)
        print(f"{'=' * 80}\n", file=sys.stderr)

        for r in successful:
            idx = r.get("subtask_index", "?")
            t = r.get("time_taken_seconds", 0)
            preview = r.get("result", "")[:200]
            print(f"  Result {idx + 1} ({t}s): {preview}", file=sys.stderr)

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


RUN_COMMAND_DESCRIPTION = """
Run a shell command directly and return the full stdout + stderr.
Use this for verification: run the project's entry point and check for errors.
The command runs inside the Memento-S workspace directory by default.

Args:
  command: The shell command to execute (e.g. "python -m snake_game.main")
  working_dir: Working directory relative to workspace (default: workspace root)
  timeout: Max seconds to wait (default: 30)
"""


@mcp.tool(description=RUN_COMMAND_DESCRIPTION)
async def run_command(command: str, working_dir: str = "", timeout: int = 30) -> dict:
    """Run a shell command and return the output."""
    import subprocess as _sp

    workspace = Path(_MEMENTO_S_DIR) / "workspace"
    if working_dir:
        cwd = workspace / working_dir
    else:
        cwd = workspace

    if not cwd.exists():
        return {"exit_code": -1, "stdout": "", "stderr": f"Directory not found: {cwd}"}

    print(f"[run_command] {command}  (cwd={cwd})", file=sys.stderr)
    try:
        proc = _sp.run(
            command,
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        print(f"[run_command] exit={proc.returncode} stdout={stdout[:200]}", file=sys.stderr)
        return {
            "exit_code": proc.returncode,
            "stdout": stdout[:5000],
            "stderr": stderr[:5000],
        }
    except _sp.TimeoutExpired:
        return {"exit_code": -1, "stdout": "", "stderr": f"Command timed out after {timeout}s"}
    except Exception as exc:
        return {"exit_code": -1, "stdout": "", "stderr": str(exc)}


if __name__ == "__main__":
    mcp.run()
