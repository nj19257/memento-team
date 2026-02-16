"""Shared workboard for coordinating parallel Memento-S workers.

All public functions are protected by a module-level ``threading.Lock`` so
that concurrent workers (running via ``asyncio.to_thread()``) can safely
read and edit the same markdown file.
"""

from __future__ import annotations

import threading
from pathlib import Path

from core.config import WORKSPACE_DIR

_lock = threading.Lock()


def get_board_path() -> Path:
    """Return the canonical workboard file path."""
    return WORKSPACE_DIR / ".workboard.md"


def cleanup_board() -> None:
    """Delete the workboard file if it exists."""
    with _lock:
        path = get_board_path()
        if path.exists():
            path.unlink()


def write_board(content: str) -> Path:
    """Write *content* to the workboard file (creates parent dirs as needed)."""
    with _lock:
        path = get_board_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path


def read_board() -> str:
    """Return the full markdown content of the workboard."""
    with _lock:
        path = get_board_path()
        if not path.exists():
            return "(no workboard exists)"
        return path.read_text(encoding="utf-8")


def edit_board(old_text: str, new_text: str) -> str:
    """Find-and-replace *old_text* with *new_text* in the workboard."""
    with _lock:
        path = get_board_path()
        if not path.exists():
            return "edit_board ERR: workboard does not exist"
        content = path.read_text(encoding="utf-8")
        if old_text not in content:
            return f"edit_board ERR: old_text not found in workboard"
        new_content = content.replace(old_text, new_text, 1)
        path.write_text(new_content, encoding="utf-8")
        return "edit_board OK"


def append_board(text: str) -> str:
    """Append text to the workboard file."""
    with _lock:
        path = get_board_path()
        if not path.exists():
            return "append_board ERR: workboard does not exist"
        content = path.read_text(encoding="utf-8")
        path.write_text(content.rstrip() + "\n\n" + text + "\n", encoding="utf-8")
        return "append_board OK"
