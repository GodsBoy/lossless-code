#!/usr/bin/env python3
"""Tests for scripts/db/spans.py (v1.2 U2)."""

import json
import os
import sys
import tempfile
import unittest

TEST_DIR = tempfile.mkdtemp(prefix="lossless_spans_test_")
os.environ["LOSSLESS_HOME"] = TEST_DIR

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import db


class TestSpanHelpers(unittest.TestCase):
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

    def setUp(self):
        # Each test gets a fresh session to avoid cross-test interference
        # on parent_message_id chains.
        conn = db.get_db()
        conn.execute("DELETE FROM messages")
        conn.commit()

    def _store_with_span(
        self,
        session_id: str,
        role: str,
        content: str,
        parent_id=None,
        span_kind=None,
        tool_call_id=None,
        attributes=None,
    ) -> int:
        """Insert a row directly so tests can control parent linkage exactly."""
        import time
        conn = db.get_db()
        attrs_json = (
            db.cap_attributes_json(attributes) if attributes is not None else None
        )
        cur = conn.execute(
            """INSERT INTO messages
               (session_id, turn_id, role, content, tool_name, working_dir,
                timestamp, parent_message_id, span_kind, tool_call_id, attributes)
               VALUES (?, NULL, ?, ?, NULL, NULL, ?, ?, ?, ?, ?)""",
            (
                session_id,
                role,
                content,
                int(time.time() * 1000),
                parent_id,
                span_kind,
                tool_call_id,
                attrs_json,
            ),
        )
        conn.commit()
        return cur.lastrowid

    # --- get_span ---

    def test_get_span_existing_row(self):
        db.ensure_session("span-1", "/tmp")
        msg_id = self._store_with_span("span-1", "user", "hello", span_kind="user_prompt")
        span = db.get_span(msg_id)
        self.assertIsNotNone(span)
        self.assertEqual(span["span_kind"], "user_prompt")
        self.assertIsNone(span["attributes"])

    def test_get_span_nonexistent_returns_none(self):
        self.assertIsNone(db.get_span(999999))

    def test_get_span_decodes_attributes_json(self):
        db.ensure_session("span-attr", "/tmp")
        msg_id = self._store_with_span(
            "span-attr",
            "tool",
            "{}",
            span_kind="tool_call",
            attributes={"tool_name": "Read", "latency_ms": 42},
        )
        span = db.get_span(msg_id)
        self.assertEqual(span["attributes"], {"tool_name": "Read", "latency_ms": 42})

    def test_get_span_handles_invalid_json_in_storage(self):
        # Simulate a corrupted attributes blob landing in the column.
        db.ensure_session("span-bad", "/tmp")
        msg_id = self._store_with_span("span-bad", "user", "x")
        conn = db.get_db()
        conn.execute(
            "UPDATE messages SET attributes = ? WHERE id = ?",
            ("not-valid-json", msg_id),
        )
        conn.commit()
        span = db.get_span(msg_id)
        self.assertEqual(span["attributes"], {"_invalid_json": "not-valid-json"})

    # --- get_span_chain ---

    def test_chain_returns_5_in_order(self):
        """5-deep chain: leaf-first ordering."""
        db.ensure_session("chain-5", "/tmp")
        ids = []
        parent = None
        for i in range(5):
            mid = self._store_with_span(
                "chain-5", "user", f"msg-{i}", parent_id=parent, span_kind="user_prompt"
            )
            ids.append(mid)
            parent = mid
        # Walk from the leaf (last inserted) upward
        chain = db.get_span_chain(ids[-1])
        self.assertEqual(len(chain), 5)
        # Leaf-first ordering: hop 0 is the leaf itself.
        self.assertEqual(chain[0]["id"], ids[-1])
        self.assertEqual(chain[-1]["id"], ids[0])

    def test_chain_orphan_returns_single_element(self):
        db.ensure_session("orphan", "/tmp")
        mid = self._store_with_span("orphan", "user", "alone")
        chain = db.get_span_chain(mid)
        self.assertEqual(len(chain), 1)
        self.assertEqual(chain[0]["id"], mid)

    def test_chain_unknown_message_returns_empty(self):
        self.assertEqual(db.get_span_chain(99999), [])

    def test_chain_respects_max_hops_cap(self):
        """If a cycle ever lands in the table, the cap prevents runaway."""
        db.ensure_session("cycle-test", "/tmp")
        a = self._store_with_span("cycle-test", "user", "a")
        b = self._store_with_span("cycle-test", "user", "b", parent_id=a)
        # Manually create a cycle a -> b -> a via UPDATE (FK is logical, not enforced).
        conn = db.get_db()
        conn.execute(
            "UPDATE messages SET parent_message_id = ? WHERE id = ?", (b, a)
        )
        conn.commit()
        # max_hops=4 should bound the result and not infinite-loop.
        chain = db.get_span_chain(b, max_hops=4)
        self.assertLessEqual(len(chain), 5)

    # --- get_children_spans ---

    def test_children_returns_direct_descendants_only(self):
        db.ensure_session("children", "/tmp")
        root = self._store_with_span("children", "user", "root")
        c1 = self._store_with_span("children", "user", "c1", parent_id=root)
        c2 = self._store_with_span("children", "user", "c2", parent_id=root)
        # grandchild: must NOT appear in get_children_spans(root)
        self._store_with_span("children", "user", "gc", parent_id=c1)
        kids = db.get_children_spans(root)
        kid_ids = [r["id"] for r in kids]
        self.assertEqual(sorted(kid_ids), sorted([c1, c2]))

    def test_children_filtered_by_span_kind(self):
        db.ensure_session("kind-filter", "/tmp")
        root = self._store_with_span("kind-filter", "user", "root")
        self._store_with_span("kind-filter", "tool", "tc", parent_id=root, span_kind="tool_call")
        self._store_with_span("kind-filter", "tool", "tr", parent_id=root, span_kind="tool_result")
        only_calls = db.get_children_spans(root, span_kind="tool_call")
        self.assertEqual(len(only_calls), 1)
        self.assertEqual(only_calls[0]["span_kind"], "tool_call")

    # --- cap_attributes_json ---

    def test_cap_normal_dict_passes_through(self):
        out = db.cap_attributes_json({"tool_name": "Read", "latency_ms": 12})
        # Validate it round-trips
        self.assertEqual(json.loads(out), {"tool_name": "Read", "latency_ms": 12})

    def test_cap_none_input_returns_empty_object(self):
        self.assertEqual(db.cap_attributes_json(None), "{}")

    def test_cap_non_dict_input_returns_empty_object(self):
        # Per U3 contract: shape validation rejects non-dict at the write site,
        # but cap_attributes_json defends in depth.
        self.assertEqual(db.cap_attributes_json([1, 2, 3]), "{}")
        self.assertEqual(db.cap_attributes_json("string"), "{}")

    def test_cap_oversized_dict_replaced_with_tombstone(self):
        # 100KB input vs default 500-token cap (~2000 chars)
        big = {"x": "a" * 100_000}
        out = db.cap_attributes_json(big, max_tokens=500)
        decoded = json.loads(out)
        self.assertTrue(decoded.get("_capped"))
        self.assertGreater(decoded.get("_original_chars", 0), 2000)
        # The tombstone itself must be small.
        self.assertLess(len(out), 200)


if __name__ == "__main__":
    unittest.main()
