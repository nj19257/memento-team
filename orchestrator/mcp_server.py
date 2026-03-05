"""Orchestrator MCP Server — wraps Memento-S worker pool with workboard support."""

import json
import os
import re
import asyncio
import subprocess
import sys
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Dict, List


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

MAX_POOL_SIZE = max(1, min(int(os.getenv("MAX_WORKERS", "10")), 100))
WORKSPACE_DIR = (Path(_MEMENTO_S_DIR) / "workspace").resolve()
WORKBOARD_PATH = WORKSPACE_DIR / ".workboard.md"
_ORCHESTRATOR_DIR = Path(__file__).resolve().parent
ORCHESTRATOR_SKILLS_DIR = (_ORCHESTRATOR_DIR / "skills").resolve()


# ---------------------------------------------------------------------------
# Path resolution helper (shared by bash_tool, str_replace, file_create, view)
# ---------------------------------------------------------------------------

def _resolve_path(raw: str) -> Path:
    """Resolve a user-supplied path, anchoring to WORKSPACE_DIR when needed."""
    p = Path(raw)
    if not p.is_absolute():
        return WORKSPACE_DIR / p
    if p.exists() or p.parent.exists():
        return p
    return WORKSPACE_DIR / p.relative_to(p.anchor)


def _view_directory(
    path: Path,
    max_depth: int = 2,
    current_depth: int = 0,
    prefix: str = "",
) -> str:
    lines: list[str] = []
    if current_depth == 0:
        lines.append(str(path) + "/")
    try:
        entries = sorted(
            path.iterdir(),
            key=lambda x: (not x.is_dir(), x.name.lower()),
        )
    except PermissionError:
        return f"{prefix}[permission denied]"
    entries = [
        e for e in entries if not e.name.startswith(".") and e.name != "node_modules"
    ]
    for i, entry in enumerate(entries):
        is_last = i == len(entries) - 1
        connector = "└── " if is_last else "├── "
        suffix = "/" if entry.is_dir() else ""
        lines.append(f"{prefix}{connector}{entry.name}{suffix}")
        if entry.is_dir() and current_depth < max_depth:
            extension = "    " if is_last else "│   "
            sub = _view_directory(entry, max_depth, current_depth + 1, prefix + extension)
            if sub:
                lines.append(sub)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Skill resolution helpers
# ---------------------------------------------------------------------------

def _iter_all_skill_roots() -> list[Path]:
    """Return all skill roots: orchestrator's own + Memento-S roots."""
    from core.skill_engine.skill_resolver import _iter_skill_roots
    roots: list[Path] = [ORCHESTRATOR_SKILLS_DIR]
    for r in _iter_skill_roots():
        if r not in roots:
            roots.append(r)
    return roots


def _resolve_skill_dir_all(skill_name: str) -> Path | None:
    """Resolve a skill name across all roots (orchestrator + Memento-S)."""
    if not isinstance(skill_name, str) or not skill_name.strip():
        return None
    for root in _iter_all_skill_roots():
        candidate = (root / skill_name.strip()).resolve()
        if candidate.exists() and candidate.is_dir():
            return candidate
    return None


def _stderr_print(*args: Any, **kwargs: Any) -> None:
    """Print to stderr unless MCP quiet mode is enabled."""
    if QUIET_STDERR:
        return
    kwargs.setdefault("file", sys.stderr)
    print(*args, **kwargs)

mcp = FastMCP("MementoSWorkerPool")

_semaphore = asyncio.Semaphore(MAX_POOL_SIZE)


# ===================================================================
# Orchestrator utility tools (bash, file ops, view, skills)
# ===================================================================

@mcp.tool
def bash_tool(
    command: Annotated[str, "Bash command to run"],
    description: Annotated[str, "Why I'm running this command"],
) -> str:
    """Run a bash command."""
    if not command.strip():
        return "bash_tool ERR: empty command"
    wd = WORKSPACE_DIR
    try:
        wd.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    try:
        proc = subprocess.run(
            ["bash", "-c", command],
            cwd=str(wd),
            capture_output=True,
            text=True,
            timeout=120,
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired:
        return f"bash_tool TIMEOUT after 120s: {command}"
    except FileNotFoundError as exc:
        return f"bash_tool ERR: shell not found: {exc}"
    except Exception as exc:
        return f"bash_tool ERR: {exc}"
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0:
        return f"bash_tool ERR (exit {proc.returncode}):\n{stderr or stdout}"
    return stdout or stderr or "OK"


@mcp.tool
def str_replace(
    description: Annotated[str, "Why I'm making this edit"],
    path: Annotated[str, "Path to the file to edit"],
    old_str: Annotated[str, "String to replace (must be unique in file)"],
    new_str: Annotated[str, "String to replace with (empty to delete)"] = "",
) -> str:
    """Replace a unique string in a file with another string."""
    p = _resolve_path(path)
    if not p.exists():
        return f"str_replace ERR: file not found: {p}"
    if not p.is_file():
        return f"str_replace ERR: not a file: {p}"
    content = p.read_text(encoding="utf-8", errors="replace")
    count = content.count(old_str)
    if count == 0:
        return f"str_replace ERR: old_str not found in {p}"
    if count > 1:
        return f"str_replace ERR: old_str appears {count} times in {p} (must be unique)"
    new_content = content.replace(old_str, new_str, 1)
    p.write_text(new_content, encoding="utf-8")
    return f"str_replace OK: {p}"


@mcp.tool
def file_create(
    description: Annotated[str, "Why I'm creating this file. ALWAYS PROVIDE THIS PARAMETER FIRST."],
    path: Annotated[str, "Path to the file to create. ALWAYS PROVIDE THIS PARAMETER SECOND."],
    file_text: Annotated[str, "Content to write to the file. ALWAYS PROVIDE THIS PARAMETER LAST."],
) -> str:
    """Create a new file with content."""
    p = _resolve_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(file_text, encoding="utf-8")
    return f"file_create OK: {p}"


@mcp.tool
def view(
    description: Annotated[str, "Why I need to view this"],
    path: Annotated[str, "Absolute path to file or directory"],
    view_range: Annotated[
        list[int] | None,
        "Optional [start_line, end_line] (1-indexed, -1 = end of file)",
    ] = None,
) -> str:
    """View a file (with line numbers) or directory listing."""
    p = _resolve_path(path)
    if not p.exists():
        return f"view ERR: not found: {p}"
    if p.is_dir():
        return _view_directory(p, max_depth=2)
    _IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
    if p.suffix.lower() in _IMAGE_EXTS:
        size = p.stat().st_size
        return f"[Image file: {p} ({size} bytes)]"
    try:
        content = p.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return f"view ERR: cannot read {p}: {exc}"
    lines = content.splitlines()
    if view_range is not None and len(view_range) == 2:
        start, end = view_range
        start = max(1, start)
        if end == -1:
            end = len(lines)
        end = min(end, len(lines))
        lines = lines[start - 1 : end]
        offset = start
    else:
        offset = 1
    numbered = [f"{offset + i:>6}\t{line}" for i, line in enumerate(lines)]
    return "\n".join(numbered)


@mcp.tool
def read_skill(
    skill_name: Annotated[str, "Name of the skill to read"],
) -> str:
    """Read a skill's SKILL.md content."""
    name = str(skill_name or "").strip()
    if not name:
        return "read_skill ERR: empty skill name"
    skill_dir = _resolve_skill_dir_all(name)
    if skill_dir is None:
        try:
            from core.skill_engine.skill_resolver import openskills_read
            return openskills_read(name)
        except Exception as exc:
            return f"read_skill ERR: {exc}"
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return f"read_skill ERR: SKILL.md not found in {skill_dir}"
    raw = skill_md.read_text(encoding="utf-8")
    base_dir = str(skill_dir.resolve())
    rendered = raw.replace("{baseDir}", base_dir)
    prefix = (
        f"[Local skill path]\n{base_dir}\n"
        f"[Tip]\nUse scripts from this path. For shell scripts, prefer "
        f"`bash {base_dir}/scripts/<script>.sh ...` if direct execution fails.\n\n"
    )
    return prefix + rendered


@mcp.tool
def list_local_skills() -> str:
    """List all locally available skills with their descriptions."""
    seen: set[str] = set()
    lines: list[str] = []
    for root in _iter_all_skill_roots():
        if not root.exists() or not root.is_dir():
            continue
        try:
            for skill_dir in sorted(root.iterdir(), key=lambda p: p.name.lower()):
                if not skill_dir.is_dir():
                    continue
                skill_md = skill_dir / "SKILL.md"
                if not skill_md.exists():
                    continue
                name = skill_dir.name
                if name in seen:
                    continue
                seen.add(name)
                desc = ""
                try:
                    for raw_line in skill_md.read_text(encoding="utf-8").splitlines():
                        line = raw_line.strip()
                        if not line or line.startswith("#") or line.startswith("```") or line.startswith("---"):
                            continue
                        if line.startswith("-") or line.startswith("*") or line.startswith("<"):
                            continue
                        desc = line[:200]
                        break
                except Exception:
                    pass
                lines.append(f"- {name}: {desc}" if desc else f"- {name}")
        except Exception:
            continue
    return "\n".join(lines) if lines else "(no local skills found)"


# ===================================================================
# Workboard helpers
# ===================================================================

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


def _workboard_uses_tag_protocol() -> bool:
    if not WORKBOARD_PATH.exists():
        return False
    try:
        text = WORKBOARD_PATH.read_text(encoding="utf-8")
    except Exception:
        return False
    return bool(re.search(r"<t\d+_[A-Za-z0-9_:-]*>.*?</t\d+_[A-Za-z0-9_:-]*>", text, re.DOTALL))


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
2. ATOMIC: One focused task per subtask

Args:
  subtasks: List[str]
    List of 1 to {MAX_POOL_SIZE} fully self-contained task descriptions.
  workboard: str (RECOMMENDED — always provide)
    Markdown content for a shared workboard file that lists the subtasks
    and provides tagged worker slots (e.g. <t1_result></t1_result>).
    Workers can use read_workboard/edit_workboard to fill their own tags.
    Include subtask IDs (t1, t2, ...) in the board and subtask descriptions.
"""


async def _execute_single_subtask(subtask: str, subtask_id: str | None = None) -> str:
    """Run a single subtask using the new Memento-S MCPAgent."""
    execution_text = subtask
    board_content = _workboard_read()
    sid = str(subtask_id or "").strip()
    tag_prefix = f"{sid}_" if sid else ""
    if board_content and board_content != "(no workboard exists)":
        execution_text = (
            f"{subtask}\n\n"
            "## Workboard Coordination\n"
            "A shared workboard exists. Use `read_workboard` and `edit_workboard` "
            "to read and fill your assigned tagged sections.\n"
            + (f"Your subtask ID is `{sid}`. Only edit tags starting with `{tag_prefix}`.\n" if sid else "")
            + "Read the board first, then write concise updates into your tags.\n\n"
            f"```markdown\n{board_content}\n```"
        )

    agent = MCPAgent(model=build_chat_model(), base_dir=WORKSPACE_DIR)
    await agent.start()
    try:
        result = await agent.run(execution_text)
        return _extract_agent_output(result).strip()
    finally:
        await agent.close()


def _trajectory_event(event: str, **fields: Any) -> dict[str, Any]:
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": str(event),
        **fields,
    }


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
        if event == "worker_start":
            _stderr_print(f"  [{ts}] START  subtask={_short(e.get('subtask', ''))}")
        elif event == "worker_attempt_start":
            _stderr_print(f"  [{ts}] TRY    attempt={e.get('attempt')}  subtask_id={e.get('subtask_id')}")
        elif event == "worker_prompt_built":
            _stderr_print(f"  [{ts}] PROMPT subtask_id={e.get('subtask_id')}")
        elif event == "agent_tools_loaded":
            _stderr_print(f"  [{ts}] TOOLS  count={e.get('tool_count')} names={_short(str(e.get('tool_names', [])), 80)}")
        elif event == "agent_run_start":
            _stderr_print(f"  [{ts}] AGENT  run_start messages={e.get('message_count')}")
        elif event == "tool_call_start":
            _stderr_print(f"  [{ts}] TOOL   {e.get('tool_name')} start")
        elif event == "tool_call_end":
            _stderr_print(
                f"  [{ts}] TOOL   {e.get('tool_name')} ok {e.get('duration_ms')}ms result={_short(str(e.get('result_preview', '')), 80)}"
            )
        elif event == "tool_call_error":
            _stderr_print(
                f"  [{ts}] TOOL   {e.get('tool_name')} ERR {e.get('duration_ms')}ms { _short(str(e.get('error','')), 80)}"
            )
        elif event == "workboard_snapshot_read":
            _stderr_print(f"  [{ts}] BOARD  snapshot bytes={e.get('bytes')}")
        elif event == "workboard_checkbox_update":
            _stderr_print(f"  [{ts}] BOARD  checkbox item={e.get('item')} checked")
        elif event == "workboard_result_append":
            _stderr_print(f"  [{ts}] BOARD  result_append item={e.get('item')}")
        elif event == "worker_agent_invoke_end":
            _stderr_print(f"  [{ts}] AGENT  run_end result={_short(str(e.get('result_preview','')), 80)}")
        elif event == "worker_end":
            _stderr_print(f"  [{ts}] END    status={e.get('status')} sec={e.get('duration_seconds')}")
        elif event == "run_one_skill_loop_start":
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
            trajectory: list[dict[str, Any]] = []

            def record(event: str, **fields: Any) -> None:
                e = _trajectory_event(event, worker_index=idx, **fields)
                trajectory.append(e)
                if live_traj_path is not None:
                    _append_live_trajectory_event(live_traj_path, e)

            record("worker_start", subtask=subtask)

            for attempt in range(max_retries):
                try:
                    async with _semaphore:
                        subtask_id = f"t{idx + 1}"
                        worker_input = f"[Subtask ID: {subtask_id}]\n{subtask}"
                        record("worker_attempt_start", attempt=attempt + 1, subtask_id=subtask_id)

                        def sink(ev: dict[str, Any]) -> None:
                            payload = dict(ev)
                            payload.setdefault("worker_index", idx)
                            payload.setdefault("subtask_id", subtask_id)
                            trajectory.append(payload)
                            if live_traj_path is not None:
                                _append_live_trajectory_event(live_traj_path, payload)

                        # Rebuild prompt here so we can emit wrapper events and use MCPAgent sink.
                        execution_text = worker_input
                        board_content = _workboard_read()
                        if board_content and board_content != "(no workboard exists)":
                            sink(_trajectory_event("workboard_snapshot_read", bytes=len(board_content.encode("utf-8"))))
                            tag_prefix = f"{subtask_id}_"
                            execution_text = (
                                f"{worker_input}\n\n"
                                "## Workboard Coordination\n"
                                "A shared workboard exists. Use `read_workboard` and `edit_workboard` "
                                "to read and fill your assigned tagged sections.\n"
                                f"Your subtask ID is `{subtask_id}`. Only edit tags starting with `{tag_prefix}`.\n"
                                "Read the board first, then write concise updates into your tags.\n\n"
                                f"```markdown\n{board_content}\n```"
                            )
                        sink(_trajectory_event("worker_prompt_built", subtask_id=subtask_id, prompt_preview=execution_text[:500]))
                        agent = MCPAgent(model=build_chat_model(), base_dir=WORKSPACE_DIR, event_sink=sink)
                        await agent.start()
                        try:
                            sink(_trajectory_event("worker_agent_invoke_start", subtask_id=subtask_id))
                            agent_result = await agent.run(execution_text)
                            result = _extract_agent_output(agent_result).strip()
                            sink(_trajectory_event("worker_agent_invoke_end", subtask_id=subtask_id, result_preview=result[:500]))
                        finally:
                            await agent.close()
                    elapsed = round(time.perf_counter() - start_time, 2)
                    logger.info(
                        f"[MementoSWorkerPool] Subtask [{idx}] completed in {elapsed}s"
                    )
                    if workboard and workboard.strip():
                        _workboard_check_off_item(idx + 1)
                        record("workboard_checkbox_update", subtask_id=subtask_id, item=idx + 1, status="checked")
                        if not _workboard_uses_tag_protocol():
                            summary = result.strip().split("\n")[0][:200] if result.strip() else "completed"
                            _workboard_append_result(idx + 1, summary)
                            record("workboard_result_append", subtask_id=subtask_id, item=idx + 1, summary=summary)
                    record("worker_end", subtask_id=subtask_id, duration_seconds=elapsed, status="ok")
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
                    record("worker_attempt_error", attempt=attempt + 1, error=f"{type(e).__name__}: {e}")
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
                        record("worker_end", duration_seconds=elapsed, status="failed", error=error_msg)
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
