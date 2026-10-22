import unittest

import numpy as np

from services.common.models import SearchHit
from services.indexer.semantic_embedder import SemanticEmbedder


class TestSemanticEmbedder(unittest.TestCase):
    def setUp(self) -> None:
        self.embedder = SemanticEmbedder()

    def test_embed_query_and_passages(self) -> None:
        query_vec = self.embedder.embed_query("playwright browser automation")
        self.assertIsNotNone(query_vec)
        self.assertIsInstance(query_vec, np.ndarray)
        self.assertEqual(len(query_vec.shape), 1)

        passages = [
            "Playwright enables fast, reliable end-to-end browser automation in Python.",
            "PostgreSQL is an open-source relational database management system.",
        ]
        doc_vecs = self.embedder.embed_passages(passages)
        self.assertEqual(len(doc_vecs), 2)

        # Cosine similarity with matching passage should be significantly higher than unrelated passage
        sim_match = self.embedder.compute_similarity(query_vec, doc_vecs[0])
        sim_unrelated = self.embedder.compute_similarity(query_vec, doc_vecs[1])

        self.assertGreater(sim_match, sim_unrelated)
        self.assertGreater(sim_match, 0.4)

    def test_reciprocal_rank_fusion(self) -> None:
        hit1 = SearchHit(
            evidence_id="ev_01",
            url="https://example.com/1",
            canonical_url="https://example.com/1",
            title="First Hit",
            text="Text 1",
            score=10.0,
            estimated_tokens=50,
            match_type="lexical",
        )
        hit2 = SearchHit(
            evidence_id="ev_02",
            url="https://example.com/2",
            canonical_url="https://example.com/2",
            title="Second Hit",
            text="Text 2",
            score=5.0,
            estimated_tokens=50,
            match_type="lexical",
        )

        # In lexical: hit1 is rank 0, hit2 is rank 1
        lexical = [hit1, hit2]
        # In semantic: hit2 is rank 0, hit1 is rank 1
        semantic = [hit2, hit1]

        fused = self.embedder.reciprocal_rank_fusion(lexical, semantic)
        self.assertEqual(len(fused), 2)
        self.assertEqual(fused[0].match_type, "hybrid")
        # Equal reciprocal scores
        self.assertAlmostEqual(fused[0].score, fused[1].score, places=5)
