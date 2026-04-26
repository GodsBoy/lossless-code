#!/usr/bin/env python3
"""Tests for scripts/contracts.py (v1.2 U6)."""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

TEST_DIR = tempfile.mkdtemp(prefix="lossless_extract_test_")
os.environ["LOSSLESS_HOME"] = TEST_DIR

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import db
import contracts as ce


class TestParsers(unittest.TestCase):
    """JSON parser tolerance: malformed input never raises."""

    def test_contracts_valid_json(self):
        response = json.dumps({
            "rules": [
                {"kind": "forbid", "body": "em-dashes"},
                {"kind": "prefer", "body": "kebab-case"},
            ]
        })
        out = ce._parse_contracts_json(response)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["kind"], "forbid")

    def test_contracts_malformed_returns_empty(self):
        self.assertEqual(ce._parse_contracts_json("not json"), [])
        self.assertEqual(ce._parse_contracts_json(""), [])
        self.assertEqual(ce._parse_contracts_json("{"), [])

    def test_contracts_strips_code_fence(self):
        wrapped = '```json\n{"rules":[{"kind":"prefer","body":"x"}]}\n```'
        out = ce._parse_contracts_json(wrapped)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["body"], "x")

    def test_contracts_filters_invalid_kind(self):
        response = json.dumps({
            "rules": [
                {"kind": "suggest", "body": "ignored"},
                {"kind": "forbid", "body": "kept"},
            ]
        })
        out = ce._parse_contracts_json(response)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["body"], "kept")

    def test_contracts_filters_empty_body(self):
        response = json.dumps({
            "rules": [
                {"kind": "forbid", "body": "   "},
                {"kind": "prefer", "body": "real rule"},
            ]
        })
        out = ce._parse_contracts_json(response)
        self.assertEqual(len(out), 1)

    def test_decisions_valid_json(self):
        response = json.dumps({
            "decisions": [
                {"summary": "use sqlite WAL mode", "session_id": "s1"},
            ]
        })
        out = ce._parse_decisions_json(response)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["session_id"], "s1")

    def test_decisions_malformed_returns_empty(self):
        self.assertEqual(ce._parse_decisions_json("garbage"), [])


class TestExtractiveFallback(unittest.TestCase):
    """Regex fallback runs when LLM is unavailable."""

    def test_contracts_fallback_finds_never_phrasing(self):
        msgs = [
            {"role": "user", "content": "Never use em-dashes in human text"},
            {"role": "user", "content": "Always prefer kebab-case for new modules"},
            {"role": "assistant", "content": "Sure thing"},
        ]
        out = ce._extractive_contracts_fallback(msgs)
        kinds = {(c["kind"], c["body"]) for c in out}
        self.assertTrue(any(k == "forbid" for k, _ in kinds))

    def test_contracts_fallback_skips_assistant_messages(self):
        msgs = [{"role": "assistant", "content": "Never claim things you cannot verify"}]
        out = ce._extractive_contracts_fallback(msgs)
        self.assertEqual(out, [])

    def test_decisions_fallback_finds_we_decided(self):
        msgs = [
            {"role": "user", "content": "We decided to ship the bundle in v1.2.0", "session_id": "s1"},
        ]
        out = ce._extractive_decisions_fallback(msgs)
        self.assertGreaterEqual(len(out), 1)


class TestExtractWithMockedLLM(unittest.TestCase):
    """End-to-end extractor flow with mocked call_llm."""

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
        conn = db.get_db()
        conn.execute("DELETE FROM contracts")
        conn.execute("DELETE FROM summaries")
        conn.commit()

    def test_extract_contracts_llm_path(self):
        """LLM returns valid JSON, mode='llm'."""
        msgs = [{"id": 1, "role": "user", "content": "test"}]
        config = {}
        llm_response = json.dumps({
            "rules": [{"kind": "forbid", "body": "test rule unique"}]
        })
        with patch("summarise.call_llm", return_value=llm_response):
            out, mode = ce.extract_contract_candidates(msgs, [], config)
        self.assertEqual(mode, "llm")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["body"], "test rule unique")

    def test_extract_contracts_falls_back_to_regex(self):
        """LLM returns empty, mode='extractive' if regex matches."""
        msgs = [{"id": 1, "role": "user", "content": "Never use foo bar baz"}]
        with patch("summarise.call_llm", return_value=""):
            out, mode = ce.extract_contract_candidates(msgs, [], {})
        self.assertEqual(mode, "extractive")
        self.assertGreater(len(out), 0)

    def test_extract_contracts_failed_when_both_empty(self):
        msgs = [{"id": 1, "role": "user", "content": "just chatting"}]
        with patch("summarise.call_llm", return_value=""):
            out, mode = ce.extract_contract_candidates(msgs, [], {})
        self.assertEqual(mode, "failed")
        self.assertEqual(out, [])

    def test_extract_contracts_llm_exception_falls_back(self):
        """LLM raises, mode='extractive' if regex matches, else 'failed'."""
        msgs = [{"id": 1, "role": "user", "content": "Always do the right thing here"}]
        with patch("summarise.call_llm", side_effect=ConnectionError("boom")):
            out, mode = ce.extract_contract_candidates(msgs, [], {})
        # ConnectionError -> regex fallback -> matches "always" pattern
        self.assertIn(mode, ("extractive", "failed"))

    def test_extract_contracts_respects_per_cycle_limit(self):
        """Returns at most contractsPerCycleLimit even if LLM emits more."""
        msgs = [{"id": 1, "role": "user", "content": "x"}]
        many_rules = [
            {"kind": "forbid", "body": f"unique rule number {i}"} for i in range(50)
        ]
        llm_response = json.dumps({"rules": many_rules})
        with patch("summarise.call_llm", return_value=llm_response):
            out, mode = ce.extract_contract_candidates(
                msgs, [], {"contractsPerCycleLimit": 5}
            )
        self.assertEqual(mode, "llm")
        self.assertEqual(len(out), 5)

    def test_extract_decisions_llm_path(self):
        msgs = [{"id": 1, "role": "user", "content": "x", "session_id": "s1"}]
        llm_response = json.dumps({
            "decisions": [
                {"summary": "use SQLite WAL", "session_id": "s1"},
            ]
        })
        with patch("summarise.call_llm", return_value=llm_response):
            out, mode = ce.extract_decision_candidates(msgs, [], {})
        self.assertEqual(mode, "llm")
        self.assertEqual(len(out), 1)


class TestConflictDetection(unittest.TestCase):
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
        conn = db.get_db()
        conn.execute("DELETE FROM contracts")
        conn.commit()

    def test_detects_opposing_kind_high_overlap(self):
        # Existing Active "FORBID em-dashes in human-facing text"
        cid = db.store_contract_candidate(
            kind="forbid", body="em-dashes in human-facing text"
        )
        db.approve_contract(cid)
        # New "PREFER em-dashes for technical emphasis" overlaps strongly
        result = ce._detect_conflicts(
            "em-dashes for human-facing text emphasis", "prefer"
        )
        self.assertEqual(result, cid)

    def test_no_conflict_for_same_kind(self):
        cid = db.store_contract_candidate(kind="forbid", body="rule alpha unique")
        db.approve_contract(cid)
        # Same kind cannot conflict.
        result = ce._detect_conflicts("rule alpha unique extended", "forbid")
        self.assertIsNone(result)

    def test_no_conflict_for_low_overlap(self):
        cid = db.store_contract_candidate(kind="forbid", body="something completely different")
        db.approve_contract(cid)
        result = ce._detect_conflicts("totally unrelated rule body", "prefer")
        self.assertIsNone(result)

    def test_verify_before_has_no_opposing_kind(self):
        cid = db.store_contract_candidate(kind="forbid", body="check first always before")
        db.approve_contract(cid)
        # verify-before has no opposing kind, so no conflict ever.
        result = ce._detect_conflicts(
            "check first always before doing", "verify-before"
        )
        self.assertIsNone(result)


class TestStoreExtracted(unittest.TestCase):
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
        conn = db.get_db()
        conn.execute("DELETE FROM contracts")
        conn.execute("DELETE FROM summaries")
        conn.commit()

    def test_store_contracts_counts_dedup(self):
        # First call: store one contract.
        ce.store_extracted_contracts(
            [{"kind": "forbid", "body": "unique alpha rule"}]
        )
        # Second call with same body: dedup blocks.
        stats = ce.store_extracted_contracts(
            [{"kind": "forbid", "body": "unique alpha rule"}]
        )
        self.assertEqual(stats["stored"], 0)
        self.assertEqual(stats["deduped"], 1)

    def test_store_contracts_counts_conflicts(self):
        # Existing Active rule.
        cid = db.store_contract_candidate(
            kind="forbid", body="rule beta one two three"
        )
        db.approve_contract(cid)
        # New Pending rule of opposing kind, high overlap.
        stats = ce.store_extracted_contracts(
            [{"kind": "prefer", "body": "rule beta one two three four"}]
        )
        self.assertEqual(stats["stored"], 1)
        self.assertEqual(stats["conflicts_detected"], 1)
        # Verify the conflicts_with column was populated.
        pending = db.list_contracts(status="Pending")
        self.assertEqual(pending[0]["conflicts_with"], cid)

    def test_store_decisions_persists_kind(self):
        n = ce.store_extracted_decisions([
            {"summary": "test decision A", "session_id": "s1"},
            {"summary": "test decision B", "session_id": "s1"},
        ])
        self.assertEqual(n, 2)
        # Verify via direct query that kind='decision' landed.
        conn = db.get_db()
        row = conn.execute(
            "SELECT kind FROM summaries WHERE content = 'test decision A'"
        ).fetchone()
        self.assertEqual(row["kind"], "decision")

    def test_store_skips_invalid_candidate_dicts(self):
        """Bad candidate shapes (missing kind / empty body) skip rather than crash."""
        stats = ce.store_extracted_contracts([
            {"kind": "forbid", "body": ""},      # empty body, skip
            {"kind": "", "body": "missing kind"},  # missing kind, skip
            {"kind": "forbid", "body": "ok valid rule"},  # ok
        ])
        self.assertEqual(stats["stored"], 1)


class TestCombineModes(unittest.TestCase):
    def test_same_mode_returns_same(self):
        self.assertEqual(ce.combine_modes("llm", "llm"), "llm")
        self.assertEqual(ce.combine_modes("failed", "failed"), "failed")

    def test_different_modes_return_mixed(self):
        self.assertEqual(ce.combine_modes("llm", "extractive"), "mixed")
        self.assertEqual(ce.combine_modes("llm", "failed"), "mixed")


if __name__ == "__main__":
    unittest.main()
