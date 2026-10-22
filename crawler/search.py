import math
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

DEFAULT_DATABASE = "data/crawler.sqlite3"


@dataclass(frozen=True)
class SearchResult:
    passage_id: int
    evidence_id: str
    text: str
    title: str
    url: str
    canonical_url: str
    domain: str
    score: float
    fetched_at: str
    changed_at: str
    start_offset: int
    end_offset: int
    estimated_tokens: int


class SearchService:
    def __init__(self, database: str = DEFAULT_DATABASE) -> None:
        self.database = str(Path(database).expanduser())

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        max_tokens: int = 1200,
        domains: list[str] | None = None,
        after: str | None = None,
        before: str | None = None,
        match: Literal["all", "any"] = "all",
    ) -> tuple[list[SearchResult], int]:
        terms = list(dict.fromkeys(_query_terms(query)))
        if not terms:
            return [], 0

        operator = " AND " if match == "all" else " OR "
        match_query = operator.join(f'"{term}"' for term in terms)
        normalized_domains = [domain.lower().strip(".") for domain in (domains or [])]
        domain_clause = ""
        parameters: list[object] = [match_query]
        if normalized_domains:
            domain_clause = (
                " AND (" + " OR ".join("(d.domain = ? OR d.domain LIKE ?)" for _ in normalized_domains) + ")"
            )
            for domain in normalized_domains:
                parameters.extend((domain, f"%.{domain}"))
        date_clause = ""
        if after:
            date_clause += " AND d.fetched_at >= ?"
            parameters.append(after)
        if before:
            date_clause += " AND d.fetched_at <= ?"
            parameters.append(before)
        parameters.extend((max(limit * 10, 50),))

        connection = sqlite3.connect(self.database)
        try:
            rows: list[tuple[int, str, str, str, str, str, str, str, int, int, float]] = connection.execute(
                f"""
                SELECT p.id, p.evidence_id, p.text, d.title, d.url, d.canonical_url,
                       d.fetched_at, d.changed_at, p.start_offset, p.end_offset,
                       bm25(passages_fts) AS rank
                FROM passages_fts
                JOIN passages AS p ON p.id = passages_fts.rowid
                JOIN documents AS d ON d.url = p.document_url
                WHERE passages_fts MATCH ? {domain_clause} {date_clause}
                ORDER BY rank
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        finally:
            connection.close()

        seen_canonical: set[str] = set()
        results: list[SearchResult] = []
        used_tokens = 0
        for (
            passage_id,
            evidence_id,
            text,
            title,
            url,
            canonical_url,
            fetched_at,
            changed_at,
            start_offset,
            end_offset,
            rank,
        ) in rows:
            if canonical_url in seen_canonical:
                continue
            domain = urlsplit(url).hostname or ""
            estimated_tokens = estimate_tokens(f"{text} {title} {url} {canonical_url} {domain}")
            if used_tokens + estimated_tokens > max_tokens:
                continue
            seen_canonical.add(canonical_url)
            used_tokens += estimated_tokens
            results.append(
                SearchResult(
                    passage_id=passage_id,
                    evidence_id=evidence_id,
                    text=text,
                    title=title,
                    url=url,
                    canonical_url=canonical_url,
                    domain=domain,
                    score=round(1 / (1 + abs(rank)), 6),
                    fetched_at=fetched_at,
                    changed_at=changed_at,
                    start_offset=start_offset,
                    end_offset=end_offset,
                    estimated_tokens=estimated_tokens,
                )
            )
            if len(results) >= limit:
                break
        return results, used_tokens

    def get_evidence(self, evidence_id: str) -> SearchResult | None:
        connection = sqlite3.connect(self.database)
        try:
            row: tuple[int, str, str, str, str, str, str, str, int, int] | None = connection.execute(
                """
                SELECT p.id, p.evidence_id, p.text, d.title, d.url, d.canonical_url,
                       d.fetched_at, d.changed_at, p.start_offset, p.end_offset
                FROM passages AS p
                JOIN documents AS d ON d.url = p.document_url
                WHERE p.evidence_id = ?
                """,
                (evidence_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None

        (
            passage_id,
            stable_id,
            text,
            title,
            url,
            canonical_url,
            fetched_at,
            changed_at,
            start_offset,
            end_offset,
        ) = row
        domain = urlsplit(url).hostname or ""
        return SearchResult(
            passage_id=passage_id,
            evidence_id=stable_id,
            text=text,
            title=title,
            url=url,
            canonical_url=canonical_url,
            domain=domain,
            score=1.0,
            fetched_at=fetched_at,
            changed_at=changed_at,
            start_offset=start_offset,
            end_offset=end_offset,
            estimated_tokens=estimate_tokens(f"{text} {title} {url} {canonical_url} {domain}"),
        )


def estimate_tokens(text: str) -> int:
    """Conservative, model-independent estimate; use a tokenizer for exact counts."""
    return math.ceil(len(text) / 4)


def _query_terms(query: str) -> list[str]:
    terms: list[str] = []
    current: list[str] = []
    for character in unicodedata.normalize("NFC", query):
        category = unicodedata.category(character)
        if category[0] in {"L", "N"} or (category[0] == "M" and current):
            current.append(character)
        elif current:
            terms.append("".join(current))
            current = []
    if current:
        terms.append("".join(current))
    return terms
