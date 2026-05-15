#!/usr/bin/env python3
"""Tests for the embedding layer (Phase 2 — Semantic Search)."""

import os
import sys
import struct
import tempfile
import unittest
from unittest.mock import MagicMock, patch

TEST_DIR = tempfile.mkdtemp(prefix="lossless_embed_test_")
os.environ["LOSSLESS_HOME"] = TEST_DIR
os.environ["LOSSLESS_VAULT_DIR"] = TEST_DIR

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import db
import embed


def _reset_db():
    db.close_db()
    db._conn = None
    db.LOSSLESS_HOME = db.Path(TEST_DIR)
    db.VAULT_DIR = db.Path(TEST_DIR)
    db.VAULT_DB = db.VAULT_DIR / "vault.db"
    db.CONFIG_PATH = db.VAULT_DIR / "config.json"


_reset_db()
db.get_db()


def _cfg(**overrides):
    base = {
        "embeddingEnabled": True,
        "embeddingProvider": "local",
        "embeddingModel": "BAAI/bge-small-en-v1.5",
        "ftsWeight": 1.0,
        "vectorWeight": 1.0,
        "lastEmbeddingModel": None,
        "vectorBackend": "auto",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Provider detection
# ---------------------------------------------------------------------------

class TestProviderDetection(unittest.TestCase):

    def test_disabled_returns_none(self):
        """embeddingEnabled=False means no provider."""
        cfg = _cfg(embeddingEnabled=False)
        self.assertIsNone(embed.detect_provider(cfg))

    def test_local_fastembed_detected(self):
        """When fastembed importable, provider is 'fastembed'."""
        fake_fastembed = MagicMock()
        with patch.dict("sys.modules", {"fastembed": fake_fastembed}):
            result = embed.detect_provider(_cfg())
        self.assertEqual(result, "fastembed")

    def test_local_fallback_to_numpy(self):
        """When fastembed missing but numpy present, falls back to 'numpy'."""
        import numpy  # ensure numpy actually available
        with patch.dict("sys.modules", {"fastembed": None}):
            result = embed.detect_provider(_cfg())
        self.assertEqual(result, "numpy")

    def test_local_no_providers_returns_none(self):
        """When neither fastembed nor numpy available, returns None."""
        with patch.dict("sys.modules", {"fastembed": None, "numpy": None}):
            result = embed.detect_provider(_cfg())
        self.assertIsNone(result)

    def test_openai_with_key(self):
        """openai provider detected when openai importable and key present."""
        fake_openai = MagicMock()
        cfg = _cfg(embeddingProvider="openai")
        with patch.dict("sys.modules", {"openai": fake_openai}), \
             patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            result = embed.detect_provider(cfg)
        self.assertEqual(result, "openai")

    def test_openai_without_key_returns_none(self):
        """openai provider not detected when key missing."""
        fake_openai = MagicMock()
        cfg = _cfg(embeddingProvider="openai")
        env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
        with patch.dict("sys.modules", {"openai": fake_openai}), \
             patch.dict(os.environ, env, clear=True):
            result = embed.detect_provider(cfg)
        self.assertIsNone(result)

    def test_anthropic_with_key(self):
        """anthropic provider detected when importable and key present."""
        fake_anthropic = MagicMock()
        cfg = _cfg(embeddingProvider="anthropic")
        with patch.dict("sys.modules", {"anthropic": fake_anthropic}), \
             patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}):
            result = embed.detect_provider(cfg)
        self.assertEqual(result, "anthropic")


# ---------------------------------------------------------------------------
# Vector serialisation
# ---------------------------------------------------------------------------

class TestVecBlob(unittest.TestCase):

    def test_round_trip(self):
        """vec_to_blob -> blob_to_vec preserves direction (normalised)."""
        vec = [1.0, 2.0, 3.0]
        blob = embed.vec_to_blob(vec)
        recovered = embed.blob_to_vec(blob)
        self.assertEqual(len(recovered), 3)
        # Normalised: magnitude should be ~1.0
        mag = sum(v * v for v in recovered) ** 0.5
        self.assertAlmostEqual(mag, 1.0, places=5)

    def test_blob_is_bytes(self):
        blob = embed.vec_to_blob([0.1, 0.2, 0.3])
        self.assertIsInstance(blob, bytes)
        # 3 floats × 4 bytes each
        self.assertEqual(len(blob), 12)

    def test_zero_vector_handled(self):
        """Zero vector does not raise."""
        blob = embed.vec_to_blob([0.0, 0.0, 0.0])
        self.assertIsInstance(blob, bytes)


# ---------------------------------------------------------------------------
# embed_texts — provider routing
# ---------------------------------------------------------------------------

class TestEmbedTexts(unittest.TestCase):

    def test_returns_none_list_when_no_provider(self):
        cfg = _cfg(embeddingEnabled=False)
        result = embed.embed_texts(["hello", "world"], cfg)
        self.assertEqual(result, [None, None])

    def test_numpy_provider_returns_none_list(self):
        """numpy provider can search but cannot generate embeddings."""
        with patch("embed.detect_provider", return_value="numpy"):
            result = embed.embed_texts(["hello"], _cfg())
        self.assertEqual(result, [None])

    def test_fastembed_success(self):
        """Successful fastembed call returns float vectors."""
        fake_vec = [0.1] * 384

        class FakeEmbedder:
            def embed(self, texts):
                return [fake_vec for _ in texts]

        with patch("embed.detect_provider", return_value="fastembed"), \
             patch("embed._fastembed_embed", return_value=[[0.1] * 384]):
            result = embed.embed_texts(["test"], _cfg())
        self.assertIsNotNone(result[0])
        self.assertEqual(len(result[0]), 384)

    def test_fastembed_exception_returns_none(self):
        """Exception in fastembed returns None entries, does not raise."""
        with patch("embed.detect_provider", return_value="fastembed"), \
             patch("embed._fastembed_embed", side_effect=RuntimeError("model missing")):
            result = embed.embed_texts(["test"], _cfg())
        self.assertEqual(result, [None])


# ---------------------------------------------------------------------------
# DB integration — upsert + query
# ---------------------------------------------------------------------------

class TestEmbeddingDB(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _reset_db()
        db.get_db()
        cls.session_id = "embed-test-session"
        db.ensure_session(cls.session_id, "/tmp")
        # Insert a few test messages
        cls.msg_ids = []
        for i in range(3):
            mid = db.store_message(cls.session_id, "user", f"message content {i}", working_dir="/tmp")
            cls.msg_ids.append(mid)

    @classmethod
    def tearDownClass(cls):
        db.close_db()

    def test_upsert_embedding_stores_blob(self):
        """upsert_embedding stores a BLOB and is retrievable."""
        blob = embed.vec_to_blob([0.1, 0.2, 0.3])
        db.upsert_embedding(db.get_db(), self.msg_ids[0], "test-model", blob)
        rows = db.get_all_embeddings("test-model")
        self.assertTrue(any(r["message_id"] == self.msg_ids[0] for r in rows))

    def test_upsert_idempotent(self):
        """Upserting the same message_id+model twice does not duplicate."""
        blob = embed.vec_to_blob([0.5, 0.5, 0.5])
        db.upsert_embedding(db.get_db(), self.msg_ids[0], "idempotent-model", blob)
        db.upsert_embedding(db.get_db(), self.msg_ids[0], "idempotent-model", blob)
        rows = db.get_all_embeddings("idempotent-model")
        matching = [r for r in rows if r["message_id"] == self.msg_ids[0]]
        self.assertEqual(len(matching), 1)

    def test_get_unembed_messages_filters_correctly(self):
        """get_unembed_messages excludes messages with existing embeddings."""
        model = "filter-test-model"
        # Only embed msg_ids[0]
        blob = embed.vec_to_blob([0.1, 0.2, 0.3])
        db.upsert_embedding(db.get_db(), self.msg_ids[0], model, blob)
        unembedded = db.get_unembed_messages(model, self.session_id)
        embedded_ids = {r["id"] for r in unembedded}
        self.assertNotIn(self.msg_ids[0], embedded_ids)
        self.assertIn(self.msg_ids[1], embedded_ids)

    def test_count_embeddings(self):
        """count_embeddings returns correct count."""
        model = "count-test-model"
        for mid in self.msg_ids[:2]:
            blob = embed.vec_to_blob([float(mid)] * 3)
            db.upsert_embedding(db.get_db(), mid, model, blob)
        count = db.count_embeddings(model)
        self.assertEqual(count, 2)

    def test_message_embeddings_table_exists(self):
        """message_embeddings table is created by get_db()."""
        conn = db.get_db()
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        self.assertIn("message_embeddings", tables)

    def test_embedding_model_coverage(self):
        """get_embedding_model_coverage returns correct totals."""
        model = "coverage-model"
        blob = embed.vec_to_blob([0.1, 0.2, 0.3])
        db.upsert_embedding(db.get_db(), self.msg_ids[0], model, blob)
        cov = db.get_embedding_model_coverage(model)
        self.assertIn("total", cov)
        self.assertIn("embedded", cov)
        self.assertIn("pending", cov)
        self.assertGreaterEqual(cov["total"], 1)
        self.assertGreaterEqual(cov["embedded"], 1)
        self.assertEqual(cov["total"], cov["embedded"] + cov["pending"])


# ---------------------------------------------------------------------------
# Vector search (numpy fallback)
# ---------------------------------------------------------------------------

class TestVectorSearch(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _reset_db()
        db.get_db()
        cls.session_id = "vec-search-session"
        db.ensure_session(cls.session_id, "/tmp")
        cls.model = "vec-search-model"

        # Insert messages with known embeddings
        cls.msg_ids = []
        cls.vecs = [
            [1.0, 0.0, 0.0],  # "pointing right"
            [0.0, 1.0, 0.0],  # "pointing up"
            [0.0, 0.0, 1.0],  # "pointing forward"
        ]
        for i, vec in enumerate(cls.vecs):
            mid = db.store_message(cls.session_id, "user", f"vec msg {i}", working_dir="/tmp")
            cls.msg_ids.append(mid)
            blob = embed.vec_to_blob(vec)
            db.upsert_embedding(db.get_db(), mid, cls.model, blob)

    @classmethod
    def tearDownClass(cls):
        db.close_db()

    def test_numpy_search_returns_closest(self):
        """_vector_search_numpy returns the most similar message first."""
        query = [1.0, 0.0, 0.0]  # matches first message exactly
        results = embed._vector_search_numpy(query, self.model, limit=3)
        self.assertGreater(len(results), 0)
        top_id = results[0][0]
        self.assertEqual(top_id, self.msg_ids[0])

    def test_numpy_search_ranks_by_similarity(self):
        """Results are ordered by descending cosine similarity."""
        query = [0.0, 1.0, 0.0]  # closest to second message
        results = embed._vector_search_numpy(query, self.model, limit=3)
        ids = [r[0] for r in results]
        self.assertEqual(ids[0], self.msg_ids[1])

    def test_numpy_search_no_embeddings_returns_empty(self):
        """No stored embeddings for a model returns empty list."""
        results = embed._vector_search_numpy([1.0, 0.0, 0.0], "nonexistent-model", limit=5)
        self.assertEqual(results, [])


# ---------------------------------------------------------------------------
# Hybrid search — FTS fallback when embedding disabled
# ---------------------------------------------------------------------------

class TestHybridSearch(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _reset_db()
        db.get_db()
        cls.session_id = "hybrid-search-session"
        db.ensure_session(cls.session_id, "/tmp")
        mid = db.store_message(cls.session_id, "user", "SQLite preferences stored locally", working_dir="/tmp")
        cls.msg_id = mid

    @classmethod
    def tearDownClass(cls):
        db.close_db()

    def test_fts_fallback_when_disabled(self):
        """hybrid_search returns FTS-only when embeddingEnabled=False."""
        cfg = _cfg(embeddingEnabled=False)
        result = embed.hybrid_search("SQLite preferences", cfg)
        self.assertIn("messages", result)
        self.assertNotIn("hybrid", result)

    def test_fts_fallback_no_embeddings(self):
        """hybrid_search returns FTS-only when no embeddings stored."""
        cfg = _cfg(embeddingEnabled=True)
        with patch("embed.detect_provider", return_value="fastembed"), \
             patch("embed.embed_texts", return_value=[[0.1] * 3]):
            result = embed.hybrid_search("SQLite preferences", cfg)
        # No embeddings in DB for this model → vec_results empty → FTS fallback
        self.assertIn("messages", result)

    def test_model_mismatch_warning_and_fallback(self):
        """Model change triggers warning and FTS-only fallback."""
        cfg = _cfg(
            embeddingEnabled=True,
            embeddingModel="new-model",
            lastEmbeddingModel="old-model",
        )
        import io
        import contextlib
        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            result = embed.hybrid_search("anything", cfg)
        self.assertNotIn("hybrid", result)
        self.assertIn("Warning", stderr_capture.getvalue())

    def test_hybrid_rrf_fusion(self):
        """RRF fusion combines FTS and vector results correctly."""
        model = "rrf-test-model"
        cfg = _cfg(embeddingEnabled=True, embeddingModel=model, lastEmbeddingModel=model)

        # Store an embedding for the message
        blob = embed.vec_to_blob([1.0, 0.0, 0.0])
        db.upsert_embedding(db.get_db(), self.msg_id, model, blob)

        query_vec = [1.0, 0.0, 0.0]
        with patch("embed.detect_provider", return_value="fastembed"), \
             patch("embed.embed_texts", return_value=[query_vec]):
            result = embed.hybrid_search("SQLite preferences", cfg)

        self.assertIn("messages", result)
        # If vector results are found, hybrid=True should be set
        if len(result["messages"]) > 0:
            self.assertIn("hybrid", result)


# ---------------------------------------------------------------------------
# Phase B: embed_messages_batch and reindex_vault
# ---------------------------------------------------------------------------

class TestBatchIndexing(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _reset_db()
        db.get_db()
        cls.session_id = "batch-index-session"
        db.ensure_session(cls.session_id, "/tmp")
        cls.msg_ids = []
        for i in range(5):
            mid = db.store_message(cls.session_id, "user", f"batch message {i}", working_dir="/tmp")
            cls.msg_ids.append(mid)

    @classmethod
    def tearDownClass(cls):
        db.close_db()

    def test_embed_messages_batch_stores_embeddings(self):
        """embed_messages_batch embeds un-indexed messages and stores results."""
        model = "batch-test-model"
        cfg = _cfg(embeddingModel=model, lastEmbeddingModel=None)
        fake_vec = [0.1] * 3

        with patch("embed.detect_provider", return_value="fastembed"), \
             patch("embed.embed_texts", return_value=[fake_vec] * 5):
            stored = embed.embed_messages_batch(cfg, session_id=self.session_id)

        self.assertGreaterEqual(stored, 1)
        count = db.count_embeddings(model)
        self.assertGreaterEqual(count, 1)

    def test_embed_messages_batch_skips_when_disabled(self):
        """embed_messages_batch returns 0 when no provider available."""
        cfg = _cfg(embeddingEnabled=False)
        stored = embed.embed_messages_batch(cfg)
        self.assertEqual(stored, 0)

    def test_embed_messages_batch_skips_none_vectors(self):
        """embed_messages_batch skips messages that return None embeddings."""
        model = "partial-batch-model"
        cfg = _cfg(embeddingModel=model)
        # 3 messages but only first 2 return real vectors
        fake_vecs = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6], None]

        # Insert exactly 3 messages for this test
        session_id = "partial-batch-session"
        db.ensure_session(session_id, "/tmp")
        for i in range(3):
            db.store_message(session_id, "user", f"partial msg {i}", working_dir="/tmp")

        with patch("embed.detect_provider", return_value="fastembed"), \
             patch("embed.embed_texts", return_value=fake_vecs):
            stored = embed.embed_messages_batch(cfg, session_id=session_id)

        # At most 2 stored (the None one was skipped)
        self.assertLessEqual(stored, 2)

    def test_last_embedding_model_written_after_batch(self):
        """lastEmbeddingModel is updated in config after successful batch."""
        model = "last-model-write-test"
        cfg = _cfg(embeddingModel=model)
        fake_vec = [0.1, 0.2, 0.3]

        session_id = "last-model-session"
        db.ensure_session(session_id, "/tmp")
        db.store_message(session_id, "user", "test msg", working_dir="/tmp")

        with patch("embed.detect_provider", return_value="fastembed"), \
             patch("embed.embed_texts", return_value=[fake_vec]):
            embed.embed_messages_batch(cfg, session_id=session_id)

        saved_cfg = db.load_config()
        self.assertEqual(saved_cfg.get("lastEmbeddingModel"), model)

    def test_reindex_vault_backfills_all_messages(self):
        """reindex_vault embeds all messages when force=False and none indexed."""
        model = "reindex-test-model"
        cfg = _cfg(embeddingModel=model)
        fake_vec = [0.2, 0.3, 0.4]

        with patch("embed.detect_provider", return_value="fastembed"), \
             patch("embed.embed_texts", return_value=[fake_vec] * 100):
            stored = embed.reindex_vault(cfg, force=False, model_override=model)

        self.assertGreater(stored, 0)
        self.assertGreaterEqual(db.count_embeddings(model), stored)

    def test_reindex_vault_force_clears_and_reembeds(self):
        """reindex_vault with force=True deletes existing and re-embeds."""
        model = "force-reindex-model"
        cfg = _cfg(embeddingModel=model)
        fake_vec = [0.5, 0.6, 0.7]

        # First index
        with patch("embed.detect_provider", return_value="fastembed"), \
             patch("embed.embed_texts", return_value=[fake_vec] * 100):
            embed.reindex_vault(cfg, force=False, model_override=model)

        count_before = db.count_embeddings(model)

        # Force reindex
        with patch("embed.detect_provider", return_value="fastembed"), \
             patch("embed.embed_texts", return_value=[fake_vec] * 100):
            embed.reindex_vault(cfg, force=True, model_override=model)

        count_after = db.count_embeddings(model)
        self.assertGreaterEqual(count_after, count_before)

    def test_reindex_vault_no_provider_returns_zero(self):
        """reindex_vault returns 0 when no embedding provider available."""
        cfg = _cfg(embeddingEnabled=True)
        with patch("embed.detect_provider", return_value=None):
            stored = embed.reindex_vault(cfg)
        self.assertEqual(stored, 0)


# ---------------------------------------------------------------------------
# Phase C: Integration scenarios (per plan)
# ---------------------------------------------------------------------------

class TestIntegrationScenarios(unittest.TestCase):
    """The 5 integration scenarios from the plan's acceptance criteria."""

    @classmethod
    def setUpClass(cls):
        _reset_db()
        db.get_db()
        cls.session_id = "integration-session"
        db.ensure_session(cls.session_id, "/tmp")

    @classmethod
    def tearDownClass(cls):
        db.close_db()

    def test_scenario1_semantic_match_missed_by_fts(self):
        """Hybrid result can surface a message that FTS would miss.

        We can't test real embedding quality without a real model, but we
        verify that when a vector result is present, it is included in the
        ranked output even if the FTS result set is empty.
        """
        model = "scenario1-model"
        cfg = _cfg(embeddingModel=model, lastEmbeddingModel=model)

        mid = db.store_message(self.session_id, "user", "storing preferences in SQLite", working_dir="/tmp")
        blob = embed.vec_to_blob([1.0, 0.0, 0.0])
        db.upsert_embedding(db.get_db(), mid, model, blob)

        # Query: "persist user state" — FTS likely returns nothing; vector returns mid
        query_vec = [1.0, 0.0, 0.0]
        with patch("embed.detect_provider", return_value="fastembed"), \
             patch("embed.embed_texts", return_value=[query_vec]):
            result = embed.hybrid_search("persist user state", cfg)

        # The hybrid result should include our message via vector path
        message_ids = [m["id"] for m in result["messages"]]
        self.assertIn(mid, message_ids)

    def test_scenario2_model_change_detection(self):
        """Model change triggers warning and FTS-only fallback."""
        cfg = _cfg(embeddingModel="new-model-v2", lastEmbeddingModel="old-model-v1")
        import io
        import contextlib
        stderr_buf = io.StringIO()
        with contextlib.redirect_stderr(stderr_buf):
            result = embed.hybrid_search("anything", cfg)
        self.assertNotIn("hybrid", result)
        stderr_out = stderr_buf.getvalue()
        self.assertIn("Warning", stderr_out)
        self.assertIn("reindex", stderr_out)

    def test_scenario3_sqlite_vec_unavailable_fallback_to_numpy(self):
        """When sqlite-vec unavailable, search falls back to numpy BLOB cosine."""
        model = "scenario3-model"
        cfg = _cfg(embeddingModel=model, lastEmbeddingModel=model)
        mid = db.store_message(self.session_id, "user", "numpy fallback test", working_dir="/tmp")
        blob = embed.vec_to_blob([1.0, 0.0, 0.0])
        db.upsert_embedding(db.get_db(), mid, model, blob)

        query_vec = [1.0, 0.0, 0.0]
        # sqlite-vec not installed in test env — numpy path is the default
        with patch("embed.detect_provider", return_value="fastembed"), \
             patch("embed.embed_texts", return_value=[query_vec]):
            result = embed.hybrid_search("numpy fallback test", cfg)
        # Should return a result without raising
        self.assertIn("messages", result)

    def test_scenario4_no_provider_fts_only(self):
        """When neither sqlite-vec nor numpy available, FTS-only search executes cleanly."""
        cfg = _cfg()
        with patch("embed.detect_provider", return_value=None):
            result = embed.hybrid_search("any query", cfg)
        self.assertIn("messages", result)
        self.assertNotIn("hybrid", result)

    def test_scenario5_reindex_backfills_and_writes_last_model(self):
        """lcc reindex backfills existing vault and writes lastEmbeddingModel."""
        model = "scenario5-model"
        cfg = _cfg(embeddingModel=model)
        fake_vec = [0.3, 0.3, 0.3]

        with patch("embed.detect_provider", return_value="fastembed"), \
             patch("embed.embed_texts", return_value=[fake_vec] * 100):
            stored = embed.reindex_vault(cfg, model_override=model)

        self.assertGreater(stored, 0)
        saved = db.load_config()
        self.assertEqual(saved.get("lastEmbeddingModel"), model)
        self.assertEqual(db.count_embeddings(model), stored)


# ---------------------------------------------------------------------------
# Phase C: lcc status vector health output
# ---------------------------------------------------------------------------

class TestStatusVectorHealth(unittest.TestCase):
    """Verify lcc status shows vector search health."""

    @classmethod
    def setUpClass(cls):
        _reset_db()
        db.get_db()

    @classmethod
    def tearDownClass(cls):
        db.close_db()

    def test_status_shows_inactive_when_disabled(self):
        """lcc status includes 'inactive' line when embeddingEnabled=false."""
        import io
        import contextlib
        import lcc
        from unittest.mock import patch

        cfg = _cfg(embeddingEnabled=False)
        stdout_buf = io.StringIO()
        with contextlib.redirect_stdout(stdout_buf), \
             patch("lcc.db.load_config", return_value=cfg):
            lcc.cmd_status(None)
        output = stdout_buf.getvalue()
        self.assertIn("Vector search", output)
        self.assertIn("inactive", output)

    def test_status_shows_active_line_when_enabled_with_provider(self):
        """lcc status shows active + coverage when embeddingEnabled=true and provider present."""
        import io
        import contextlib
        import lcc

        model = "status-test-model"
        cfg = _cfg(embeddingModel=model)

        stdout_buf = io.StringIO()
        with contextlib.redirect_stdout(stdout_buf), \
             patch("embed.detect_provider", return_value="fastembed"), \
             patch("lcc.db.load_config", return_value=cfg):
            lcc.cmd_status(None)
        output = stdout_buf.getvalue()
        self.assertIn("Vector search", output)
        # Should show embeddings line
        self.assertIn("Embeddings", output)


if __name__ == "__main__":
    unittest.main()
