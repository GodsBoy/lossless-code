#!/usr/bin/env python3
"""Tests for scripts/db/contracts.py (v1.2 U5)."""

import os
import sys
import tempfile
import unittest

TEST_DIR = tempfile.mkdtemp(prefix="lossless_contracts_test_")
os.environ["LOSSLESS_HOME"] = TEST_DIR

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import db


class TestContracts(unittest.TestCase):
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
        # Each test gets a fresh contracts table to avoid dedup interference
        # across the body_hash unique-ish constraint.
        conn = db.get_db()
        conn.execute("DELETE FROM contracts")
        conn.commit()

    # --- store_contract_candidate ---

    def test_store_pending_contract(self):
        cid = db.store_contract_candidate(
            kind="forbid",
            body="em-dashes in human-facing text",
            byline_session_id="abc123",
            byline_model="claude-opus-4-7",
        )
        self.assertIsNotNone(cid)
        self.assertTrue(cid.startswith("con_"))
        row = db.get_contract(cid)
        self.assertEqual(row["status"], "Pending")
        self.assertEqual(row["kind"], "forbid")
        self.assertEqual(row["byline_session_id"], "abc123")

    def test_store_rejects_invalid_kind(self):
        with self.assertRaises(ValueError):
            db.store_contract_candidate(kind="suggest", body="...")

    def test_store_rejects_empty_body(self):
        with self.assertRaises(ValueError):
            db.store_contract_candidate(kind="prefer", body="")
        with self.assertRaises(ValueError):
            db.store_contract_candidate(kind="prefer", body="   ")

    def test_store_dedup_blocks_identical_body(self):
        """Same body, second insert returns None (Pending queue stays clean)."""
        first = db.store_contract_candidate(kind="forbid", body="no em-dashes")
        self.assertIsNotNone(first)
        second = db.store_contract_candidate(kind="forbid", body="no em-dashes")
        self.assertIsNone(second)
        # And case/whitespace variation should ALSO dedup (normalized hash).
        third = db.store_contract_candidate(kind="forbid", body="No  em-dashes")
        self.assertIsNone(third)

    def test_store_caps_oversized_body(self):
        big = "x " * 20_000  # ~40K chars, well over the 8K cap
        cid = db.store_contract_candidate(kind="prefer", body=big)
        row = db.get_contract(cid)
        self.assertIn("[Capped from", row["body"])
        # Stored size is bounded.
        self.assertLess(len(row["body"]), 12_000)

    # --- approve_contract / reject_contract ---

    def test_approve_pending_to_active(self):
        cid = db.store_contract_candidate(kind="prefer", body="kebab-case modules")
        self.assertTrue(db.approve_contract(cid))
        self.assertEqual(db.get_contract(cid)["status"], "Active")

    def test_approve_idempotent_only_for_pending(self):
        """Once Active, approve() returns False. Closes the silently-double-approve gap."""
        cid = db.store_contract_candidate(kind="prefer", body="x-prefix tests")
        db.approve_contract(cid)
        # Calling again on an Active row is a no-op.
        self.assertFalse(db.approve_contract(cid))

    def test_approve_unknown_returns_false(self):
        self.assertFalse(db.approve_contract("con_nonexistent"))

    def test_reject_pending(self):
        cid = db.store_contract_candidate(kind="forbid", body="bad rule")
        self.assertTrue(db.reject_contract(cid))
        self.assertEqual(db.get_contract(cid)["status"], "Rejected")

    def test_reject_active_is_noop(self):
        """Rejected is for Pending. Use retract on Active."""
        cid = db.store_contract_candidate(kind="forbid", body="active rule")
        db.approve_contract(cid)
        self.assertFalse(db.reject_contract(cid))

    # --- retract_contract ---

    def test_retract_active(self):
        cid = db.store_contract_candidate(kind="forbid", body="old rule")
        db.approve_contract(cid)
        ok = db.retract_contract(cid, reason="scope changed; rule no longer applies")
        self.assertTrue(ok)
        self.assertEqual(db.get_contract(cid)["status"], "Retracted")
        # A tombstone row points at the original.
        chain = db.list_contracts(status="Retracted")
        tombstones = [c for c in chain if c["supersedes_id"] == cid]
        self.assertEqual(len(tombstones), 1)
        self.assertIn("scope changed", tombstones[0]["body"])

    def test_retract_requires_reason(self):
        cid = db.store_contract_candidate(kind="forbid", body="x")
        db.approve_contract(cid)
        with self.assertRaises(ValueError):
            db.retract_contract(cid, reason="")

    def test_retract_unknown_returns_false(self):
        self.assertFalse(db.retract_contract("con_unknown", reason="-"))

    # --- supersede_contract (atomicity gate) ---

    def test_supersede_replaces_active_with_new_active(self):
        old_id = db.store_contract_candidate(kind="forbid", body="forbid em-dashes broadly")
        db.approve_contract(old_id)
        new_id = db.supersede_contract(
            old_id,
            new_body="forbid em-dashes in human-facing text only",
            byline_session_id="xyz",
            byline_model="claude-opus-4-7",
        )
        self.assertIsNotNone(new_id)
        self.assertEqual(db.get_contract(old_id)["status"], "Retracted")
        self.assertEqual(db.get_contract(new_id)["status"], "Active")
        self.assertEqual(db.get_contract(new_id)["supersedes_id"], old_id)

    def test_supersede_preserves_old_body_for_audit(self):
        old_id = db.store_contract_candidate(kind="prefer", body="ORIGINAL CONTENT")
        db.approve_contract(old_id)
        db.supersede_contract(old_id, new_body="REVISED CONTENT")
        # Original row's body must be preserved unchanged (audit trail).
        self.assertEqual(db.get_contract(old_id)["body"], "ORIGINAL CONTENT")

    def test_supersede_atomicity_no_partial_state(self):
        """Force a PRIMARY KEY violation inside the supersede transaction
        and verify no partial state survives. If BEGIN IMMEDIATE / COMMIT
        is wrapping both INSERT and UPDATE, the rollback runs cleanly and
        the original row stays Active.
        """
        old_id = db.store_contract_candidate(kind="forbid", body="rule A original")
        db.approve_contract(old_id)
        # Pre-create a contract whose id we will collide against. The
        # supersede flow will attempt to INSERT with this same id, hit a
        # PRIMARY KEY violation mid-transaction, and must roll back cleanly.
        collision_id = db.store_contract_candidate(
            kind="forbid", body="rule C separate"
        )

        # Patch gen_contract_id to return the collision id so the INSERT
        # inside supersede_contract fails on PRIMARY KEY constraint. This
        # is the most realistic failure point because gen_contract_id is
        # called inside the function under test.
        from unittest.mock import patch
        import db.contracts as contracts_module

        with patch.object(contracts_module, "gen_contract_id", return_value=collision_id):
            with self.assertRaises(Exception):
                db.supersede_contract(old_id, new_body="rule A revised")

        # After the exception, the original row must still be Active and
        # no rival Active row should reference old_id.
        self.assertEqual(db.get_contract(old_id)["status"], "Active")
        rivals = [
            c for c in db.list_contracts(status="Active")
            if c["supersedes_id"] == old_id
        ]
        self.assertEqual(
            rivals, [],
            f"Found rival Active rows referencing {old_id} after rollback: {rivals}",
        )

    def test_supersede_unknown_returns_none(self):
        self.assertIsNone(db.supersede_contract("con_unknown", new_body="x"))

    def test_supersede_blocks_duplicate_body(self):
        """Re-introducing a body that was previously stored is blocked,
        even via supersede (closes the 'rejected rule sneaks back as new id' gap)."""
        a_id = db.store_contract_candidate(kind="forbid", body="rule A original")
        db.approve_contract(a_id)
        b_id = db.store_contract_candidate(kind="forbid", body="rule B different")
        # body of b is new; supersede a with b's body should be blocked.
        result = db.supersede_contract(a_id, new_body="rule B different")
        self.assertIsNone(result)
        # And original a is still Active (rollback worked).
        self.assertEqual(db.get_contract(a_id)["status"], "Active")

    # --- list_contracts ---

    def test_list_filtered_by_status(self):
        a = db.store_contract_candidate(kind="prefer", body="rule a unique 1")
        b = db.store_contract_candidate(kind="prefer", body="rule b unique 2")
        c = db.store_contract_candidate(kind="prefer", body="rule c unique 3")
        db.approve_contract(b)
        pending = db.list_contracts(status="Pending")
        active = db.list_contracts(status="Active")
        self.assertEqual({r["id"] for r in pending}, {a, c})
        self.assertEqual({r["id"] for r in active}, {b})

    def test_list_invalid_status_raises(self):
        with self.assertRaises(ValueError):
            db.list_contracts(status="bogus")

    # --- __all__ surface ---

    def test_contracts_in_db_all(self):
        for name in (
            "store_contract_candidate",
            "approve_contract",
            "supersede_contract",
            "list_contracts",
        ):
            self.assertIn(name, db.__all__)


if __name__ == "__main__":
    unittest.main()
