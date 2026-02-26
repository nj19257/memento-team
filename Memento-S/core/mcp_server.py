"""Unified FastMCP server with workboard-aware MCP tools.

A single in-process server that the LLM can call via the standard MCP
protocol instead of the legacy ops/bridge dispatcher.

Core tools: bash_tool, str_replace, file_create, view
Coordination tools: read_workboard, edit_workboard
Skills tools: search_cloud_skills, read_skill, list_local_skills
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Annotated

from fastmcp import FastMCP

from core.config import WORKSPACE_DIR

mcp = FastMCP("memento")

# ---------------------------------------------------------------------------
# Shared state – set once at startup via configure()
# ---------------------------------------------------------------------------
_base_dir: Path = WORKSPACE_DIR
_workboard_lock = threading.Lock()


def configure(*, base_dir: Path | None = None) -> None:
    """Set the working context for the MCP server tools."""
    global _base_dir
    if base_dir is not None:
        _base_dir = base_dir


def _resolve_path(raw: str) -> Path:
    """Resolve a user-supplied path, anchoring it to ``_base_dir`` when needed.

    LLMs sometimes emit paths like ``/skills/foo`` intending a project-relative
    path.  If the path is absolute but doesn't exist on the real filesystem, we
    treat it as relative to ``_base_dir`` so it doesn't land in the root.
    """
    p = Path(raw)
    if not p.is_absolute():
        return _base_dir / p
    # Already absolute – use it directly only if it already exists or is
    # clearly inside an existing parent (e.g. the user's home directory).
    # Otherwise assume the LLM meant it relative to _base_dir.
    if p.exists() or p.parent.exists():
        return p
    return _base_dir / p.relative_to(p.anchor)


def _workboard_path() -> Path:
    return _base_dir / ".workboard.md"


# ===================================================================
# 1. bash_tool
# ===================================================================

@mcp.tool
def bash_tool(
    command: Annotated[str, "Bash command to run in container"],
    description: Annotated[str, "Why I'm running this command"],
) -> str:
    """Run a bash command in the container."""
    if not command.strip():
        return "bash_tool ERR: empty command"

    wd = _base_dir
    try:
        wd.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    env = os.environ.copy()

    try:
        proc = subprocess.run(
            ["bash", "-c", command],
            cwd=str(wd),
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
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


# ===================================================================
# 2. str_replace
# ===================================================================

@mcp.tool
def str_replace(
    description: Annotated[str, "Why I'm making this edit"],
    path: Annotated[str, "Path to the file to edit"],
    old_str: Annotated[str, "String to replace (must be unique in file)"],
    new_str: Annotated[str, "String to replace with (empty to delete)"] = "",
) -> str:
    """Replace a unique string in a file with another string. The string to replace must appear exactly once in the file."""
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


# ===================================================================
# 3. file_create
# ===================================================================

@mcp.tool
def file_create(
    description: Annotated[str, "Why I'm creating this file. ALWAYS PROVIDE THIS PARAMETER FIRST."],
    path: Annotated[str, "Path to the file to create. ALWAYS PROVIDE THIS PARAMETER SECOND."],
    file_text: Annotated[str, "Content to write to the file. ALWAYS PROVIDE THIS PARAMETER LAST."],
) -> str:
    """Create a new file with content in the container."""
    p = _resolve_path(path)

    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(file_text, encoding="utf-8")
    return f"file_create OK: {p}"


# ===================================================================
# 4. view
# ===================================================================

@mcp.tool
def view(
    description: Annotated[str, "Why I need to view this"],
    path: Annotated[str, "Absolute path to file or directory, e.g. `/repo/file.py` or `/repo`."],
    view_range: Annotated[
        list[int] | None,
        "Optional line range for text files. Format: [start_line, end_line] where lines are indexed starting at 1. Use [start_line, -1] to view from start_line to end of file.",
    ] = None,
) -> str:
    """Supports viewing text, images, and directory listings.

    Supported path types:
    - Directories: Lists files and directories up to 2 levels deep, ignoring hidden items and node_modules
    - Image files (.jpg, .jpeg, .png, .gif, .webp): Displays the image visually
    - Text files: Displays numbered lines. You can optionally specify a view_range to see specific lines.

    Note: Files with non-UTF-8 encoding will display hex escapes (e.g. \\x84) for invalid bytes"""
    p = _resolve_path(path)

    if not p.exists():
        return f"view ERR: not found: {p}"

    # Directory listing
    if p.is_dir():
        return _view_directory(p, max_depth=2)

    # Image files
    _IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
    if p.suffix.lower() in _IMAGE_EXTS:
        size = p.stat().st_size
        return f"[Image file: {p} ({size} bytes)]"

    # Text files
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

    # Filter hidden items and node_modules
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


# ===================================================================
# 5. read_workboard
# ===================================================================


@mcp.tool
def read_workboard(
    tag: Annotated[str, "Optional tag name to read (e.g. t1_result). Empty reads full board."] = "",
) -> str:
    """Read the shared workboard, or a single tagged section."""
    path = _workboard_path()
    if not path.exists():
        return "read_workboard ERR: workboard does not exist"
    text = path.read_text(encoding="utf-8")
    tag_name = str(tag or "").strip()
    if not tag_name:
        return text
    if not re.fullmatch(r"[A-Za-z0-9_:-]+", tag_name):
        return f"read_workboard ERR: invalid tag: {tag_name!r}"
    m = re.search(rf"<{re.escape(tag_name)}>(.*?)</{re.escape(tag_name)}>", text, re.DOTALL)
    if not m:
        return f"read_workboard ERR: tag not found: {tag_name}"
    return m.group(1).strip()


# ===================================================================
# 6. edit_workboard
# ===================================================================


@mcp.tool
def edit_workboard(
    tag: Annotated[str, "Tag name to replace (e.g. t1_result)"],
    content: Annotated[str, "New content to write inside the tag block"],
) -> str:
    """Replace the content of a tagged workboard section: <tag>...</tag>."""
    tag_name = str(tag or "").strip()
    if not tag_name:
        return "edit_workboard ERR: missing tag"
    if not re.fullmatch(r"[A-Za-z0-9_:-]+", tag_name):
        return f"edit_workboard ERR: invalid tag: {tag_name!r}"

    path = _workboard_path()
    if not path.exists():
        return "edit_workboard ERR: workboard does not exist"

    with _workboard_lock:
        text = path.read_text(encoding="utf-8")
        pattern = re.compile(
            rf"(<{re.escape(tag_name)}>)(.*?)(</{re.escape(tag_name)}>)",
            re.DOTALL,
        )
        m = pattern.search(text)
        if not m:
            return f"edit_workboard ERR: tag not found: {tag_name}"

        new_inner = str(content or "")
        replacement = m.group(1) + new_inner + m.group(3)
        new_text = text[: m.start()] + replacement + text[m.end() :]
        path.write_text(new_text, encoding="utf-8")
    return f"edit_workboard OK: {tag_name}"


# ===================================================================
# 7. search_cloud_skills
# ===================================================================

@mcp.tool
def search_cloud_skills(
    query: Annotated[str, "Search query to find matching skills"],
    top_k: Annotated[int, "Maximum number of results to return"] = 8,
) -> str:
    """Search for skills in the cloud catalog."""
    from cli.skill_search import (
        load_cloud_skill_catalog as _load_catalog,
        search_cloud_skills as _search,
    )
    entries, meta = _load_catalog()
    if not entries:
        err = str(meta.get("error") or "catalog unavailable")
        return f"search_cloud_skills ERR: {err}"
    results = _search(query, entries, top_k=top_k)
    if not results:
        return f"search_cloud_skills: no matches for '{query}'"
    lines: list[str] = []
    for item in results:
        name = item.get("name", "")
        desc = item.get("description", "")
        lines.append(f"- {name}: {desc}")
    return "\n".join(lines)


# ===================================================================
# 8. read_skill
# ===================================================================

@mcp.tool
def read_skill(
    skill_name: Annotated[str, "Name of the skill to read"],
) -> str:
    """Read a skill's SKILL.md content."""
    from core.skill_engine.skill_resolver import openskills_read, _resolve_skill_dir
    try:
        raw = openskills_read(skill_name)
        local_dir = _resolve_skill_dir(skill_name)
        if local_dir is None:
            return raw

        base_dir = str(local_dir.resolve())
        # Expand the placeholder shown in many SKILL.md files so the agent
        # receives a concrete runnable path instead of guessing legacy paths.
        rendered = raw.replace("{baseDir}", base_dir)
        if rendered is raw:
            rendered = raw

        prefix = (
            f"[Local skill path]\n{base_dir}\n"
            f"[Tip]\nUse scripts from this path. For shell scripts, prefer "
            f"`bash {base_dir}/scripts/<script>.sh ...` if direct execution fails.\n\n"
        )
        return prefix + rendered
    except Exception as exc:
        return f"read_skill ERR: {exc}"


# ===================================================================
# 9. list_local_skills
# ===================================================================

@mcp.tool
def list_local_skills() -> str:
    """List all locally available skills with their descriptions."""
    from core.skill_engine.skill_resolver import _iter_skill_roots
    seen: set[str] = set()
    lines: list[str] = []
    for root in _iter_skill_roots():
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
                # Extract first description line from SKILL.md
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
