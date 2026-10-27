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
    max_chars: int = 1000,
    min_chars: int = 150,
) -> list[PassageChunk]:
    """Chunk clean text into structured passages with exact character offsets.

    Uses sentence-aware subdivision for long paragraphs so that every chunk
    remains within the embedding model token window (512 tokens / ~1200 chars).
    """
    if not text.strip():
        return []

    # 1. Identify all paragraph spans by double newlines, preserving exact character positions
    para_spans: list[tuple[int, int]] = []
    for m in re.finditer(r"(?:[^\n]|\n(?!\n))+", text):
        raw_span = m.group()
        stripped = raw_span.strip()
        if stripped:
            start = m.start() + (len(raw_span) - len(raw_span.lstrip()))
            end = start + len(stripped)
            para_spans.append((start, end))

    if not para_spans:
        para_spans = [(0, len(text))]

    # 2. Break down spans that exceed max_chars into sentence spans
    unit_spans: list[tuple[int, int]] = []
    for start, end in para_spans:
        span_len = end - start
        if span_len <= max_chars:
            unit_spans.append((start, end))
        else:
            para_text = text[start:end]
            sent_matches = list(re.finditer(r"(?:(?<=[.!?])\s+|\n+)", para_text))
            if not sent_matches:
                # No sentence punctuation found; subdivide by space
                sub_start = 0
                while sub_start < span_len:
                    sub_end = min(sub_start + max_chars, span_len)
                    if sub_end < span_len:
                        last_space = para_text.rfind(" ", sub_start, sub_end)
                        if last_space > sub_start:
                            sub_end = last_space
                    s_str = para_text[sub_start:sub_end].strip()
                    if s_str:
                        actual_s = start + sub_start + para_text[sub_start:sub_end].find(s_str)
                        unit_spans.append((actual_s, actual_s + len(s_str)))
                    sub_start = sub_end + 1
            else:
                sent_start = 0
                for sm in sent_matches:
                    sent_end = sm.start()
                    s_str = para_text[sent_start:sent_end].strip()
                    if s_str:
                        actual_s = start + sent_start + para_text[sent_start:sent_end].find(s_str)
                        unit_spans.append((actual_s, actual_s + len(s_str)))
                    sent_start = sm.end()
                if sent_start < span_len:
                    s_str = para_text[sent_start:span_len].strip()
                    if s_str:
                        actual_s = start + sent_start + para_text[sent_start:span_len].find(s_str)
                        unit_spans.append((actual_s, actual_s + len(s_str)))

    # 3. Pack consecutive unit spans into passages <= max_chars
    raw_chunks: list[tuple[int, int]] = []
    cur_start: int | None = None
    cur_end: int | None = None

    for u_start, u_end in unit_spans:
        if cur_start is None:
            cur_start = u_start
            cur_end = u_end
        else:
            cand_len = u_end - cur_start
            if cand_len <= max_chars:
                cur_end = u_end
            else:
                raw_chunks.append((cur_start, cur_end))
                cur_start = u_start
                cur_end = u_end

    if cur_start is not None and cur_end is not None:
        raw_chunks.append((cur_start, cur_end))

    # Merge very small trailing chunk if feasible
    if len(raw_chunks) > 1:
        prev_s, _prev_e = raw_chunks[-2]
        last_s, last_e = raw_chunks[-1]
        if (last_e - last_s) < min_chars and (last_e - prev_s) <= int(max_chars * 1.3):
            raw_chunks[-2] = (prev_s, last_e)
            raw_chunks.pop()

    passages: list[PassageChunk] = []
    for idx, (s_pos, e_pos) in enumerate(raw_chunks):
        chunk_text = text[s_pos:e_pos]
        passage_hash = compute_sha256(f"{url}#{idx}#{chunk_text}")
        passage_id = f"ev_{passage_hash[:16]}"
        passages.append(
            PassageChunk(
                passage_id=passage_id,
                document_url=url,
                order_index=idx,
                start_char=s_pos,
                end_char=e_pos,
                text=chunk_text,
                estimated_tokens=estimate_tokens(chunk_text),
                content_hash=passage_hash,
            )
        )

    return passages
