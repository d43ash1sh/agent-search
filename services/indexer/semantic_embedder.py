from __future__ import annotations

import logging
from typing import Any

import numpy as np

from services.common.models import SearchHit

logger = logging.getLogger(__name__)


class SemanticEmbedder:
    """Local, lightweight ONNX dense vector embedding using FastEmbed."""

    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5") -> None:
        self.model_name = model_name
        self._model: Any = None

    def _ensure_model(self) -> Any:
        if self._model is None:
            try:
                from fastembed import TextEmbedding

                logger.info("Initializing FastEmbed dense vector model: %s", self.model_name)
                self._model = TextEmbedding(model_name=self.model_name)
            except Exception as error:
                logger.warning("Could not load FastEmbed model: %s", error)
                return None
        return self._model

    def embed_query(self, query: str) -> np.ndarray | None:
        """Embed a search query into a normalized float32 vector."""
        model = self._ensure_model()
        if model is None:
            return None
        try:
            embeddings = list(model.query_embed(query))
            if embeddings:
                vec = np.array(embeddings[0], dtype=np.float32)
                norm = np.linalg.norm(vec)
                return vec / (norm + 1e-9)
            return None
        except Exception as error:
            logger.warning("Query embedding failed: %s", error)
            return None

    def embed_passages(self, texts: list[str]) -> list[np.ndarray]:
        """Embed multiple text passages into normalized float32 vectors."""
        model = self._ensure_model()
        if model is None or not texts:
            return []
        try:
            raw_embeddings = list(model.passage_embed(texts))
            normalized = []
            for emb in raw_embeddings:
                vec = np.array(emb, dtype=np.float32)
                norm = np.linalg.norm(vec)
                normalized.append(vec / (norm + 1e-9))
            return normalized
        except Exception as error:
            logger.warning("Passage embedding failed: %s", error)
            return []

    @staticmethod
    def compute_similarity(query_vec: np.ndarray, doc_vec: np.ndarray) -> float:
        """Compute cosine similarity between two normalized vectors."""
        return float(np.dot(query_vec, doc_vec))

    @staticmethod
    def reciprocal_rank_fusion(
        lexical_hits: list[SearchHit],
        semantic_hits: list[SearchHit],
        k: int = 60,
    ) -> list[SearchHit]:
        """Combine BM25 lexical rank with semantic vector rank using RRF."""
        scores: dict[str, float] = {}
        hit_map: dict[str, SearchHit] = {}

        for rank, hit in enumerate(lexical_hits):
            scores[hit.evidence_id] = scores.get(hit.evidence_id, 0.0) + 1.0 / (k + rank + 1)
            hit_map[hit.evidence_id] = hit

        for rank, hit in enumerate(semantic_hits):
            scores[hit.evidence_id] = scores.get(hit.evidence_id, 0.0) + 1.0 / (k + rank + 1)
            if hit.evidence_id not in hit_map:
                hit_map[hit.evidence_id] = hit

        # Sort by combined RRF score descending
        fused: list[SearchHit] = []
        for evidence_id, rrf_score in sorted(scores.items(), key=lambda item: item[1], reverse=True):
            orig_hit = hit_map[evidence_id]
            fused.append(
                SearchHit(
                    evidence_id=orig_hit.evidence_id,
                    url=orig_hit.url,
                    canonical_url=orig_hit.canonical_url,
                    title=orig_hit.title,
                    text=orig_hit.text,
                    score=float(rrf_score),
                    estimated_tokens=orig_hit.estimated_tokens,
                    match_type="hybrid",
                )
            )

        return fused
