from __future__ import annotations

import logging
from typing import Any

from services.common.models import SearchHit

logger = logging.getLogger(__name__)


class NeuralReranker:
    """Ultra-fast neural cross-encoder reranker using FlashRank (TinyBERT ONNX)."""

    def __init__(self, model_name: str = "ms-marco-TinyBERT-L-2-v2") -> None:
        self.model_name = model_name
        self._ranker: Any = None

    def _get_ranker(self) -> Any:
        if self._ranker is None:
            try:
                from flashrank import Ranker  # type: ignore[import-untyped]

                self._ranker = Ranker(model_name=self.model_name)
            except Exception as error:
                logger.warning("Failed to initialize FlashRank: %s", error)
                return None
        return self._ranker

    def rerank(
        self,
        query: str,
        hits: list[SearchHit],
        max_tokens: int = 1200,
        top_k: int | None = None,
    ) -> tuple[list[SearchHit], int]:
        """Rerank candidate search hits using neural cross-encoder.

        Packs the highest-signal evidence passages first within the token budget.
        """
        if not hits or not query.strip():
            return hits, sum(h.estimated_tokens for h in hits)

        ranker = self._get_ranker()
        if ranker is None:
            return hits, sum(h.estimated_tokens for h in hits)

        try:
            from flashrank import RerankRequest

            hit_map = {hit.evidence_id: hit for hit in hits}
            passages = [{"id": hit.evidence_id, "text": f"{hit.title}\n{hit.text}"} for hit in hits]

            request = RerankRequest(query=query, passages=passages)
            ranked_output = ranker.rerank(request)

            reranked_hits: list[SearchHit] = []
            total_tokens = 0
            limit = top_k or len(hits)

            for item in ranked_output[:limit]:
                ev_id = item["id"]
                score = float(item["score"])
                hit = hit_map[ev_id]
                if total_tokens + hit.estimated_tokens > max_tokens and reranked_hits:
                    break
                hit.score = score
                total_tokens += hit.estimated_tokens
                reranked_hits.append(hit)

            return reranked_hits, total_tokens

        except Exception as error:
            logger.warning("Neural reranking failed, falling back to original rank: %s", error)
            return hits, sum(h.estimated_tokens for h in hits)
