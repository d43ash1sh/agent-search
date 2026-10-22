import os
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from crawler.pipelines import SQLitePipeline
from crawler.search import DEFAULT_DATABASE, SearchResult, SearchService


class Citation(BaseModel):
    evidence_id: str
    url: str
    canonical_url: str
    title: str
    domain: str
    fetched_at: str
    changed_at: str
    start_offset: int
    end_offset: int


class Evidence(BaseModel):
    passage_id: int
    evidence_id: str
    text: str
    citation: Citation
    score: float
    estimated_tokens: int


class SearchResponse(BaseModel):
    query: str
    results: list[Evidence]
    result_count: int
    estimated_tokens: int
    token_budget: int
    retrieval_mode: str = "lexical"
    match_mode: Literal["all", "any"] = "all"


def create_app(service_factory: Callable[[], SearchService] | None = None) -> FastAPI:
    get_service = service_factory or (lambda: SearchService(os.getenv("CRAWLER_DATABASE", DEFAULT_DATABASE)))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
        service = get_service()
        if os.path.exists(service.database):
            migration = SQLitePipeline(service.database)
            migration.open_spider()
            migration.close_spider()
        yield

    app = FastAPI(
        title="Agent Search API",
        version="0.1.0",
        description="Search a local web index and return compact, cited evidence.",
        lifespan=lifespan,
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    def make_evidence(result: SearchResult) -> Evidence:
        return Evidence(
            passage_id=result.passage_id,
            evidence_id=result.evidence_id,
            text=result.text,
            citation=Citation(
                evidence_id=result.evidence_id,
                url=result.url,
                canonical_url=result.canonical_url,
                title=result.title,
                domain=result.domain,
                fetched_at=result.fetched_at,
                changed_at=result.changed_at,
                start_offset=result.start_offset,
                end_offset=result.end_offset,
            ),
            score=result.score,
            estimated_tokens=result.estimated_tokens,
        )

    @app.get("/v1/search", response_model=SearchResponse)
    def search(
        q: Annotated[str, Query(min_length=1, max_length=500)],
        limit: Annotated[int, Query(ge=1, le=50)] = 10,
        max_tokens: Annotated[int, Query(ge=1, le=12000)] = 1200,
        domain: Annotated[list[str] | None, Query()] = None,
        after: str | None = None,
        before: str | None = None,
        match: Literal["all", "any"] = "all",
    ) -> SearchResponse:
        service = get_service()
        if not os.path.exists(service.database):
            index_path = Path(service.database).resolve()
            raise HTTPException(
                status_code=503,
                detail=(
                    f"Search index not found at {index_path}. "
                    "Run `python -m crawler crawl --seed <URL> --database <same-path>` "
                    "first, then start the API with `--database <same-path>`."
                ),
            )
        results, token_total = service.search(
            q,
            limit=limit,
            max_tokens=max_tokens,
            domains=domain,
            after=after,
            before=before,
            match=match,
        )

        evidence = [make_evidence(result) for result in results]
        return SearchResponse(
            query=q,
            results=evidence,
            result_count=len(evidence),
            estimated_tokens=token_total,
            token_budget=max_tokens,
            match_mode=match,
        )

    @app.get("/v1/evidence/{evidence_id}", response_model=Evidence)
    def get_evidence(evidence_id: str) -> Evidence:
        service = get_service()
        if not os.path.exists(service.database):
            raise HTTPException(
                status_code=503,
                detail=f"Search index not found at {Path(service.database).resolve()}.",
            )
        result = service.get_evidence(evidence_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Evidence passage not found.")
        return make_evidence(result)

    return app


app = create_app()
