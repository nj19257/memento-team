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
    """Find-and-replace *old_text* with *new_text* in the workboard.

    Uses exact match first, then falls back to stripped-whitespace matching
    for checkbox lines to handle minor formatting differences from workers.
    """
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
    with _lock:
        path = get_board_path()
        if not path.exists():
            return "append_board ERR: workboard does not exist"
        content = path.read_text(encoding="utf-8")
        path.write_text(content.rstrip() + "\n\n" + text + "\n", encoding="utf-8")
        return "append_board OK"


def append_result(index: int, text: str) -> str:
    """Append a result line under the ## Results section, ordered by task index."""
    with _lock:
        path = get_board_path()
        if not path.exists():
            return "append_result ERR: workboard does not exist"
        content = path.read_text(encoding="utf-8")
        one_line = " ".join(str(text).split())[:200]
        result_line = f"- Task {index}: {one_line}"
        marker = "## Results"
        marker_idx = content.find(marker)
        if marker_idx == -1:
            content = content.rstrip() + f"\n\n{marker}\n{result_line}\n"
        else:
            marker_line_end = content.find("\n", marker_idx)
            if marker_line_end == -1:
                content += f"\n{result_line}\n"
            else:
                # Find section boundaries (up to next ## or EOF)
                section_start = marker_line_end + 1
                next_section = content.find("\n##", section_start)
                if next_section == -1:
                    section_end = len(content)
                    after_section = ""
                else:
                    section_end = next_section + 1
                    after_section = content[section_end:]

                # Parse existing result lines and non-result lines
                existing_results = []
                for line in content[section_start:section_end].splitlines():
                    m = re.match(r"^- Task (\d+):", line)
                    if m:
                        existing_results.append((int(m.group(1)), line))
                    # Drop non-result lines (e.g. placeholder text)

                # Upsert: replace if same index exists, else add
                existing_results = [(i, l) for i, l in existing_results if i != index]
                existing_results.append((index, result_line))
                existing_results.sort(key=lambda x: x[0])

                new_section = "\n".join(l for _, l in existing_results) + "\n"
                content = content[:section_start] + new_section + after_section
        path.write_text(content, encoding="utf-8")
        return f"append_result OK: task {index}"


def check_off_item(index: int) -> str:
    """Mark checkbox item *index* as done: ``- [ ] {index}:`` → ``- [x] {index}:``."""
    with _lock:
        path = get_board_path()
        if not path.exists():
            return "check_off_item ERR: workboard does not exist"
        content = path.read_text(encoding="utf-8")
        pattern = re.compile(rf'^(\s*-\s)\[ \](\s+{index}\b)', re.MULTILINE)
        new_content, n = pattern.subn(r'\1[x]\2', content, count=1)
        if n == 0:
            return f"check_off_item SKIP: item {index} not found or already checked"
        path.write_text(new_content, encoding="utf-8")
        return f"check_off_item OK: item {index}"
