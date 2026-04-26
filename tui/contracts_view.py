#!/usr/bin/env python3
"""TUI modal screens for the v1.2 Contracts tab.

Extracted from lcc_tui.py to keep the main app under the project's
800-line file-size cap. Three modal screens live here:

- ContractDetailScreen: full-body view with byline + supersede chain.
- SupersedeBodyScreen: TextArea editor pre-filled with the old body.
- RetractionReasonPrompt: required-reason single-line prompt.

Plus the Contracts-tab filter cycle constants used by the main app.
"""

from datetime import datetime

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Label, Static
try:
    from textual.widgets import TextArea
    _HAS_TEXTAREA = True
except ImportError:  # textual < 0.30
    _HAS_TEXTAREA = False

import db


def _ts(epoch: int | None) -> str:
    """Format a unix epoch to a readable timestamp.

    Duplicated from lcc_tui to avoid a circular import. Kept tiny so
    the duplication is visible and the cost of fixing drift is low.
    """
    if not epoch:
        return "-"
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")


# Filter cycle for the Contracts tab (t key).
CONTRACT_FILTERS = ["Pending", "Active", "Retracted"]
CONTRACT_EMPTY_MESSAGES = {
    "Pending": "No pending contracts. Run `lcc dream --run` to populate from session content.",
    "Active": "No active contracts. Approve some Pending candidates first via the `a` key.",
    "Retracted": "No retracted contracts. Supersede or retract Active contracts to see them here.",
}


class ContractDetailScreen(ModalScreen[None]):
    """Shows full body, byline, status, and supersede chain for a contract.

    The DataTable preview truncates body text. The detail modal renders
    the full body so the user has the unredacted text in front of them
    before pressing 'a' to approve. This is the safe approval surface.
    """

    BINDINGS = [Binding("escape", "go_back", "Back")]

    DEFAULT_CSS = """
    ContractDetailScreen {
        align: center middle;
    }
    #contract-detail-container {
        width: 90%;
        height: 90%;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #contract-detail-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #contract-detail-scroll {
        height: 1fr;
    }
    .contract-meta {
        color: $text-muted;
    }
    .conflict-warning {
        color: $warning;
        text-style: bold;
    }
    """

    def __init__(self, contract_id: str) -> None:
        super().__init__()
        self.contract_id = contract_id

    def compose(self) -> ComposeResult:
        with Vertical(id="contract-detail-container"):
            yield Label(f"Contract: {self.contract_id}", id="contract-detail-title")
            yield VerticalScroll(id="contract-detail-scroll")

    def on_mount(self) -> None:
        scroll = self.query_one("#contract-detail-scroll", VerticalScroll)
        contract = db.get_contract(self.contract_id)
        if not contract:
            scroll.mount(Label("Contract not found."))
            return

        scroll.mount(Static(
            f"[bold]Kind:[/bold] {contract['kind']}    "
            f"[bold]Status:[/bold] {contract['status']}    "
            f"[bold]Created:[/bold] {_ts(contract.get('created_at'))}",
            classes="contract-meta",
        ))
        if contract.get("byline_session_id") or contract.get("byline_model"):
            scroll.mount(Static(
                f"[bold]Byline:[/bold] session={contract.get('byline_session_id') or '(none)'}, "
                f"model={contract.get('byline_model') or '(none)'}",
                classes="contract-meta",
            ))
        if contract.get("supersedes_id"):
            scroll.mount(Static(
                f"[bold]Supersedes:[/bold] {contract['supersedes_id']}",
                classes="contract-meta",
            ))
        if contract.get("conflicts_with"):
            scroll.mount(Static(
                f"Conflicts with active contract: {contract['conflicts_with']}",
                classes="conflict-warning",
            ))
        scroll.mount(Static(""))
        scroll.mount(Static("[bold]Body:[/bold]"))
        scroll.mount(Static(contract.get("body", "")))

    def action_go_back(self) -> None:
        self.dismiss(None)


class SupersedeBodyScreen(ModalScreen[str | None]):
    """Multi-line entry for a new contract body. Pre-fills the old body so
    the user edits in place rather than starting blank.

    Returns the new body string when the user presses Ctrl+S, or None
    when escaped. Caller passes the result to db.supersede_contract.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "submit", "Submit"),
    ]

    DEFAULT_CSS = """
    SupersedeBodyScreen {
        align: center middle;
    }
    #supersede-container {
        width: 90%;
        height: 80%;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #supersede-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #supersede-body-input, #supersede-body-textarea {
        height: 1fr;
    }
    """

    def __init__(self, contract_id: str, old_body: str) -> None:
        super().__init__()
        self.contract_id = contract_id
        self.old_body = old_body

    def compose(self) -> ComposeResult:
        with Vertical(id="supersede-container"):
            yield Label(
                f"Supersede {self.contract_id} (Ctrl+S to submit, Esc to cancel)",
                id="supersede-title",
            )
            if _HAS_TEXTAREA:
                yield TextArea(self.old_body, id="supersede-body-textarea")
            else:
                # Fallback: single-line input. Body cap discipline still
                # applies but multiline editing is not available.
                yield Input(value=self.old_body, id="supersede-body-input")

    def action_submit(self) -> None:
        if _HAS_TEXTAREA:
            widget = self.query_one("#supersede-body-textarea", TextArea)
            new_body = widget.text
        else:
            widget = self.query_one("#supersede-body-input", Input)
            new_body = widget.value
        new_body = (new_body or "").strip()
        if not new_body:
            self.dismiss(None)
            return
        self.dismiss(new_body)

    def action_cancel(self) -> None:
        self.dismiss(None)


class RetractionReasonPrompt(ModalScreen[str | None]):
    """Single-line prompt for a retraction reason. Required: empty input
    cancels rather than submits an empty reason (db.retract_contract
    raises on empty reason)."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    DEFAULT_CSS = """
    RetractionReasonPrompt {
        align: center middle;
    }
    #retract-container {
        width: 70%;
        height: 30%;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #retract-title {
        text-style: bold;
        margin-bottom: 1;
    }
    """

    def __init__(self, contract_id: str) -> None:
        super().__init__()
        self.contract_id = contract_id

    def compose(self) -> ComposeResult:
        with Vertical(id="retract-container"):
            yield Label(
                f"Retract {self.contract_id} (Enter to submit, Esc to cancel)",
                id="retract-title",
            )
            yield Input(placeholder="Reason (required)", id="retract-reason-input")

    @on(Input.Submitted, "#retract-reason-input")
    def submit(self, event: Input.Submitted) -> None:
        reason = (event.value or "").strip()
        self.dismiss(reason or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


__all__ = [
    "ContractDetailScreen",
    "SupersedeBodyScreen",
    "RetractionReasonPrompt",
    "CONTRACT_FILTERS",
    "CONTRACT_EMPTY_MESSAGES",
]
