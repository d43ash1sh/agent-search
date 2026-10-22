import math
import sqlite3
import unicodedata
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal
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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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
        mode: Literal["compact", "balanced", "research"] = "compact",
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
        parameters.extend((max(limit * 15, 60),))

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

        # Build candidate results
        candidates: list[SearchResult] = []
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
            domain = (urlsplit(url).hostname or "").lower()
            estimated_tokens = estimate_tokens(f"{text} {title} {url} {canonical_url} {domain}")
            candidates.append(
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

        # Select results based on retrieval mode
        return _select_results(candidates, limit=limit, max_tokens=max_tokens, mode=mode)

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
        domain = (urlsplit(url).hostname or "").lower()
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

    def get_evidence_context(self, evidence_id: str, window: int = 1) -> dict[str, Any] | None:
        """Retrieve neighboring passages around an evidence passage for expanded verification."""
        connection = sqlite3.connect(self.database)
        try:
            target = connection.execute(
                """
                SELECT id, document_url, ordinal, text, start_offset, end_offset
                FROM passages WHERE evidence_id = ?
                """,
                (evidence_id,),
            ).fetchone()
            if not target:
                return None

            passage_id, doc_url, ordinal, text, start_off, end_off = target
            preceding = connection.execute(
                """
                SELECT id, evidence_id, ordinal, text, start_offset, end_offset
                FROM passages
                WHERE document_url = ? AND ordinal >= ? AND ordinal < ?
                ORDER BY ordinal ASC
                """,
                (doc_url, max(0, ordinal - window), ordinal),
            ).fetchall()

            succeeding = connection.execute(
                """
                SELECT id, evidence_id, ordinal, text, start_offset, end_offset
                FROM passages
                WHERE document_url = ? AND ordinal > ? AND ordinal <= ?
                ORDER BY ordinal ASC
                """,
                (doc_url, ordinal, ordinal + window),
            ).fetchall()

            doc_info = connection.execute(
                "SELECT title, canonical_url, domain, fetched_at FROM documents WHERE url = ?",
                (doc_url,),
            ).fetchone()
        finally:
            connection.close()

        title, canonical_url, domain, fetched_at = doc_info if doc_info else ("", doc_url, "", "")

        def make_passage_dict(row: tuple[Any, ...]) -> dict[str, Any]:
            p_id, ev_id, ord_num, p_text, s_off, e_off = row
            return {
                "passage_id": p_id,
                "evidence_id": ev_id,
                "ordinal": ord_num,
                "text": p_text,
                "start_offset": s_off,
                "end_offset": e_off,
                "estimated_tokens": estimate_tokens(p_text),
            }

        return {
            "evidence_id": evidence_id,
            "document_url": doc_url,
            "canonical_url": canonical_url,
            "title": title,
            "domain": domain,
            "fetched_at": fetched_at,
            "ordinal": ordinal,
            "target": {
                "passage_id": passage_id,
                "evidence_id": evidence_id,
                "ordinal": ordinal,
                "text": text,
                "start_offset": start_off,
                "end_offset": end_off,
                "estimated_tokens": estimate_tokens(text),
            },
            "preceding": [make_passage_dict(r) for r in preceding],
            "succeeding": [make_passage_dict(r) for r in succeeding],
        }

    def get_document(self, url: str) -> dict[str, Any] | None:
        """Retrieve full document text and all its indexed passages."""
        connection = sqlite3.connect(self.database)
        try:
            doc = connection.execute(
                """
                SELECT url, canonical_url, title, text, domain, content_hash, changed_at, fetched_at
                FROM documents WHERE url = ? OR canonical_url = ?
                """,
                (url, url),
            ).fetchone()
            if not doc:
                return None

            doc_url, canonical_url, title, full_text, domain, content_hash, changed_at, fetched_at = doc
            passages = connection.execute(
                """
                SELECT id, evidence_id, ordinal, text, start_offset, end_offset
                FROM passages WHERE document_url = ? ORDER BY ordinal ASC
                """,
                (doc_url,),
            ).fetchall()
        finally:
            connection.close()

        return {
            "url": doc_url,
            "canonical_url": canonical_url,
            "title": title,
            "text": full_text,
            "domain": domain,
            "content_hash": content_hash,
            "changed_at": changed_at,
            "fetched_at": fetched_at,
            "estimated_tokens": estimate_tokens(full_text),
            "passages": [
                {
                    "passage_id": p[0],
                    "evidence_id": p[1],
                    "ordinal": p[2],
                    "text": p[3],
                    "start_offset": p[4],
                    "end_offset": p[5],
                    "estimated_tokens": estimate_tokens(p[3]),
                }
                for p in passages
            ],
        }

    def get_stats(self) -> dict[str, Any]:
        """Return search index statistics."""
        db_path = Path(self.database)
        if not db_path.exists():
            return {"exists": False, "database_path": str(db_path)}

        connection = sqlite3.connect(self.database)
        try:
            doc_count: int = connection.execute("SELECT count(*) FROM documents").fetchone()[0]
            passage_count: int = connection.execute("SELECT count(*) FROM passages").fetchone()[0]
            domain_count: int = connection.execute(
                "SELECT count(DISTINCT domain) FROM documents WHERE domain != ''"
            ).fetchone()[0]
        finally:
            connection.close()

        return {
            "exists": True,
            "database_path": str(db_path),
            "size_bytes": db_path.stat().st_size,
            "total_documents": doc_count,
            "total_passages": passage_count,
            "total_domains": domain_count,
        }


def _select_results(
    candidates: list[SearchResult],
    limit: int,
    max_tokens: int,
    mode: Literal["compact", "balanced", "research"],
) -> tuple[list[SearchResult], int]:
    """Select and rank evidence units according to the requested retrieval mode."""
    if mode == "compact":
        # Strict deduplication: 1 best passage per canonical URL
        selected: list[SearchResult] = []
        seen_canonical: set[str] = set()
        used_tokens = 0
        for item in candidates:
            if item.canonical_url in seen_canonical:
                continue
            if used_tokens + item.estimated_tokens > max_tokens:
                continue
            seen_canonical.add(item.canonical_url)
            used_tokens += item.estimated_tokens
            selected.append(item)
            if len(selected) >= limit:
                break
        return selected, used_tokens

    elif mode == "balanced":
        # Up to 2 passages per canonical URL, keeping diversity
        selected = []
        canonical_counts: dict[str, int] = defaultdict(int)
        used_tokens = 0
        for item in candidates:
            if canonical_counts[item.canonical_url] >= 2:
                continue
            if used_tokens + item.estimated_tokens > max_tokens:
                continue
            canonical_counts[item.canonical_url] += 1
            used_tokens += item.estimated_tokens
            selected.append(item)
            if len(selected) >= limit:
                break
        return selected, used_tokens

    else:
        # "research" mode: Round-robin across distinct domains to maximize source diversity
        by_domain: dict[str, list[SearchResult]] = defaultdict(list)
        for item in candidates:
            by_domain[item.domain].append(item)

        interleaved: list[SearchResult] = []
        domains = list(by_domain.keys())
        idx = 0
        while any(by_domain.values()):
            domain = domains[idx % len(domains)]
            if by_domain[domain]:
                interleaved.append(by_domain[domain].pop(0))
            idx += 1
            if len(interleaved) >= limit * 3:
                break

        selected = []
        seen_evidence: set[str] = set()
        used_tokens = 0
        for item in interleaved:
            if item.evidence_id in seen_evidence:
                continue
            if used_tokens + item.estimated_tokens > max_tokens:
                continue
            seen_evidence.add(item.evidence_id)
            used_tokens += item.estimated_tokens
            selected.append(item)
            if len(selected) >= limit:
                break
        return selected, used_tokens


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
