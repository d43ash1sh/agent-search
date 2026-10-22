from __future__ import annotations

import logging
from typing import Any

from services.common.config import settings
from services.common.models import CleanDocument, SearchHit
from services.indexer.base import AbstractSearchIndex

logger = logging.getLogger(__name__)


class OpenSearchIndex(AbstractSearchIndex):
    """Distributed BM25 lexical search index using OpenSearch / Elasticsearch."""

    def __init__(self, opensearch_url: str | None = None, index_name: str = "agent_documents") -> None:
        self.url = opensearch_url or settings.opensearch_url
        self.index_name = index_name
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                from opensearchpy import OpenSearch  # type: ignore[import-not-found]

                self._client = OpenSearch(hosts=[self.url], timeout=10)
                self._ensure_index()
            except ImportError as err:
                raise RuntimeError(
                    "opensearch-py is not installed. Install with `pip install opensearch-py`"
                ) from err
        return self._client

    def _ensure_index(self) -> None:
        if not self._client.indices.exists(index=self.index_name):
            mapping = {
                "settings": {
                    "index": {
                        "number_of_shards": 2,
                        "number_of_replicas": 1,
                        "similarity": {"default": {"type": "BM25"}},
                    }
                },
                "mappings": {
                    "properties": {
                        "url": {"type": "keyword"},
                        "canonical_url": {"type": "keyword"},
                        "title": {"type": "text", "boost": 2.0},
                        "clean_text": {"type": "text"},
                        "markdown": {"type": "text"},
                        "content_hash": {"type": "keyword"},
                        "passages": {
                            "type": "nested",
                            "properties": {
                                "passage_id": {"type": "keyword"},
                                "text": {"type": "text"},
                                "order_index": {"type": "integer"},
                            },
                        },
                    }
                },
            }
            self._client.indices.create(index=self.index_name, body=mapping)

    async def index_document(self, doc: CleanDocument) -> None:
        client = self._get_client()
        body = doc.model_dump()
        client.index(
            index=self.index_name,
            id=doc.canonical_url,
            body=body,
            refresh=True,
        )

    async def search(
        self,
        query: str,
        limit: int = 10,
        max_tokens: int = 1200,
        domains: list[str] | None = None,
        hybrid: bool = False,
    ) -> tuple[list[SearchHit], int]:
        client = self._get_client()
        search_query: dict[str, Any] = {
            "size": limit,
            "query": {
                "multi_match": {
                    "query": query,
                    "fields": ["title^3", "clean_text", "passages.text"],
                }
            },
        }
        res = client.search(index=self.index_name, body=search_query)
        hits: list[SearchHit] = []
        total_tokens = 0

        for item in res.get("hits", {}).get("hits", []):
            src = item.get("_source", {})
            score = float(item.get("_score", 0.0))
            text_snippet = src.get("clean_text", "")[:400]
            est_tokens = max(1, len(text_snippet.split()))
            if total_tokens + est_tokens > max_tokens and hits:
                break
            total_tokens += est_tokens
            hits.append(
                SearchHit(
                    evidence_id=f"os_{src.get('content_hash', '')[:12]}",
                    url=src.get("url", ""),
                    canonical_url=src.get("canonical_url", ""),
                    title=src.get("title", ""),
                    text=text_snippet,
                    score=score,
                    estimated_tokens=est_tokens,
                    match_type="lexical",
                )
            )

        return hits, total_tokens

    async def get_evidence(self, evidence_id: str) -> SearchHit | None:
        return None

    async def get_stats(self) -> dict[str, Any]:
        client = self._get_client()
        count = client.count(index=self.index_name).get("count", 0)
        return {"backend": "opensearch", "document_count": count}
