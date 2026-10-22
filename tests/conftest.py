"""Shared pytest fixtures for the crawler test suite."""

from collections.abc import Generator
from pathlib import Path

import pytest

from crawler.pipelines import SQLitePipeline
from crawler.search import SearchService


@pytest.fixture()
def tmp_database(tmp_path: Path) -> Generator[str, None, None]:
    """Provide a temporary SQLite database path with schema initialised."""
    database = str(tmp_path / "test.sqlite3")
    pipeline = SQLitePipeline(database)
    pipeline.open_spider()
    pipeline.close_spider()
    yield database


@pytest.fixture()
def search_service(tmp_database: str) -> SearchService:
    """Provide a SearchService backed by a fresh temporary database."""
    return SearchService(tmp_database)
