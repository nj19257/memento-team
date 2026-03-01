"""Workboard FastMCP server with orchestrator approval queue.

Exposes workboard tools to workers (read, write, edit, append, cleanup).
``edit_board`` and ``append_board`` are queued — the worker blocks on an
``asyncio.Event`` until the orchestrator reviews and resolves the request.

Python API (called by the orchestrator or tests — plain functions):
    - ``read_board_sync()``       — read workboard content
    - ``write_board_sync(content)``— write workboard content
    - ``cleanup_board_sync()``    — delete workboard file
    - ``submit_edit(old_text, new_text, reason)`` — async, queues edit request
    - ``submit_append(text, reason)``             — async, queues append request
    - ``get_pending_requests()``  — drain and return all unresolved edits
    - ``resolve_request(req, approved, feedback)`` — approve/reject, unblock worker
"""

from __future__ import annotations

import asyncio
import json
import threading
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

from fastmcp import FastMCP

from core.config import WORKSPACE_DIR

mcp = FastMCP("workboard")

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------
_board_path: Path = WORKSPACE_DIR / ".workboard.md"
_board_lock = threading.Lock()

# Pending edit requests: request_id -> EditRequest
_pending: dict[str, "EditRequest"] = {}
_pending_lock = threading.Lock()

# Worker context (set by orchestrator before launching each worker task)
_current_worker_idx: ContextVar[int] = ContextVar("worker_idx", default=-1)


def configure(*, base_dir: Path | None = None) -> None:
    """Set the workspace root for the workboard file."""
    global _board_path
    if base_dir is not None:
        _board_path = base_dir / ".workboard.md"


def set_worker_context(worker_idx: int):
    """Set the worker index in the current task's context.

    Called by the orchestrator before each ``asyncio.create_task`` so that
    ``edit_board`` / ``append_board`` can tag requests with the correct worker.
    """
    return _current_worker_idx.set(worker_idx)


# ---------------------------------------------------------------------------
# Edit-request dataclass
# ---------------------------------------------------------------------------

@dataclass
class EditRequest:
    request_id: str
    worker_idx: int
    edit_type: str  # "edit" or "append"
    params: dict[str, Any]
    reason: str
    board_snapshot: str
    event: asyncio.Event
    result: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Plain Python API (thread-safe file I/O)
# ---------------------------------------------------------------------------

def read_board_sync() -> str:
    """Read the workboard file.  Returns ``""`` if it doesn't exist."""
    with _board_lock:
        if not _board_path.exists():
            return ""
        return _board_path.read_text(encoding="utf-8")


def write_board_sync(content: str) -> None:
    """Create or overwrite the workboard file."""
    with _board_lock:
        _board_path.parent.mkdir(parents=True, exist_ok=True)
        _board_path.write_text(content, encoding="utf-8")


def cleanup_board_sync() -> None:
    """Delete the workboard file if it exists."""
    with _board_lock:
        if _board_path.exists():
            _board_path.unlink()


async def submit_edit(
    old_text: str, new_text: str, reason: str
) -> str:
    """Queue an edit request and block until the orchestrator resolves it.

    Returns a JSON string: ``{"status": "success"/"failure", "feedback": "..."}``.
    """
    snapshot = read_board_sync()
    request = EditRequest(
        request_id=str(uuid4()),
        worker_idx=_current_worker_idx.get(),
        edit_type="edit",
        params={"old_text": old_text, "new_text": new_text},
        reason=reason,
        board_snapshot=snapshot,
        event=asyncio.Event(),
    )
    with _pending_lock:
        _pending[request.request_id] = request

    await request.event.wait()

    with _pending_lock:
        _pending.pop(request.request_id, None)

    return json.dumps(request.result)


async def submit_append(text: str, reason: str) -> str:
    """Queue an append request and block until the orchestrator resolves it.

    Returns a JSON string: ``{"status": "success"/"failure", "feedback": "..."}``.
    """
    snapshot = read_board_sync()
    request = EditRequest(
        request_id=str(uuid4()),
        worker_idx=_current_worker_idx.get(),
        edit_type="append",
        params={"text": text},
        reason=reason,
        board_snapshot=snapshot,
        event=asyncio.Event(),
    )
    with _pending_lock:
        _pending[request.request_id] = request

    await request.event.wait()

    with _pending_lock:
        _pending.pop(request.request_id, None)

    return json.dumps(request.result)


async def get_pending_requests() -> list[EditRequest]:
    """Return all unresolved edit requests from the pending queue.

    Only returns requests that have not yet been resolved (``result is None``).
    """
    with _pending_lock:
        requests = [r for r in _pending.values() if r.result is None]
    return requests


async def resolve_request(
    request: EditRequest,
    approved: bool,
    feedback: str = "",
) -> None:
    """Resolve an edit request: apply if approved, reject otherwise, then unblock worker."""
    if approved:
        if request.edit_type == "edit":
            old_text = request.params["old_text"]
            new_text = request.params["new_text"]
            content = read_board_sync()
            if old_text in content:
                content = content.replace(old_text, new_text, 1)
                write_board_sync(content)
            else:
                request.result = {"status": "failure", "feedback": feedback or "old_text not found in workboard"}
                request.event.set()
                return
        elif request.edit_type == "append":
            text = request.params["text"]
            content = read_board_sync()
            content = content + "\n" + text if content else text
            write_board_sync(content)
        request.result = {"status": "success", "feedback": feedback}
    else:
        request.result = {"status": "failure", "feedback": feedback}

    request.event.set()


# ---------------------------------------------------------------------------
# MCP Tools (exposed to workers via FastMCP protocol)
# These wrap the plain Python API above.
# ---------------------------------------------------------------------------

@mcp.tool
def read_board() -> str:
    """Read the current shared workboard.

    The workboard is a live document that changes as workers update it.
    It contains the task plan, subtask checklist, shared context, and
    updates from other workers. Read it at the start and re-read it
    during your work to see the latest updates."""
    content = read_board_sync()
    if not content:
        return "(no workboard exists)"
    return content


@mcp.tool
def write_board(
    content: Annotated[str, "Full markdown content for the workboard"],
) -> str:
    """Create or overwrite the shared workboard."""
    write_board_sync(content)
    return f"write_board OK: {_board_path}"


@mcp.tool
async def edit_board(
    old_text: Annotated[str, "Exact text to find in the workboard"],
    new_text: Annotated[str, "Text to replace it with"],
    reason: Annotated[str, "Why this edit is needed"],
) -> str:
    """Update the shared workboard by replacing text.

    Use this to update your progress, post findings, or mark your checklist item as done.
    Returns JSON with status and feedback."""
    return await submit_edit(old_text, new_text, reason)


@mcp.tool
async def append_board(
    text: Annotated[str, "Text to append to the workboard"],
    reason: Annotated[str, "Why this append is needed"],
) -> str:
    """Append text to the shared workboard.

    Use this to add new findings, notes, or results to the workboard.
    Returns JSON with status and feedback."""
    return await submit_append(text, reason)


@mcp.tool
def cleanup_board() -> str:
    """Delete the workboard file."""
    cleanup_board_sync()
    return "cleanup_board OK"
