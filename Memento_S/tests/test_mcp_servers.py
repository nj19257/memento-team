"""Tests for the unified FastMCP server (7 tools) and MCP client."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core.mcp_server import mcp, configure


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Path:
    """Create a temporary workspace for tests."""
    configure(base_dir=tmp_path)
    return tmp_path


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# -------------------------------------------------------------------
# bash_tool
# -------------------------------------------------------------------

class TestBashTool:
    def test_echo(self, tmp_workspace: Path) -> None:
        from core.mcp_server import bash_tool

        result = bash_tool.fn(command="echo hello", description="test echo")
        assert "hello" in result

    def test_empty_command(self, tmp_workspace: Path) -> None:
        from core.mcp_server import bash_tool

        result = bash_tool.fn(command="", description="empty")
        assert "ERR" in result

    def test_failure(self, tmp_workspace: Path) -> None:
        from core.mcp_server import bash_tool

        result = bash_tool.fn(command="false", description="fail")
        assert "ERR" in result

    def test_working_dir(self, tmp_workspace: Path) -> None:
        from core.mcp_server import bash_tool

        result = bash_tool.fn(command="pwd", description="check cwd")
        assert str(tmp_workspace) in result


# -------------------------------------------------------------------
# str_replace
# -------------------------------------------------------------------

class TestStrReplace:
    def test_basic_replace(self, tmp_workspace: Path) -> None:
        from core.mcp_server import str_replace

        f = tmp_workspace / "test.txt"
        f.write_text("foo bar baz", encoding="utf-8")
        result = str_replace.fn(description="replace bar", path=str(f), old_str="bar", new_str="qux")
        assert "OK" in result
        assert f.read_text() == "foo qux baz"

    def test_not_found(self, tmp_workspace: Path) -> None:
        from core.mcp_server import str_replace

        f = tmp_workspace / "test.txt"
        f.write_text("foo bar", encoding="utf-8")
        result = str_replace.fn(description="missing", path=str(f), old_str="NOPE", new_str="x")
        assert "ERR" in result
        assert "not found" in result

    def test_not_unique(self, tmp_workspace: Path) -> None:
        from core.mcp_server import str_replace

        f = tmp_workspace / "test.txt"
        f.write_text("foo foo", encoding="utf-8")
        result = str_replace.fn(description="not unique", path=str(f), old_str="foo", new_str="bar")
        assert "ERR" in result
        assert "2 times" in result

    def test_delete_string(self, tmp_workspace: Path) -> None:
        from core.mcp_server import str_replace

        f = tmp_workspace / "test.txt"
        f.write_text("hello world", encoding="utf-8")
        result = str_replace.fn(description="delete", path=str(f), old_str=" world")
        assert "OK" in result
        assert f.read_text() == "hello"

    def test_file_not_exists(self, tmp_workspace: Path) -> None:
        from core.mcp_server import str_replace

        result = str_replace.fn(description="missing file", path=str(tmp_workspace / "nope.txt"), old_str="a", new_str="b")
        assert "ERR" in result
        assert "not found" in result

    def test_relative_path(self, tmp_workspace: Path) -> None:
        from core.mcp_server import str_replace

        f = tmp_workspace / "rel.txt"
        f.write_text("alpha beta", encoding="utf-8")
        result = str_replace.fn(description="relative", path="rel.txt", old_str="alpha", new_str="gamma")
        assert "OK" in result
        assert f.read_text() == "gamma beta"


# -------------------------------------------------------------------
# file_create
# -------------------------------------------------------------------

class TestFileCreate:
    def test_create_file(self, tmp_workspace: Path) -> None:
        from core.mcp_server import file_create

        result = file_create.fn(description="create", path=str(tmp_workspace / "new.txt"), file_text="hello")
        assert "OK" in result
        assert (tmp_workspace / "new.txt").read_text() == "hello"

    def test_create_nested(self, tmp_workspace: Path) -> None:
        from core.mcp_server import file_create

        result = file_create.fn(
            description="nested create",
            path=str(tmp_workspace / "a" / "b" / "c.txt"),
            file_text="nested",
        )
        assert "OK" in result
        assert (tmp_workspace / "a" / "b" / "c.txt").read_text() == "nested"

    def test_overwrite(self, tmp_workspace: Path) -> None:
        from core.mcp_server import file_create

        f = tmp_workspace / "over.txt"
        f.write_text("old", encoding="utf-8")
        file_create.fn(description="overwrite", path=str(f), file_text="new")
        assert f.read_text() == "new"

    def test_relative_path(self, tmp_workspace: Path) -> None:
        from core.mcp_server import file_create

        result = file_create.fn(description="relative", path="relfile.txt", file_text="content")
        assert "OK" in result
        assert (tmp_workspace / "relfile.txt").read_text() == "content"


# -------------------------------------------------------------------
# view
# -------------------------------------------------------------------

class TestView:
    def test_view_text_file(self, tmp_workspace: Path) -> None:
        from core.mcp_server import view

        f = tmp_workspace / "hello.txt"
        f.write_text("line1\nline2\nline3", encoding="utf-8")
        result = view.fn(description="view text", path=str(f))
        assert "line1" in result
        assert "line2" in result
        assert "line3" in result
        # Check line numbers present
        assert "1\t" in result

    def test_view_range(self, tmp_workspace: Path) -> None:
        from core.mcp_server import view

        f = tmp_workspace / "lines.txt"
        f.write_text("a\nb\nc\nd\ne", encoding="utf-8")
        result = view.fn(description="view range", path=str(f), view_range=[2, 4])
        assert "b" in result
        assert "c" in result
        assert "d" in result
        lines = result.strip().splitlines()
        assert len(lines) == 3

    def test_view_range_to_end(self, tmp_workspace: Path) -> None:
        from core.mcp_server import view

        f = tmp_workspace / "lines.txt"
        f.write_text("a\nb\nc\nd\ne", encoding="utf-8")
        result = view.fn(description="view to end", path=str(f), view_range=[3, -1])
        lines = result.strip().splitlines()
        assert len(lines) == 3  # c, d, e

    def test_view_directory(self, tmp_workspace: Path) -> None:
        from core.mcp_server import view

        (tmp_workspace / "sub").mkdir()
        (tmp_workspace / "file.txt").write_text("x", encoding="utf-8")
        result = view.fn(description="view dir", path=str(tmp_workspace))
        assert "sub/" in result
        assert "file.txt" in result

    def test_view_directory_ignores_hidden(self, tmp_workspace: Path) -> None:
        from core.mcp_server import view

        (tmp_workspace / ".hidden").mkdir()
        (tmp_workspace / "visible.txt").write_text("x", encoding="utf-8")
        result = view.fn(description="view dir hidden", path=str(tmp_workspace))
        assert ".hidden" not in result
        assert "visible.txt" in result

    def test_view_not_found(self, tmp_workspace: Path) -> None:
        from core.mcp_server import view

        result = view.fn(description="missing", path=str(tmp_workspace / "nope.txt"))
        assert "ERR" in result
        assert "not found" in result

    def test_view_image(self, tmp_workspace: Path) -> None:
        from core.mcp_server import view

        f = tmp_workspace / "img.png"
        f.write_bytes(b"\x89PNG\r\n")
        result = view.fn(description="view image", path=str(f))
        assert "Image file" in result
        assert "bytes" in result


# -------------------------------------------------------------------
# MCP Client (integration)
# -------------------------------------------------------------------

class TestMCPClient:
    def test_start_and_list_tools(self, tmp_workspace: Path, event_loop) -> None:
        async def _test():
            from core.mcp_client import MCPToolManager

            mgr = MCPToolManager()
            await mgr.start(base_dir=tmp_workspace)
            try:
                tools = mgr.get_tool_definitions()
                assert len(tools) == 7
                tool_names = {t["function"]["name"] for t in tools}
                assert {"bash_tool", "str_replace", "file_create", "view"}.issubset(tool_names)
                assert {"search_cloud_skills", "read_skill", "list_local_skills"}.issubset(tool_names)
            finally:
                await mgr.shutdown()

        event_loop.run_until_complete(_test())

    def test_call_tool(self, tmp_workspace: Path, event_loop) -> None:
        async def _test():
            from core.mcp_client import MCPToolManager

            mgr = MCPToolManager()
            await mgr.start(base_dir=tmp_workspace)
            try:
                result = await mgr.call_tool(
                    "file_create",
                    {"description": "create", "path": str(tmp_workspace / "test.txt"), "file_text": "test content"},
                )
                assert "OK" in result

                result = await mgr.call_tool(
                    "view",
                    {"description": "view", "path": str(tmp_workspace / "test.txt")},
                )
                assert "test content" in result
            finally:
                await mgr.shutdown()

        event_loop.run_until_complete(_test())

    def test_langchain_tools(self, tmp_workspace: Path, event_loop) -> None:
        async def _test():
            from core.mcp_client import MCPToolManager

            mgr = MCPToolManager()
            await mgr.start(base_dir=tmp_workspace)
            try:
                lc_tools = mgr.get_langchain_tools()
                assert len(lc_tools) == 7
                names = {t.name for t in lc_tools}
                assert {"bash_tool", "str_replace", "file_create", "view"}.issubset(names)

                # Each tool should have a description
                for t in lc_tools:
                    assert t.description

                # Invoke a tool via LangChain interface
                bash = next(t for t in lc_tools if t.name == "bash_tool")
                result = await bash.ainvoke({"command": "echo langchain", "description": "test"})
                assert "langchain" in result
            finally:
                await mgr.shutdown()

        event_loop.run_until_complete(_test())

    def test_tool_definitions_format(self, tmp_workspace: Path, event_loop) -> None:
        async def _test():
            from core.mcp_client import MCPToolManager

            mgr = MCPToolManager()
            await mgr.start(base_dir=tmp_workspace)
            try:
                tools = mgr.get_tool_definitions()
                for tool in tools:
                    assert tool["type"] == "function"
                    assert "name" in tool["function"]
                    assert "description" in tool["function"]
                    assert "parameters" in tool["function"]
                    assert tool["function"]["parameters"].get("type") == "object"
            finally:
                await mgr.shutdown()

        event_loop.run_until_complete(_test())


# -------------------------------------------------------------------
# Workboard Server (direct tool calls)
# -------------------------------------------------------------------

class TestWorkboardServer:
    def test_read_empty(self, tmp_workspace: Path) -> None:
        from core.workboard_mcp import (
            read_board_sync, configure as wb_configure, cleanup_board_sync,
        )

        wb_configure(base_dir=tmp_workspace)
        cleanup_board_sync()  # ensure clean state
        result = read_board_sync()
        assert result == ""

    def test_write_and_read(self, tmp_workspace: Path) -> None:
        from core.workboard_mcp import (
            read_board_sync, write_board_sync, configure as wb_configure,
        )

        wb_configure(base_dir=tmp_workspace)
        content = "# Task Board\n## Subtasks\n- [ ] 1: Build API"
        write_board_sync(content)
        result = read_board_sync()
        assert "Task Board" in result
        assert "Build API" in result

    def test_cleanup(self, tmp_workspace: Path) -> None:
        from core.workboard_mcp import (
            read_board_sync, write_board_sync, cleanup_board_sync,
            configure as wb_configure,
        )

        wb_configure(base_dir=tmp_workspace)
        write_board_sync("some content")
        assert read_board_sync() != ""

        cleanup_board_sync()
        assert read_board_sync() == ""


# -------------------------------------------------------------------
# Multi-Server Client
# -------------------------------------------------------------------

class TestMultiServerClient:
    def test_discovers_all_tools(self, tmp_workspace: Path, event_loop) -> None:
        """MCPToolManager with workboard extra server discovers 12 tools (7 core + 5 workboard)."""
        async def _test():
            from core.mcp_client import MCPToolManager
            from core.workboard_mcp import mcp as wb_mcp, configure as wb_configure

            wb_configure(base_dir=tmp_workspace)

            mgr = MCPToolManager(extra_servers=[(wb_mcp, wb_configure)])
            await mgr.start(base_dir=tmp_workspace)
            try:
                tools = mgr.get_tool_definitions()
                assert len(tools) == 12  # 7 core + 5 workboard
                tool_names = {t["function"]["name"] for t in tools}
                # Core tools
                assert {"bash_tool", "str_replace", "file_create", "view"}.issubset(tool_names)
                # Workboard tools
                assert {"read_board", "write_board", "edit_board", "append_board", "cleanup_board"}.issubset(tool_names)
            finally:
                await mgr.shutdown()

        event_loop.run_until_complete(_test())

    def test_call_workboard_read(self, tmp_workspace: Path, event_loop) -> None:
        """Can call read_board via multi-server client."""
        async def _test():
            from core.mcp_client import MCPToolManager
            from core.workboard_mcp import (
                mcp as wb_mcp,
                configure as wb_configure,
                write_board_sync,
            )

            wb_configure(base_dir=tmp_workspace)
            write_board_sync("# Test Board\nHello from test")

            mgr = MCPToolManager(extra_servers=[(wb_mcp, wb_configure)])
            await mgr.start(base_dir=tmp_workspace)
            try:
                result = await mgr.call_tool("read_board", {})
                assert "Test Board" in result
                assert "Hello from test" in result
            finally:
                await mgr.shutdown()

        event_loop.run_until_complete(_test())


# -------------------------------------------------------------------
# Edit Approval Flow
# -------------------------------------------------------------------

class TestEditApprovalFlow:
    def test_edit_approved(self, tmp_workspace: Path, event_loop) -> None:
        """Approved edit updates the workboard and returns success."""
        async def _test():
            import json
            from core.workboard_mcp import (
                configure as wb_configure,
                write_board_sync, read_board_sync, submit_edit,
                get_pending_requests, resolve_request,
                set_worker_context,
            )

            wb_configure(base_dir=tmp_workspace)
            write_board_sync("# Board\n- [ ] Task A\n- [ ] Task B")
            set_worker_context(0)

            # Start edit in background (it will block on event)
            edit_task = asyncio.create_task(
                submit_edit(
                    old_text="- [ ] Task A",
                    new_text="- [x] Task A",
                    reason="Task A is done",
                )
            )

            # Give the edit a moment to register
            await asyncio.sleep(0.1)

            # Get pending requests
            pending = await get_pending_requests()
            assert len(pending) == 1
            req = pending[0]
            assert req.worker_idx == 0
            assert req.edit_type == "edit"
            assert req.reason == "Task A is done"

            # Approve
            await resolve_request(req, approved=True, feedback="")

            # Worker should unblock and get result
            result_str = await edit_task
            result = json.loads(result_str)
            assert result["status"] == "success"

            # Board should be updated
            board = read_board_sync()
            assert "- [x] Task A" in board
            assert "- [ ] Task B" in board

        event_loop.run_until_complete(_test())

    def test_edit_rejected(self, tmp_workspace: Path, event_loop) -> None:
        """Rejected edit does NOT change the workboard and returns failure."""
        async def _test():
            import json
            from core.workboard_mcp import (
                configure as wb_configure,
                write_board_sync, read_board_sync, submit_edit,
                get_pending_requests, resolve_request,
                set_worker_context,
            )

            wb_configure(base_dir=tmp_workspace)
            write_board_sync("# Board\n- [ ] Task A\n- [ ] Task B")
            set_worker_context(1)

            edit_task = asyncio.create_task(
                submit_edit(
                    old_text="- [ ] Task A\n- [ ] Task B",
                    new_text="",
                    reason="Remove all tasks",
                )
            )

            await asyncio.sleep(0.1)

            pending = await get_pending_requests()
            assert len(pending) == 1
            req = pending[0]

            # Reject
            await resolve_request(req, approved=False, feedback="Don't remove the header row")

            result_str = await edit_task
            result = json.loads(result_str)
            assert result["status"] == "failure"
            assert "header row" in result["feedback"]

            # Board should NOT be changed
            board = read_board_sync()
            assert "- [ ] Task A" in board
            assert "- [ ] Task B" in board

        event_loop.run_until_complete(_test())

    def test_multiple_queued(self, tmp_workspace: Path, event_loop) -> None:
        """Two workers submit edits, both get resolved."""
        async def _test():
            import json
            from core.workboard_mcp import (
                configure as wb_configure,
                write_board_sync, submit_edit, submit_append,
                get_pending_requests, resolve_request,
                set_worker_context, _current_worker_idx,
            )

            wb_configure(base_dir=tmp_workspace)
            write_board_sync("# Board\n## Results")

            # Worker 0 submits an edit
            _current_worker_idx.set(0)
            edit_task_0 = asyncio.create_task(
                submit_edit(
                    old_text="## Results",
                    new_text="## Results\n- Worker 0 done",
                    reason="Report result",
                )
            )

            # Worker 1 submits an append
            _current_worker_idx.set(1)
            edit_task_1 = asyncio.create_task(
                submit_append(
                    text="\n- Worker 1 note",
                    reason="Add note",
                )
            )

            await asyncio.sleep(0.1)

            # Both should be pending
            pending = await get_pending_requests()
            assert len(pending) == 2

            # Resolve both
            for req in pending:
                await resolve_request(req, approved=True, feedback="")

            # Both should complete
            r0 = json.loads(await edit_task_0)
            r1 = json.loads(await edit_task_1)
            assert r0["status"] == "success"
            assert r1["status"] == "success"

        event_loop.run_until_complete(_test())
