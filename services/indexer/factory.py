from __future__ import annotations

import logging

from services.common.config import settings
from services.indexer.base import AbstractSearchIndex
from services.indexer.postgres_index import PostgresSearchIndex
from services.indexer.sqlite_index import SQLiteSearchIndex

logger = logging.getLogger(__name__)


def create_search_index(backend: str | None = None) -> AbstractSearchIndex:
    """Factory to create the appropriate search index backend.

    Defaults to PostgreSQL with pgvector if configured and available, falling back to SQLite.
    """
    selected_backend = (backend or settings.index_backend).lower()

    if selected_backend in ("postgres", "postgresql", "pgvector"):
        from urllib.parse import urlsplit

        from services.common.docker_manager import is_port_in_use

        host = urlsplit(settings.postgres_url).hostname or "127.0.0.1"
        port = urlsplit(settings.postgres_url).port or 5432
        if is_port_in_use(host, port):
            try:
                logger.info("Initializing PostgresSearchIndex with URL: %s", settings.postgres_url)
                return PostgresSearchIndex(dsn=settings.postgres_url)
            except Exception as error:
                logger.warning(
                    "Failed to initialize PostgresSearchIndex (%s); falling back to SQLiteSearchIndex",
                    error,
                )
        else:
            logger.warning(
                "PostgreSQL server at %s:%d is unreachable; falling back to SQLiteSearchIndex at %s",
                host,
                port,
                settings.sqlite_path,
            )

    logger.info("Initializing SQLiteSearchIndex with path: %s", settings.sqlite_path)
    return SQLiteSearchIndex(database_path=settings.sqlite_path)
