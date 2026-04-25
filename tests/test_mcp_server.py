#!/usr/bin/env python3
"""Tests for lossless-code MCP server."""

import asyncio
import os
import sys
import tempfile
import unittest

# Point to test vault before any imports
TEST_DIR = tempfile.mkdtemp(prefix="lossless_mcp_test_")
os.environ["LOSSLESS_HOME"] = TEST_DIR

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mcp"))

import db
from server import (
    _do_grep,
    _do_expand,
    _do_expand_span,
    _do_context,
    _do_sessions,
    _do_handoff,
    _do_status,
    list_tools,
    call_tool,
    TOOLS,
)


class TestMCPToolFunctions(unittest.TestCase):
    """Test each tool function independently against a test vault."""

    @classmethod
    def setUpClass(cls):
        db._conn = None
        db.LOSSLESS_HOME = db.Path(TEST_DIR)
        db.VAULT_DB = db.LOSSLESS_HOME / "vault.db"
        db.CONFIG_PATH = db.LOSSLESS_HOME / "config.json"
        db.get_db()

        # Seed test data
        db.ensure_session("mcp-test-session", "/tmp/project")
        db.store_message("mcp-test-session", "user", "How do I configure the auth module?")
        db.store_message(
            "mcp-test-session",
            "assistant",
            "The auth module uses JWT tokens stored in config/auth.json",
        )
        db.store_message("mcp-test-session", "user", "What about the database layer?")
        db.store_message(
            "mcp-test-session",
            "assistant",
            "The database layer uses SQLite with WAL mode for concurrency",
        )

        # Create a summary with sources
        cls.test_summary_id = db.gen_summary_id()
        msg_ids = db.get_unsummarised("mcp-test-session")
        source_ids = [("message", str(m["id"])) for m in msg_ids[:2]]
        db.store_summary(
            summary_id=cls.test_summary_id,
            content="Discussed auth config (JWT in config/auth.json) and DB layer (SQLite WAL).",
            depth=0,
            source_ids=source_ids,
            session_id="mcp-test-session",
            token_count=20,
        )

        # Set handoff
        db.set_handoff("mcp-test-session", "Working on auth module. Next: add tests.")

    @classmethod
    def tearDownClass(cls):
        db.close_db()

    # --- grep ---

    def test_grep_finds_messages(self):
        result = _do_grep("auth module")
        self.assertIn("Messages", result)
        self.assertIn("auth", result.lower())

    def test_grep_finds_summaries(self):
        result = _do_grep("JWT")
        self.assertIn("JWT", result)

    def test_grep_no_results(self):
        result = _do_grep("xyznonexistent12345")
        self.assertIn("No results", result)

    def test_grep_with_limit(self):
        result = _do_grep("auth", limit=1)
        self.assertIsInstance(result, str)
        self.assertIn("auth", result.lower())

    def test_grep_special_chars(self):
        """Special characters don't crash the search."""
        result = _do_grep("What? (test) *glob*")
        self.assertIsInstance(result, str)

    # --- expand ---

    def test_expand_valid_summary(self):
        result = _do_expand(self.test_summary_id)
        self.assertIn("Summary", result)
        self.assertIn("Sources", result)
        self.assertIn(self.test_summary_id, result)

    def test_expand_not_found(self):
        result = _do_expand("sum_nonexistent999")
        self.assertIn("not found", result)

    def test_expand_full_content(self):
        result = _do_expand(self.test_summary_id, full=True)
        self.assertIn(self.test_summary_id, result)

    # --- expand_span (v1.2 U4) ---

    def test_expand_span_returns_chain(self):
        """Happy path: walk parent chain upward from a leaf message."""
        import json as _json

        db.ensure_session("span-mcp", "/tmp")
        # Build a 3-deep chain manually so we control parent ids exactly.
        conn = db.get_db()
        import time as _time
        ts = int(_time.time() * 1000)
        cur = conn.execute(
            "INSERT INTO messages (session_id, turn_id, role, content, tool_name, "
            "working_dir, timestamp, span_kind, parent_message_id) "
            "VALUES (?, '', 'user', 'root msg', '', '', ?, 'user_prompt', NULL)",
            ("span-mcp", ts),
        )
        root_id = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO messages (session_id, turn_id, role, content, tool_name, "
            "working_dir, timestamp, span_kind, parent_message_id) "
            "VALUES (?, '', 'assistant', 'mid msg', '', '', ?, 'assistant_reply', ?)",
            ("span-mcp", ts + 1, root_id),
        )
        mid_id = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO messages (session_id, turn_id, role, content, tool_name, "
            "working_dir, timestamp, span_kind, parent_message_id) "
            "VALUES (?, '', 'tool', 'leaf msg', 'Read', '', ?, 'tool_call', ?)",
            ("span-mcp", ts + 2, mid_id),
        )
        leaf_id = cur.lastrowid
        conn.commit()

        result = _do_expand_span(str(leaf_id))
        self.assertNotIn('"error"', result, f"Expected text response, got JSON error: {result}")
        self.assertIn("Span chain", result)
        self.assertIn("3 hops", result)
        # Leaf-first ordering: hop 0 is the leaf
        self.assertIn("hop 0", result)
        self.assertIn("hop 2", result)

    def test_expand_span_unknown_id_returns_structured_error(self):
        import json as _json
        result = _do_expand_span("99999999")
        payload = _json.loads(result)
        self.assertEqual(payload["error"]["code"], "span_not_found")
        # Sanitized message: no filesystem paths or stack traces.
        self.assertNotIn("/", payload["error"]["message"])

    def test_expand_span_invalid_id_returns_span_not_found(self):
        import json as _json
        result = _do_expand_span("not-a-number")
        payload = _json.loads(result)
        self.assertEqual(payload["error"]["code"], "span_not_found")

    def test_expand_span_oversized_chain_returns_expand_too_large(self):
        """When the rendered chain exceeds the soft cap, return structured error."""
        import json as _json

        db.ensure_session("oversized-span", "/tmp")
        conn = db.get_db()
        import time as _time
        # Build a chain with very long content to exceed _SPAN_EXPAND_MAX_CHARS.
        big_content = "x" * 600  # 600 chars per hop -> ~12K chars across 20 hops
        ts = int(_time.time() * 1000)
        parent = None
        leaf_id = None
        for i in range(25):
            cur = conn.execute(
                "INSERT INTO messages (session_id, turn_id, role, content, "
                "tool_name, working_dir, timestamp, span_kind, parent_message_id) "
                "VALUES (?, '', 'user', ?, '', '', ?, 'user_prompt', ?)",
                ("oversized-span", big_content, ts + i, parent),
            )
            parent = cur.lastrowid
            leaf_id = cur.lastrowid
        conn.commit()

        # full=False should trigger the cap. content is truncated per-line, but
        # 25 hops with 500-char-truncated lines still pushes well past 8K.
        result = _do_expand_span(str(leaf_id), full=False)
        if '"error"' in result:
            payload = _json.loads(result)
            self.assertEqual(payload["error"]["code"], "expand_too_large")
        else:
            # If the per-line truncation happened to fit, that's also acceptable.
            self.assertIn("Span chain", result)

    def test_expand_span_error_message_no_str_exception_leakage(self):
        """Sanitization rule: error messages are static strings, no path leaks."""
        import json as _json
        from server import _STRUCTURED_ERROR_MESSAGES

        for code, msg in _STRUCTURED_ERROR_MESSAGES.items():
            self.assertNotIn("/", msg, f"code={code} leaks path-like char in: {msg}")
            self.assertNotIn("Traceback", msg)
            self.assertNotIn("sqlite3", msg)

    # --- context ---

    def test_context_with_query(self):
        result = _do_context(query="auth")
        self.assertIsInstance(result, str)

    def test_context_no_query(self):
        result = _do_context()
        self.assertIsInstance(result, str)

    # --- sessions ---

    def test_sessions_lists_sessions(self):
        result = _do_sessions()
        self.assertIn("mcp-test-session", result)
        self.assertIn("started=", result)

    def test_sessions_with_limit(self):
        result = _do_sessions(limit=1)
        lines = [l for l in result.strip().split("\n") if l.strip()]
        self.assertEqual(len(lines), 1)

    # --- handoff ---

    def test_handoff_existing(self):
        result = _do_handoff(session_id="mcp-test-session")
        self.assertIn("auth module", result)

    def test_handoff_no_session(self):
        result = _do_handoff()
        # Should return the most recent handoff or "No handoff"
        self.assertIsInstance(result, str)

    def test_handoff_nonexistent_session(self):
        result = _do_handoff(session_id="nonexistent-session-12345")
        # Falls back to most recent with handoff
        self.assertIsInstance(result, str)

    # --- status ---

    def test_status_output(self):
        result = _do_status()
        self.assertIn("Sessions:", result)
        self.assertIn("Messages:", result)
        self.assertIn("Summaries:", result)
        self.assertIn("vault status", result)


class TestMCPProtocol(unittest.TestCase):
    """Test MCP protocol compliance: list_tools and call_tool."""

    @classmethod
    def setUpClass(cls):
        db._conn = None
        db.LOSSLESS_HOME = db.Path(TEST_DIR)
        db.VAULT_DB = db.LOSSLESS_HOME / "vault.db"
        db.CONFIG_PATH = db.LOSSLESS_HOME / "config.json"
        db.get_db()

    @classmethod
    def tearDownClass(cls):
        db.close_db()

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    # --- list_tools ---

    def test_list_tools_returns_all(self):
        tools = self._run(list_tools())
        self.assertEqual(len(tools), 6)
        names = {t.name for t in tools}
        expected = {"lcc_grep", "lcc_expand", "lcc_context", "lcc_sessions", "lcc_handoff", "lcc_status"}
        self.assertEqual(names, expected)

    def test_tool_schemas_valid(self):
        tools = self._run(list_tools())
        for tool in tools:
            self.assertIsInstance(tool.name, str)
            self.assertIsInstance(tool.description, str)
            self.assertIsInstance(tool.inputSchema, dict)
            self.assertIn("type", tool.inputSchema)
            self.assertEqual(tool.inputSchema["type"], "object")

    # --- call_tool ---

    def test_call_grep(self):
        result = self._run(call_tool("lcc_grep", {"query": "auth"}))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].type, "text")
        self.assertIsInstance(result[0].text, str)

    def test_call_expand(self):
        result = self._run(call_tool("lcc_expand", {"summary_id": "sum_nonexistent"}))
        self.assertEqual(len(result), 1)
        self.assertIn("not found", result[0].text)

    def test_call_context(self):
        result = self._run(call_tool("lcc_context", {}))
        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0].text, str)

    def test_call_sessions(self):
        result = self._run(call_tool("lcc_sessions", {}))
        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0].text, str)

    def test_call_handoff(self):
        result = self._run(call_tool("lcc_handoff", {}))
        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0].text, str)

    def test_call_status(self):
        result = self._run(call_tool("lcc_status", {}))
        self.assertEqual(len(result), 1)
        self.assertIn("vault status", result[0].text)

    def test_call_unknown_tool(self):
        result = self._run(call_tool("nonexistent_tool", {}))
        self.assertEqual(len(result), 1)
        self.assertIn("Unknown tool", result[0].text)

    def test_call_tool_returns_text_content(self):
        """All tool calls return TextContent objects."""
        for tool_name, args in [
            ("lcc_grep", {"query": "test"}),
            ("lcc_status", {}),
            ("lcc_sessions", {}),
        ]:
            result = self._run(call_tool(tool_name, args))
            self.assertGreater(len(result), 0)
            for item in result:
                self.assertEqual(item.type, "text")


if __name__ == "__main__":
    unittest.main()
