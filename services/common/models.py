from __future__ import annotations

import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class FetchRequest(BaseModel):
    """Instruction for the Fetcher service to retrieve a URL."""

    url: str
    depth: int = 0
    priority: int = 0
    domain: str = ""
    render_js: bool = False
    screenshot: bool = False
    actions: list[dict[str, Any]] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RawPageRecord(BaseModel):
    """Decoupled raw download artifact produced by Fetcher service.

    Contains untouched HTTP body and headers before any CPU-intensive extraction.
    """

    url: str
    canonical_url: str
    status_code: int
    headers: dict[str, str] = Field(default_factory=dict)
    raw_html: str
    fetched_at: str = Field(default_factory=lambda: datetime.datetime.now(datetime.UTC).isoformat())
    fetcher_engine: Literal["httpx", "playwright"] = "httpx"
    elapsed_seconds: float = 0.0
    screenshot_base64: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class FailedFetchRecord(BaseModel):
    """Artifact routed to the Dead Letter Queue (DLQ) when a fetch fails.

    Guarantees that network errors never stall or crash the indexing pipeline.
    """

    url: str
    domain: str = ""
    error_type: str
    error_message: str
    retries_attempted: int = 0
    failed_at: str = Field(default_factory=lambda: datetime.datetime.now(datetime.UTC).isoformat())


class PassageChunk(BaseModel):
    """Token-budgeted evidence unit for agent retrieval."""

    passage_id: str
    document_url: str
    order_index: int
    start_char: int
    end_char: int
    text: str
    estimated_tokens: int
    content_hash: str


class CleanDocument(BaseModel):
    """Normalized, extracted document produced by Extractor service."""

    url: str
    canonical_url: str
    title: str = ""
    author: str | None = None
    published_date: str | None = None
    sitename: str | None = None
    description: str | None = None
    clean_text: str = ""
    markdown: str = ""
    passages: list[PassageChunk] = Field(default_factory=list)
    content_hash: str = ""
    simhash: int = 0
    extracted_at: str = Field(default_factory=lambda: datetime.datetime.now(datetime.UTC).isoformat())


class SearchHit(BaseModel):
    """Ranked evidence item returned by Search API."""

    evidence_id: str
    url: str
    canonical_url: str
    title: str
    text: str
    score: float
    estimated_tokens: int
    match_type: Literal["lexical", "vector", "hybrid"] = "lexical"
    author: str | None = None
    published_date: str | None = None
