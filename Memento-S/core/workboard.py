"""Shared workboard for coordinating parallel Memento-S workers.

All public functions are protected by a module-level ``threading.Lock`` so
that concurrent workers (running via ``asyncio.to_thread()``) can safely
read and edit the same markdown file.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

from core.config import WORKSPACE_DIR

_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Orchestrator mode — thread-local flag
# ---------------------------------------------------------------------------
_orchestrator_local = threading.local()


def set_orchestrator_mode(enabled: bool) -> None:
    """Enable/disable orchestrator mode for the current thread.

    When enabled, ``edit_board()`` and ``append_board()`` return a BLOCKED
    message instead of modifying the workboard.  ``read_board()`` stays
    available so workers can still take a snapshot.
    """
    _orchestrator_local.enabled = enabled


def is_orchestrator_mode() -> bool:
    """Return *True* if the current thread is in orchestrator mode."""
    return getattr(_orchestrator_local, "enabled", False)


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
    if is_orchestrator_mode():
        return "edit_board BLOCKED: workboard writes are disabled during task execution"
    with _lock:
        path = get_board_path()
        if not path.exists():
            return "edit_board ERR: workboard does not exist"
        content = path.read_text(encoding="utf-8")

        # Exact match — fast path
        if old_text in content:
            new_content = content.replace(old_text, new_text, 1)
            path.write_text(new_content, encoding="utf-8")
            return "edit_board OK"

        # Fuzzy match — try stripping whitespace on each line
        old_stripped = old_text.strip()
        if old_stripped:
            for line in content.splitlines():
                if line.strip() == old_stripped:
                    new_content = content.replace(line, new_text.strip(), 1)
                    path.write_text(new_content, encoding="utf-8")
                    return "edit_board OK (fuzzy)"

        return "edit_board ERR: old_text not found in workboard"


def append_board(text: str) -> str:
    """Append text to the workboard file."""
    if is_orchestrator_mode():
        return "append_board BLOCKED: workboard writes are disabled during task execution"
    with _lock:
        path = get_board_path()
        if not path.exists():
            return "append_board ERR: workboard does not exist"
        content = path.read_text(encoding="utf-8")
        path.write_text(content.rstrip() + "\n\n" + text + "\n", encoding="utf-8")
        return "append_board OK"


# ---------------------------------------------------------------------------
# Mechanical fallback helpers (used when LLM-based update fails)
# ---------------------------------------------------------------------------

def check_off_item(idx: int) -> str:
    """Check off the *idx*-th ``- [ ]`` checkbox (0-based) in the workboard."""
    with _lock:
        path = get_board_path()
        if not path.exists():
            return "check_off_item ERR: workboard does not exist"
        content = path.read_text(encoding="utf-8")
        matches = list(re.finditer(r"- \[ \]", content))
        if idx < 0 or idx >= len(matches):
            return f"check_off_item ERR: index {idx} out of range (found {len(matches)} unchecked items)"
        m = matches[idx]
        new_content = content[: m.start()] + "- [x]" + content[m.end() :]
        path.write_text(new_content, encoding="utf-8")
        return "check_off_item OK"


def append_result(idx: int, text: str) -> str:
    """Append a result entry for subtask *idx* under the ``## Results`` section."""
    with _lock:
        path = get_board_path()
        if not path.exists():
            return "append_result ERR: workboard does not exist"
        content = path.read_text(encoding="utf-8")
        entry = f"\n### Subtask {idx + 1}\n{text.strip()}\n"
        # Try to append under an existing ## Results heading
        results_match = re.search(r"^## Results", content, re.MULTILINE)
        if results_match:
            insert_pos = len(content)
            # Find the next ## heading after ## Results to insert before it
            next_heading = re.search(r"^## ", content[results_match.end() :], re.MULTILINE)
            if next_heading:
                insert_pos = results_match.end() + next_heading.start()
            new_content = content[:insert_pos].rstrip() + "\n" + entry + "\n" + content[insert_pos:]
        else:
            new_content = content.rstrip() + "\n\n## Results\n" + entry
        path.write_text(new_content, encoding="utf-8")
        return "append_result OK"
