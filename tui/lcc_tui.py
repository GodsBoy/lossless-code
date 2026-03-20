#!/usr/bin/env python3
"""lcc-tui — Terminal UI for lossless-code vault.db."""

import os
import sys
from datetime import datetime
from pathlib import Path

# Ensure scripts dir is importable
_scripts_dir = os.environ.get("LOSSLESS_HOME", str(Path.home() / ".lossless-code"))
sys.path.insert(0, os.path.join(_scripts_dir, "scripts"))
# Also support running from the repo directly
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Static,
    TabbedContent,
    TabPane,
)

import db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts(epoch: int | None) -> str:
    """Format a unix epoch to a readable timestamp."""
    if not epoch:
        return "—"
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")


def _trunc(text: str, maxlen: int = 120) -> str:
    """Truncate text for display."""
    if not text:
        return ""
    line = text.replace("\n", " ").strip()
    if len(line) > maxlen:
        return line[: maxlen - 1] + "…"
    return line


def _file_size(path: Path) -> str:
    """Human-readable file size."""
    try:
        size = path.stat().st_size
    except OSError:
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


# ---------------------------------------------------------------------------
# Search modal
# ---------------------------------------------------------------------------

class SearchModal(ModalScreen[str]):
    """Floating search dialog triggered by /."""

    BINDINGS = [Binding("escape", "cancel", "Close")]

    DEFAULT_CSS = """
    SearchModal {
        align: center middle;
    }
    #search-dialog {
        width: 60;
        height: auto;
        max-height: 80%;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #search-input {
        margin-bottom: 1;
    }
    #search-results {
        max-height: 20;
        overflow-y: auto;
    }
    .search-result {
        padding: 0 1;
        margin-bottom: 1;
    }
    .search-result-session {
        color: $accent;
    }
    .search-result-content {
        color: $text-muted;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="search-dialog"):
            yield Label("Search vault", id="search-title")
            yield Input(placeholder="Type to search messages…", id="search-input")
            yield VerticalScroll(id="search-results")

    def on_mount(self) -> None:
        self.query_one("#search-input", Input).focus()

    @on(Input.Submitted, "#search-input")
    def run_search(self, event: Input.Submitted) -> None:
        query = event.value.strip()
        if not query:
            return
        container = self.query_one("#search-results", VerticalScroll)
        container.remove_children()

        results = db.search_messages(query, limit=30)
        if not results:
            container.mount(Label("No results found."))
            return

        for r in results:
            sid = r.get("session_id", "?")
            content = _trunc(r.get("content", ""), 80)
            ts = _ts(r.get("timestamp"))
            block = Static(
                f"[bold]{sid}[/bold]  {ts}\n{content}",
                classes="search-result",
            )
            block.tooltip = r.get("session_id", "")
            container.mount(block)

    def action_cancel(self) -> None:
        self.dismiss("")


# ---------------------------------------------------------------------------
# Session detail screen
# ---------------------------------------------------------------------------

class SessionDetailScreen(ModalScreen[None]):
    """Shows messages for a selected session."""

    BINDINGS = [Binding("escape", "go_back", "Back")]

    DEFAULT_CSS = """
    SessionDetailScreen {
        align: center middle;
    }
    #detail-container {
        width: 90%;
        height: 90%;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #detail-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #detail-scroll {
        height: 1fr;
    }
    .msg-user {
        color: #5dade2;
        padding: 0 1;
        margin-bottom: 1;
    }
    .msg-assistant {
        color: #58d68d;
        padding: 0 1 0 4;
        margin-bottom: 1;
    }
    .msg-tool {
        color: #f0b27a;
        padding: 0 1 0 4;
        margin-bottom: 1;
    }
    .msg-meta {
        color: $text-muted;
        text-style: italic;
    }
    """

    def __init__(self, session_id: str) -> None:
        super().__init__()
        self.session_id = session_id

    def compose(self) -> ComposeResult:
        with Vertical(id="detail-container"):
            yield Label(f"Session: {self.session_id}", id="detail-title")
            yield VerticalScroll(id="detail-scroll")

    def on_mount(self) -> None:
        conn = db.get_db()
        rows = conn.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY timestamp",
            (self.session_id,),
        ).fetchall()
        messages = [dict(r) for r in rows]

        scroll = self.query_one("#detail-scroll", VerticalScroll)
        if not messages:
            scroll.mount(Label("No messages in this session."))
            return

        for m in messages:
            role = m.get("role", "unknown")
            content = m.get("content", "")
            ts = _ts(m.get("timestamp"))
            tool = m.get("tool_name", "")

            if role == "user":
                cls = "msg-user"
                prefix = "USER"
            elif role == "assistant":
                cls = "msg-assistant"
                prefix = "ASSISTANT"
            else:
                cls = "msg-tool"
                prefix = role.upper()

            header = f"[bold]{prefix}[/bold]  {ts}"
            if tool:
                header += f"  (tool: {tool})"

            # Limit display length for very long messages
            display_content = content if len(content) <= 2000 else content[:2000] + "\n… (truncated)"
            scroll.mount(Static(header, classes="msg-meta"))
            scroll.mount(Static(display_content, classes=cls))

    def action_go_back(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Summary detail screen
# ---------------------------------------------------------------------------

class SummaryDetailScreen(ModalScreen[None]):
    """Shows full content and sources for a summary."""

    BINDINGS = [Binding("escape", "go_back", "Back")]

    DEFAULT_CSS = """
    SummaryDetailScreen {
        align: center middle;
    }
    #summary-detail-container {
        width: 90%;
        height: 90%;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #summary-detail-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #summary-detail-scroll {
        height: 1fr;
    }
    .source-item {
        color: $text-muted;
        padding: 0 1;
    }
    """

    def __init__(self, summary_id: str) -> None:
        super().__init__()
        self.summary_id = summary_id

    def compose(self) -> ComposeResult:
        with Vertical(id="summary-detail-container"):
            yield Label(f"Summary: {self.summary_id}", id="summary-detail-title")
            yield VerticalScroll(id="summary-detail-scroll")

    def on_mount(self) -> None:
        scroll = self.query_one("#summary-detail-scroll", VerticalScroll)
        summary = db.get_summary(self.summary_id)
        if not summary:
            scroll.mount(Label("Summary not found."))
            return

        scroll.mount(Static(
            f"[bold]Depth:[/bold] {summary['depth']}  "
            f"[bold]Created:[/bold] {_ts(summary.get('created_at'))}  "
            f"[bold]Tokens:[/bold] {summary.get('token_count') or '—'}"
        ))
        scroll.mount(Static(""))
        scroll.mount(Static(summary.get("content", "")))
        scroll.mount(Static(""))

        sources = db.get_summary_sources(self.summary_id)
        if sources:
            scroll.mount(Static(f"[bold]Sources ({len(sources)}):[/bold]"))
            for s in sources:
                scroll.mount(Static(
                    f"  {s['source_type']}: {s['source_id']}",
                    classes="source-item",
                ))

    def action_go_back(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------

class LccTui(App):
    """lcc-tui — Terminal UI for lossless-code."""

    TITLE = "lcc-tui"
    SUB_TITLE = "lossless-code vault browser"

    CSS = """
    Screen {
        background: $surface;
    }
    #sessions-table, #summaries-table {
        height: 1fr;
    }
    .stat-label {
        text-style: bold;
        color: $accent;
        width: 100%;
        padding: 1 2;
    }
    .stat-value {
        padding: 0 2 1 4;
    }
    .stat-row {
        height: auto;
        padding: 0;
    }
    #stats-container {
        padding: 1;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("slash", "search", "Search"),
        Binding("1", "tab_sessions", "Sessions", show=False),
        Binding("2", "tab_search", "Search", show=False),
        Binding("3", "tab_summaries", "Summaries", show=False),
        Binding("4", "tab_stats", "Stats", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="sessions"):
            with TabPane("Sessions", id="sessions"):
                yield DataTable(id="sessions-table")
            with TabPane("Search", id="search"):
                yield Input(placeholder="Search messages and summaries…", id="inline-search-input")
                yield VerticalScroll(id="inline-search-results")
            with TabPane("Summaries", id="summaries"):
                yield DataTable(id="summaries-table")
            with TabPane("Stats", id="stats"):
                yield VerticalScroll(id="stats-container")
        yield Footer()

    def on_mount(self) -> None:
        self._load_sessions()
        self._load_summaries()
        self._load_stats()

    # ── Data loading ──────────────────────────────────────────────────

    def _load_sessions(self) -> None:
        table = self.query_one("#sessions-table", DataTable)
        table.cursor_type = "row"
        table.add_columns("Session ID", "Working Dir", "Started", "Last Active", "Messages")
        sessions = db.list_sessions(limit=100)
        for s in sessions:
            sid = s["session_id"]
            # Get message count
            msg_count = db.count_session_messages(sid)
            table.add_row(
                _trunc(sid, 40),
                _trunc(s.get("working_dir", ""), 40),
                _ts(s.get("started_at")),
                _ts(s.get("last_active")),
                str(msg_count),
                key=sid,
            )

    def _load_summaries(self) -> None:
        table = self.query_one("#summaries-table", DataTable)
        table.cursor_type = "row"
        table.add_columns("ID", "Session", "Depth", "Created", "Preview")
        conn = db.get_db()
        rows = conn.execute(
            "SELECT * FROM summaries ORDER BY depth DESC, created_at DESC LIMIT 200"
        ).fetchall()
        for r in rows:
            row = dict(r)
            table.add_row(
                _trunc(row["id"], 24),
                _trunc(row.get("session_id") or "—", 24),
                str(row["depth"]),
                _ts(row.get("created_at")),
                _trunc(row.get("content", ""), 60),
                key=row["id"],
            )

    def _load_stats(self) -> None:
        container = self.query_one("#stats-container", VerticalScroll)
        conn = db.get_db()

        session_count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        msg_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        summary_count = conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0]
        max_depth_row = conn.execute("SELECT MAX(depth) FROM summaries").fetchone()
        max_depth = max_depth_row[0] if max_depth_row[0] is not None else 0

        oldest = conn.execute("SELECT MIN(started_at) FROM sessions").fetchone()[0]
        newest = conn.execute("SELECT MAX(last_active) FROM sessions").fetchone()[0]

        vault_size = _file_size(db.VAULT_DB)

        stats = [
            ("Total Sessions", str(session_count)),
            ("Total Messages", str(msg_count)),
            ("Total Summaries", str(summary_count)),
            ("Max DAG Depth", str(max_depth)),
            ("Vault Size", vault_size),
            ("Oldest Session", _ts(oldest)),
            ("Newest Activity", _ts(newest)),
        ]

        for label, value in stats:
            container.mount(Static(f"[bold]{label}[/bold]", classes="stat-label"))
            container.mount(Static(value, classes="stat-value"))

    # ── Actions ───────────────────────────────────────────────────────

    def action_search(self) -> None:
        self.push_screen(SearchModal())

    def action_tab_sessions(self) -> None:
        self.query_one(TabbedContent).active = "sessions"

    def action_tab_search(self) -> None:
        self.query_one(TabbedContent).active = "search"
        self.query_one("#inline-search-input", Input).focus()

    def action_tab_summaries(self) -> None:
        self.query_one(TabbedContent).active = "summaries"

    def action_tab_stats(self) -> None:
        self.query_one(TabbedContent).active = "stats"

    # ── Row selection ─────────────────────────────────────────────────

    @on(DataTable.RowSelected, "#sessions-table")
    def session_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key and event.row_key.value:
            self.push_screen(SessionDetailScreen(event.row_key.value))

    @on(DataTable.RowSelected, "#summaries-table")
    def summary_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key and event.row_key.value:
            self.push_screen(SummaryDetailScreen(event.row_key.value))

    # ── Inline search ─────────────────────────────────────────────────

    @on(Input.Submitted, "#inline-search-input")
    def inline_search(self, event: Input.Submitted) -> None:
        query = event.value.strip()
        container = self.query_one("#inline-search-results", VerticalScroll)
        container.remove_children()

        if not query:
            return

        results = db.search_all(query, limit=30)
        msg_results = results.get("messages", [])
        sum_results = results.get("summaries", [])

        if not msg_results and not sum_results:
            container.mount(Label("No results found."))
            return

        if msg_results:
            container.mount(Static(f"[bold]Messages ({len(msg_results)}):[/bold]"))
            for r in msg_results:
                sid = r.get("session_id", "?")
                content = _trunc(r.get("content", ""), 80)
                ts = _ts(r.get("timestamp"))
                container.mount(Static(f"  [bold]{sid}[/bold]  {ts}\n  {content}"))

        if sum_results:
            container.mount(Static(""))
            container.mount(Static(f"[bold]Summaries ({len(sum_results)}):[/bold]"))
            for r in sum_results:
                sid = r.get("id", "?")
                content = _trunc(r.get("content", ""), 80)
                depth = r.get("depth", 0)
                container.mount(Static(f"  [bold]{sid}[/bold]  depth={depth}\n  {content}"))


def main() -> None:
    app = LccTui()
    app.run()


if __name__ == "__main__":
    main()
