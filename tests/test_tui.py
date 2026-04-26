#!/usr/bin/env python3
"""Tests for lcc-tui — verifies the app can be instantiated without crashing."""

import os
import sys
import tempfile
import unittest

# Point to test vault
TEST_DIR = tempfile.mkdtemp(prefix="lossless_tui_test_")
os.environ["LOSSLESS_HOME"] = TEST_DIR
os.environ["LOSSLESS_VAULT_DIR"] = TEST_DIR

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tui"))

import db

# Reset db connection to use test dir
db._conn = None
db.VAULT_DIR = db.Path(TEST_DIR)
db.VAULT_DB = db.VAULT_DIR / "vault.db"
db.CONFIG_PATH = db.VAULT_DIR / "config.json"
db.get_db()


class TestTuiImport(unittest.TestCase):
    """Test that the TUI module can be imported and the app instantiated."""

    def test_import_lcc_tui(self):
        import lcc_tui
        self.assertTrue(hasattr(lcc_tui, "LccTui"))
        self.assertTrue(hasattr(lcc_tui, "main"))

    def test_app_instantiation(self):
        from lcc_tui import LccTui
        app = LccTui()
        self.assertIsNotNone(app)
        self.assertEqual(app.TITLE, "lcc-tui")

    def test_helper_ts(self):
        from lcc_tui import _ts
        self.assertEqual(_ts(None), "—")
        self.assertEqual(_ts(0), "—")
        result = _ts(1700000000)
        self.assertIn("2023", result)

    def test_helper_trunc(self):
        from lcc_tui import _trunc
        self.assertEqual(_trunc(""), "")
        self.assertEqual(_trunc("short"), "short")
        long_text = "a" * 200
        result = _trunc(long_text, 50)
        self.assertEqual(len(result), 50)
        self.assertTrue(result.endswith("…"))

    def test_helper_trunc_newlines(self):
        from lcc_tui import _trunc
        result = _trunc("line one\nline two\nline three")
        self.assertNotIn("\n", result)

    def test_search_modal_instantiation(self):
        from lcc_tui import SearchModal
        modal = SearchModal()
        self.assertIsNotNone(modal)

    def test_session_detail_instantiation(self):
        from lcc_tui import SessionDetailScreen
        screen = SessionDetailScreen("test-session-123")
        self.assertIsNotNone(screen)
        self.assertEqual(screen.session_id, "test-session-123")

    def test_summary_detail_instantiation(self):
        from lcc_tui import SummaryDetailScreen
        screen = SummaryDetailScreen("sum_abc123")
        self.assertIsNotNone(screen)
        self.assertEqual(screen.summary_id, "sum_abc123")

    # --- v1.2 U8: Contracts tab modals + filter cycling ---

    def test_contract_detail_instantiation(self):
        from lcc_tui import ContractDetailScreen
        screen = ContractDetailScreen("con_abc123")
        self.assertIsNotNone(screen)
        self.assertEqual(screen.contract_id, "con_abc123")

    def test_supersede_screen_instantiation(self):
        from lcc_tui import SupersedeBodyScreen
        screen = SupersedeBodyScreen("con_x", "old body text")
        self.assertEqual(screen.contract_id, "con_x")
        self.assertEqual(screen.old_body, "old body text")

    def test_retraction_prompt_instantiation(self):
        from lcc_tui import RetractionReasonPrompt
        prompt = RetractionReasonPrompt("con_y")
        self.assertEqual(prompt.contract_id, "con_y")

    def test_contract_filter_cycle_covers_all_statuses(self):
        from lcc_tui import _CONTRACT_FILTERS, _CONTRACT_EMPTY_MESSAGES
        self.assertEqual(_CONTRACT_FILTERS, ["Pending", "Active", "Retracted"])
        # Every filter has an empty-state placeholder message defined.
        for status in _CONTRACT_FILTERS:
            self.assertIn(status, _CONTRACT_EMPTY_MESSAGES)
            msg = _CONTRACT_EMPTY_MESSAGES[status]
            # Each message is non-trivial guidance, not a stub.
            self.assertGreater(len(msg), 20)

    def test_contracts_filter_default_is_pending(self):
        from lcc_tui import LccTui
        app = LccTui()
        self.assertEqual(app.contracts_filter, "Pending")

    def test_contracts_keybindings_present(self):
        """v1.2 U8 keybindings (a, r, s, t, 5) must be wired."""
        from lcc_tui import LccTui
        keys = {b.key for b in LccTui.BINDINGS}
        for required in ("a", "r", "s", "t", "5"):
            self.assertIn(required, keys, f"missing keybinding: {required}")


class TestTuiWithData(unittest.TestCase):
    """Test TUI components with actual data in the vault."""

    @classmethod
    def setUpClass(cls):
        db.ensure_session("tui-test-session", "/tmp/test")
        db.store_message("tui-test-session", "user", "Hello from TUI test")
        db.store_message("tui-test-session", "assistant", "Response from TUI test")
        db.store_summary(
            db.gen_summary_id(),
            "TUI test summary content",
            0,
            [],
            "tui-test-session",
        )

    def test_app_with_data(self):
        from lcc_tui import LccTui
        app = LccTui()
        self.assertIsNotNone(app)


if __name__ == "__main__":
    unittest.main()
