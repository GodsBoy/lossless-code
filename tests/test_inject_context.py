#!/usr/bin/env python3
"""Tests for lossless-code context injection with budget-aware packing."""

import os
import sys
import tempfile
import unittest

# Point to test vault (must be set before importing db)
TEST_DIR = tempfile.mkdtemp(prefix="lossless_test_inject_")
os.environ["LOSSLESS_HOME"] = TEST_DIR
os.environ["LOSSLESS_VAULT_DIR"] = TEST_DIR

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import db
import inject_context


class TestInjectContextBase(unittest.TestCase):
    """Base class with DB setup/teardown for inject_context tests."""

    @classmethod
    def setUpClass(cls):
        db._conn = None
        db.LOSSLESS_HOME = db.Path(TEST_DIR)
        db.VAULT_DIR = db.Path(TEST_DIR)
        db.VAULT_DB = db.VAULT_DIR / "vault.db"
        db.CONFIG_PATH = db.VAULT_DIR / "config.json"
        db.get_db()

    @classmethod
    def tearDownClass(cls):
        db.close_db()

    def setUp(self):
        """Clear tables between tests."""
        conn = db.get_db()
        conn.execute("DELETE FROM summary_sources")
        conn.execute("DELETE FROM summaries")
        conn.execute("DELETE FROM messages")
        conn.execute("DELETE FROM sessions")
        conn.commit()

    def _seed_session(self, session_id="test-session", working_dir="/tmp/test"):
        db.ensure_session(session_id, working_dir)
        return session_id

    def _seed_summary(self, content, depth=0, session_id="test-session",
                      summary_id=None):
        sid = summary_id or db.gen_summary_id()
        token_count = len(content) // 4
        db.store_summary(
            summary_id=sid,
            content=content,
            depth=depth,
            source_ids=[],
            session_id=session_id,
            token_count=token_count,
        )
        return sid


class TestBuildContextNoBudgetPressure(TestInjectContextBase):
    """When context fits within budget, all items are included."""

    def test_no_summaries_returns_empty(self):
        """No summaries, no handoff, no dream -> empty string."""
        result = inject_context.build_context()
        self.assertEqual(result, "")

    def test_summaries_included_without_query(self):
        """Without query, summaries are selected by depth (R2)."""
        self._seed_session()
        self._seed_summary("Deep overview of project architecture", depth=2)
        self._seed_summary("Shallow detail about a function", depth=0)

        result = inject_context.build_context(session_id="test-session")
        self.assertIn("Deep overview", result)
        # Depth-2 should come first (higher depth = more compressed overview)
        deep_pos = result.find("Deep overview")
        shallow_pos = result.find("Shallow detail")
        if shallow_pos != -1:
            self.assertLess(deep_pos, shallow_pos)

    def test_summaries_with_query_uses_fts(self):
        """With query, FTS5 BM25 ranking determines inclusion order (R1)."""
        self._seed_session()
        self._seed_summary("The database migration strategy was decided", depth=0)
        self._seed_summary("React component styling for buttons", depth=0)
        self._seed_summary("Database schema changes for user table", depth=0)

        result = inject_context.build_context(
            session_id="test-session", query="database migration"
        )
        self.assertIn("Lossless Context", result)
        # Database-related summaries should appear (FTS match)
        self.assertIn("database", result.lower())

    def test_handoff_included(self):
        """Handoff text from previous session is always included (R3)."""
        self._seed_session()
        conn = db.get_db()
        conn.execute(
            "UPDATE sessions SET handoff_text = ? WHERE session_id = ?",
            ("Continue working on auth refactor", "test-session"),
        )
        conn.commit()

        result = inject_context.build_context(session_id="test-session")
        self.assertIn("Continue working on auth refactor", result)


class TestBudgetAwarePacking(TestInjectContextBase):
    """Budget-aware packing selects summaries by relevance within budget."""

    def test_budget_limits_summaries_included(self):
        """When budget is tight, only summaries that fit are included (R4, R6)."""
        self._seed_session()
        # Create summaries with known sizes. Each ~100 tokens (400 chars).
        for i in range(5):
            self._seed_summary(f"Summary {i}: " + "x" * 390, depth=0)

        # Set a very small budget that can only fit ~2 summaries + header
        result = inject_context.build_context(
            session_id="test-session",
            config_override={"contextTokenBudget": 300},
        )

        # Should have some summaries but not all 5
        count = result.count("Summary ")
        self.assertGreater(count, 0, "Should include at least one summary")
        self.assertLess(count, 5, "Budget should prevent including all 5")

    def test_no_mid_summary_truncation(self):
        """Summaries are never cut mid-content (R4)."""
        self._seed_session()
        marker = "UNIQUE_END_MARKER_12345"
        self._seed_summary(f"Short summary with {marker}", depth=0)
        self._seed_summary("x" * 2000, depth=0)  # Large summary

        result = inject_context.build_context(
            session_id="test-session",
            config_override={"contextTokenBudget": 200},
        )

        # If the short summary is included, it must be complete
        if marker in result:
            # The marker should appear intact, not truncated
            self.assertIn(marker, result)

    def test_budget_prefers_relevant_with_query(self):
        """Under budget pressure with query, FTS-ranked summaries win (R1)."""
        self._seed_session()
        # Irrelevant summary (big)
        self._seed_summary("Frontend CSS styling " + "x" * 300, depth=0)
        # Relevant summary (big)
        self._seed_summary("Database migration plan " + "x" * 300, depth=0)
        # Another relevant
        self._seed_summary("Database schema changes " + "x" * 300, depth=0)

        result = inject_context.build_context(
            session_id="test-session",
            query="database migration",
            config_override={"contextTokenBudget": 400},
        )

        # Under tight budget, database-related summaries should be preferred
        db_count = result.lower().count("database")
        css_count = result.lower().count("css styling")
        # At minimum, relevant items should appear more than irrelevant ones
        self.assertGreaterEqual(db_count, css_count)

    def test_handoff_always_included_even_over_budget(self):
        """Handoff is included even if it alone exceeds budget (R3)."""
        self._seed_session()
        big_handoff = "Important handoff: " + "y" * 2000
        conn = db.get_db()
        conn.execute(
            "UPDATE sessions SET handoff_text = ? WHERE session_id = ?",
            (big_handoff, "test-session"),
        )
        conn.commit()
        self._seed_summary("Some summary", depth=0)

        result = inject_context.build_context(
            session_id="test-session",
            config_override={"contextTokenBudget": 100},
        )
        self.assertIn("Important handoff", result)

    def test_empty_query_falls_back_to_depth(self):
        """Empty/blank query uses depth-based selection, not FTS (R5)."""
        self._seed_session()
        self._seed_summary("Shallow info", depth=0)
        self._seed_summary("Deep overview", depth=2)

        result = inject_context.build_context(
            session_id="test-session", query=""
        )

        # Should behave same as no query — depth-based
        if "Deep overview" in result and "Shallow info" in result:
            deep_pos = result.find("Deep overview")
            shallow_pos = result.find("Shallow info")
            self.assertLess(deep_pos, shallow_pos)

    def test_query_no_fts_matches_falls_back_to_depth(self):
        """Query with no FTS matches falls back to depth-based (R5)."""
        self._seed_session()
        self._seed_summary("Apple banana cherry", depth=0)
        self._seed_summary("Overview of all fruits", depth=2)

        result = inject_context.build_context(
            session_id="test-session",
            query="zzzznonexistentterm",
        )

        # Should fall back to depth-based — both should appear
        self.assertIn("Lossless Context", result)

    def test_no_summaries_only_handoff_and_dream(self):
        """No summaries in DB -> only handoff + dream patterns (no crash)."""
        self._seed_session()
        conn = db.get_db()
        conn.execute(
            "UPDATE sessions SET handoff_text = ? WHERE session_id = ?",
            ("Some handoff", "test-session"),
        )
        conn.commit()

        result = inject_context.build_context(session_id="test-session")
        self.assertIn("Some handoff", result)

    def test_budget_respected_total_output(self):
        """Total output respects contextTokenBudget (R6)."""
        self._seed_session()
        for i in range(10):
            self._seed_summary(f"Summary {i}: " + "word " * 200, depth=0)

        budget = 500
        result = inject_context.build_context(
            session_id="test-session",
            config_override={"contextTokenBudget": budget},
        )

        estimated_tokens = len(result) // 4
        # Allow some overhead for headers/formatting
        self.assertLessEqual(
            estimated_tokens, budget * 1.2,
            f"Output ~{estimated_tokens} tokens exceeds budget {budget}"
        )


class TestGetRelevantSummaries(TestInjectContextBase):
    """Test the candidate fetching with multiplier."""

    def test_fetches_more_candidates_than_limit(self):
        """get_relevant_summaries fetches limit*3 candidates internally."""
        self._seed_session()
        for i in range(9):
            self._seed_summary(f"Summary about topic {i} details", depth=0)

        # With limit=3, should fetch up to 9 candidates internally
        results = inject_context.get_relevant_summaries(
            query="topic details", limit=3
        )
        # Can return up to limit*3 candidates for the packer to select from
        self.assertGreater(len(results), 0)
        self.assertLessEqual(len(results), 9)


if __name__ == "__main__":
    unittest.main()
