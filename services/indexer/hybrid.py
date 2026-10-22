from __future__ import annotations

from services.common.models import SearchHit


def reciprocal_rank_fusion(
    lexical_hits: list[SearchHit],
    vector_hits: list[SearchHit],
    k: int = 60,
    limit: int = 10,
    max_tokens: int = 1200,
) -> tuple[list[SearchHit], int]:
    """Fuse lexical and vector search results using Reciprocal Rank Fusion (RRF)."""
    scores: dict[str, float] = {}
    doc_map: dict[str, SearchHit] = {}

    for rank, hit in enumerate(lexical_hits, start=1):
        scores[hit.evidence_id] = scores.get(hit.evidence_id, 0.0) + (1.0 / (k + rank))
        doc_map[hit.evidence_id] = hit

    for rank, hit in enumerate(vector_hits, start=1):
        scores[hit.evidence_id] = scores.get(hit.evidence_id, 0.0) + (1.0 / (k + rank))
        if hit.evidence_id not in doc_map:
            doc_map[hit.evidence_id] = hit

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    fused: list[SearchHit] = []
    total_tokens = 0

    for ev_id, fused_score in ranked[:limit]:
        hit = doc_map[ev_id]
        if total_tokens + hit.estimated_tokens > max_tokens and fused:
            break
        total_tokens += hit.estimated_tokens
        hit.score = fused_score
        hit.match_type = "hybrid"
        fused.append(hit)

    return fused, total_tokens
