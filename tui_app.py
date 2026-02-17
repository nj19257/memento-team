"""Textual TUI for orchestrator runs and live worker trajectory inspection."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Footer, Header, Static, TextArea

from orchestrator.orchestrator_agent import OrchestratorAgent

ROOT = Path(__file__).resolve().parent
LOGS_DIR = ROOT / "logs"

# Ensure Memento-S imports resolve for workboard helpers.
sys.path.insert(0, str(ROOT / "Memento-S"))

from core.workboard import cleanup_board, get_board_path  # noqa: E402


class MementoTUI(App):
    """Minimal Textual UI for running tasks and inspecting worker logs."""

    CSS = """
    Screen {
        layout: vertical;
    }

    #layout {
        height: 1fr;
    }

    #left {
        width: 34;
        min-width: 30;
        padding: 1;
        border: round #666666;
    }

    #right {
        width: 1fr;
        padding: 1;
        border: round #666666;
    }

    #task_input {
        height: 9;
        margin-bottom: 1;
    }

    #run_task {
        margin-bottom: 1;
    }

    #status {
        height: 3;
        margin-bottom: 1;
        border: round #666666;
        padding: 0 1;
    }

    #workers_table {
        height: 1fr;
    }

    #steps_table {
        height: 10;
        margin-bottom: 1;
    }

    #workboard {
        height: 1fr;
        margin-bottom: 1;
    }

    #workboard_container {
        border: round #666666;
    }

    #final_output {
        height: 8;
        border: round #666666;
        padding: 0 1;
    }

    .section_title {
        margin: 0 0 1 0;
        text-style: bold;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh_workers", "Refresh Workers"),
        ("ctrl+enter", "run_task", "Run Task"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.orchestrator: OrchestratorAgent | None = None
        self._task_running: bool = False
        self._worker_files: list[Path] = []
        self._worker_row_key_to_path: dict[Any, Path] = {}
        self._selected_worker_path: Path | None = None
        self._selected_worker_mtime: float = -1.0
        self._workboard_last_text: str = ""
        self._session_id: str | None = None
        self._last_session_id: str | None = None
        self._session_started_at: float = 0.0
        self._session_file_baseline: set[str] = set()
        self._current_session_files: list[Path] = []
        self._session_board_path: Path | None = None
        self._last_task_running_state: bool = False
        self._last_session_file_signature: list[tuple[str, int]] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="layout"):
            with Vertical(id="left"):
                yield Static("Task", classes="section_title")
                yield TextArea("", id="task_input")
                yield Button("Run Task", id="run_task", variant="primary")
                yield Static("Status: initializing", id="status")
                yield Static("Workers (live)", classes="section_title")
                yield DataTable(id="workers_table")
            with Vertical(id="right"):
                yield Static("Execution Steps (selected worker)", classes="section_title")
                yield DataTable(id="steps_table")
                yield Static("Workboard", classes="section_title")
                with Vertical(id="workboard_container"):
                    yield TextArea("(no workboard exists)", id="workboard", read_only=True)
                yield Static("Final Output", classes="section_title")
                yield TextArea("", id="final_output", read_only=True)
        yield Footer()

    async def on_mount(self) -> None:
        workers_table = self.query_one("#workers_table", DataTable)
        workers_table.add_columns("Worker", "Status", "Timestamp", "Events", "Seconds", "Subtask")
        workers_table.cursor_type = "row"

        steps_table = self.query_one("#steps_table", DataTable)
        steps_table.add_columns("Time", "Event", "Details")
        steps_table.cursor_type = "row"

        self.set_interval(1.0, self._refresh_workers)
        self.set_interval(1.0, self._refresh_selected_worker_steps)
        self.set_interval(1.0, self._refresh_workboard)

        await self._start_orchestrator()
        self._refresh_workers()
        self._refresh_workboard()

    async def on_unmount(self) -> None:
        if self.orchestrator is not None:
            await self.orchestrator.close()

    async def _start_orchestrator(self) -> None:
        status = self.query_one("#status", Static)
        status.update("Status: starting orchestrator...")
        try:
            load_dotenv()
            model = ChatOpenAI(
                model=os.getenv("OPENROUTER_MODEL", "anthropic/claude-sonnet-4.5"),
                openai_api_key=os.getenv("OPENROUTER_API_KEY"),
                openai_api_base=os.getenv("OPENROUTER_BASE_URL"),
                temperature=0,
            )
            child_env = dict(os.environ)
            # Prevent MCP server stderr logs from corrupting Textual rendering.
            child_env["MCP_QUIET_STDERR"] = "1"
            child_env.setdefault("FASTMCP_LOG_LEVEL", "ERROR")
            child_env.setdefault("FASTMCP_QUIET", "1")
            self.orchestrator = OrchestratorAgent(model=model, env=child_env)
            await self.orchestrator.start()
            status.update("Status: ready | session: (none)")
        except Exception as exc:
            status.update(f"Status: failed to start orchestrator: {exc}")

    def action_refresh_workers(self) -> None:
        self._refresh_workers(force=True)

    def action_run_task(self) -> None:
        self._trigger_run_task()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "run_task":
            self._trigger_run_task()

    def _trigger_run_task(self) -> None:
        if self._task_running:
            return

        task_input = self.query_one("#task_input", TextArea)
        task = task_input.text.strip()
        if not task:
            self.query_one("#status", Static).update("Status: enter a task first")
            return

        asyncio.create_task(self._run_task(task))

    async def _run_task(self, task: str) -> None:
        self._task_running = True
        status = self.query_one("#status", Static)
        output = self.query_one("#final_output", TextArea)
        output.text = ""

        if self.orchestrator is None:
            status.update("Status: orchestrator not available")
            self._task_running = False
            return

        try:
            self._session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            self._session_started_at = time.time()
            self._prepare_new_session_board(self._session_id)
            self._session_file_baseline = {
                p.name
                for p in LOGS_DIR.glob("worker-*.jsonl")
            }
            self._current_session_files = []
            self._selected_worker_path = None
            self._selected_worker_mtime = -1.0
            self.query_one("#steps_table", DataTable).clear(columns=False)

            status.update(f"Status: running task... | session: {self._session_id}")
            result = await self.orchestrator.run(task)
            final = str(result.get("output", "")).strip()
            output.text = final or "(no output)"
            status.update(f"Status: done | session: {self._session_id}")
            self._last_session_id = self._session_id
        except Exception as exc:
            status.update(f"Status: run failed: {exc}")
            self._last_session_id = self._session_id
        finally:
            self._task_running = False

    def _refresh_workers(self, force: bool = False) -> None:
        all_files = sorted(
            LOGS_DIR.glob("worker-*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        self._worker_files = all_files

        if self._session_id is None:
            files: list[Path] = []
        else:
            files = [
                p
                for p in all_files
                if p.name not in self._session_file_baseline
                and p.stat().st_mtime >= (self._session_started_at - 1.0)
            ]
        file_signature = [
            (p.name, int(p.stat().st_mtime_ns))
            for p in files
        ]
        run_state_changed = self._last_task_running_state != self._task_running
        if (
            not force
            and not run_state_changed
            and file_signature == self._last_session_file_signature
        ):
            return
        self._current_session_files = files
        self._last_task_running_state = self._task_running
        self._last_session_file_signature = file_signature
        self._worker_row_key_to_path.clear()

        table = self.query_one("#workers_table", DataTable)
        table.clear(columns=False)

        now = time.time()
        for path in files:
            header = self._read_header(path)
            worker_label, timestamp = self._worker_and_ts_from_file(path)
            header_status = str(header.get("status", "")).strip().lower()
            if header_status in {"live", "finished", "failed"}:
                worker_status = header_status
            elif self._task_running and (now - path.stat().st_mtime) < 2.0:
                worker_status = "live"
            else:
                worker_status = "finished"
            events = header.get("total_events", "?")
            seconds = header.get("time_taken_seconds", "?")
            subtask = str(header.get("subtask", ""))
            subtask = self._short(subtask, 70)
            row_key = path.name
            added_key = table.add_row(
                worker_label,
                worker_status,
                timestamp,
                str(events),
                str(seconds),
                subtask,
                key=row_key,
            )
            self._worker_row_key_to_path[added_key] = path

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        table = event.data_table
        if table.id != "workers_table":
            return

        path = self._worker_row_key_to_path.get(event.row_key)
        if path is None:
            return

        self._selected_worker_path = path
        self._selected_worker_mtime = -1.0
        self._load_worker_steps(path)

    def _refresh_selected_worker_steps(self) -> None:
        if self._selected_worker_path is None or not self._selected_worker_path.exists():
            return
        mtime = self._selected_worker_path.stat().st_mtime
        if mtime <= self._selected_worker_mtime:
            return
        self._selected_worker_mtime = mtime
        self._load_worker_steps(self._selected_worker_path)

    def _load_worker_steps(self, path: Path) -> None:
        table = self.query_one("#steps_table", DataTable)
        table.clear(columns=False)

        for event in self._read_events(path):
            ts = self._event_time(event)
            name = str(event.get("event", ""))
            detail = self._event_detail(event)
            table.add_row(ts, name, detail)

    def _refresh_workboard(self) -> None:
        board_widget = self.query_one("#workboard", TextArea)
        if self._session_id is None and not self._task_running:
            text = "(no active task session yet)"
            if text != self._workboard_last_text:
                self._workboard_last_text = text
                board_widget.text = text
            return

        board_path = get_board_path()
        if not board_path.exists():
            text = "(no workboard exists)"
        else:
            try:
                text = board_path.read_text(encoding="utf-8")
                # Persist a session-specific live copy for later inspection.
                if self._session_board_path is not None:
                    self._session_board_path.write_text(text, encoding="utf-8")
            except Exception as exc:
                text = f"(failed to read workboard: {exc})"

        if text != self._workboard_last_text:
            self._workboard_last_text = text
            board_widget.load_text(text)

    def _prepare_new_session_board(self, new_session_id: str) -> None:
        """Archive previous board (if any) and start a fresh board for this session."""
        board_path = get_board_path()
        board_path.parent.mkdir(parents=True, exist_ok=True)

        session_board = board_path.parent / f".workboard-{new_session_id}.md"
        self._session_board_path = session_board

        if board_path.exists():
            archive_session = self._last_session_id or datetime.now(timezone.utc).strftime(
                "%Y%m%dT%H%M%SZ"
            )
            archived = board_path.parent / f".workboard-{archive_session}.md"
            i = 1
            while archived.exists():
                archived = board_path.parent / f".workboard-{archive_session}-{i}.md"
                i += 1
            try:
                board_path.rename(archived)
            except Exception:
                # Fallback: if rename fails, try explicit cleanup to avoid stale board reuse.
                cleanup_board()

        # Fresh board file for the new session (workers will overwrite with orchestrator content).
        initial = f"# Task Board ({new_session_id})\n\n## Subtasks\n\n## Shared Context\n\n## Results\n"
        board_path.write_text(initial, encoding="utf-8")
        session_board.write_text(initial, encoding="utf-8")

    @staticmethod
    def _worker_and_ts_from_file(path: Path) -> tuple[str, str]:
        # worker-3-20260217T143202Z.jsonl
        m = re.match(r"worker-(\d+)-([^.]+)\.jsonl$", path.name)
        if not m:
            return path.stem, "-"
        worker_idx = m.group(1)
        ts = m.group(2)
        return f"worker-{worker_idx}", ts

    @staticmethod
    def _read_header(path: Path) -> dict[str, Any]:
        try:
            with path.open("r", encoding="utf-8") as f:
                first = f.readline().strip()
            if not first:
                return {}
            data = json.loads(first)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _read_events(path: Path) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(record, dict) and record.get("type") != "header":
                        events.append(record)
        except Exception:
            return []
        return events

    @staticmethod
    def _event_time(event: dict[str, Any]) -> str:
        ts = str(event.get("ts", ""))
        if "T" in ts and len(ts) >= 19:
            return ts[11:19]
        return ts[:8]

    @classmethod
    def _event_detail(cls, event: dict[str, Any]) -> str:
        name = str(event.get("event", ""))
        if name == "run_one_skill_loop_start":
            return f"skill={event.get('skill_name', '?')} task={cls._short(str(event.get('user_text', '')), 80)}"
        if name == "run_one_skill_loop_round_plan":
            plan = event.get("plan", {})
            if isinstance(plan, dict):
                ops = plan.get("ops", [])
                if isinstance(ops, list):
                    op_types = [str(o.get("type", "?")) for o in ops if isinstance(o, dict)]
                    return f"round={event.get('round')} ops={op_types}"
                if plan.get("final"):
                    return cls._short(f"final={plan.get('final')}", 120)
            return cls._short(str(plan), 120)
        if name == "execute_skill_plan_output":
            return cls._short(str(event.get("result", "")), 140)
        if name == "run_one_skill_loop_end":
            return cls._short(f"mode={event.get('mode')} result={event.get('result', '')}", 140)
        if name == "route_skill_output":
            decision = event.get("decision", {})
            return cls._short(str(decision), 140)

        # Generic fallback: include compact JSON without very large fields.
        compact = {
            k: v
            for k, v in event.items()
            if k not in {"session_id", "messages", "result", "output", "user_text"}
        }
        return cls._short(json.dumps(compact, ensure_ascii=False), 140)

    @staticmethod
    def _short(text: str, max_len: int = 120) -> str:
        one_line = text.replace("\n", " ").strip()
        if len(one_line) <= max_len:
            return one_line
        return one_line[: max_len - 3] + "..."


def main() -> None:
    MementoTUI().run()


if __name__ == "__main__":
    main()
