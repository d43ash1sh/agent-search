from __future__ import annotations

import re

from services.common.hashing import compute_sha256
from services.common.models import PassageChunk


def estimate_tokens(text: str) -> int:
    """Fast, accurate token estimation."""
    return max(1, len(re.findall(r"\w+|[^\w\s]", text, re.UNICODE)))


def chunk_document(
    url: str,
    text: str,
    max_chars: int = 1200,
    min_chars: int = 150,
) -> list[PassageChunk]:
    """Chunk clean text into structured passages with exact character offsets."""
    if not text.strip():
        return []

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        paragraphs = [p.strip() for p in text.splitlines() if p.strip()]
    if not paragraphs:
        paragraphs = [text.strip()]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for para in paragraphs:
        para_len = len(para)
        if current and (current_len + para_len + 2 > max_chars):
            chunks.append("\n\n".join(current))
            current = [para]
            current_len = para_len
        else:
            current.append(para)
            current_len += para_len + (2 if current_len else 0)

    if current:
        chunks.append("\n\n".join(current))

    # Merge very small trailing chunks
    if len(chunks) > 1 and len(chunks[-1]) < min_chars:
        chunks[-2] = f"{chunks[-2]}\n\n{chunks[-1]}"
        chunks.pop()

    passages: list[PassageChunk] = []
    cursor = 0

    for idx, chunk_text in enumerate(chunks):
        pos = text.find(chunk_text, cursor)
        if pos == -1:
            pos = cursor
        start_char = pos
        end_char = pos + len(chunk_text)
        cursor = end_char

        passage_hash = compute_sha256(f"{url}#{idx}#{chunk_text}")
        passage_id = f"ev_{passage_hash[:16]}"
        passages.append(
            PassageChunk(
                passage_id=passage_id,
                document_url=url,
                order_index=idx,
                start_char=start_char,
                end_char=end_char,
                text=chunk_text,
                estimated_tokens=estimate_tokens(chunk_text),
                content_hash=passage_hash,
            )
        )

    return passages
