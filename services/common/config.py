from __future__ import annotations

import os

from pydantic import BaseModel, Field


class ServiceSettings(BaseModel):
    # Queue / Broker settings
    redis_url: str = Field(default_factory=lambda: os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    fetch_queue_name: str = Field(default_factory=lambda: os.getenv("FETCH_QUEUE_NAME", "queue:fetch"))
    staging_queue_name: str = Field(default_factory=lambda: os.getenv("STAGING_QUEUE_NAME", "queue:staging"))
    index_queue_name: str = Field(default_factory=lambda: os.getenv("INDEX_QUEUE_NAME", "queue:index"))
    dlq_name: str = Field(default_factory=lambda: os.getenv("DLQ_NAME", "queue:dead_letter"))

    # Fetcher settings
    max_connections: int = Field(default_factory=lambda: int(os.getenv("MAX_CONNECTIONS", "100")))
    max_keepalive: int = Field(default_factory=lambda: int(os.getenv("MAX_KEEPALIVE", "20")))
    connect_timeout: float = Field(default_factory=lambda: float(os.getenv("CONNECT_TIMEOUT", "10.0")))
    read_timeout: float = Field(default_factory=lambda: float(os.getenv("READ_TIMEOUT", "15.0")))
    enable_playwright_fallback: bool = Field(
        default_factory=lambda: os.getenv("ENABLE_PLAYWRIGHT_FALLBACK", "false").lower() in ("true", "1")
    )

    # Search / Indexer settings
    opensearch_url: str = Field(default_factory=lambda: os.getenv("OPENSEARCH_URL", "http://localhost:9200"))
    postgres_url: str = Field(
        default_factory=lambda: os.getenv(
            "POSTGRES_URL", "postgresql://postgres:postgres@localhost:5432/search_db"
        )
    )
    sqlite_path: str = Field(default_factory=lambda: os.getenv("SQLITE_PATH", "data/crawler.sqlite3"))
    index_backend: str = Field(
        default_factory=lambda: os.getenv("INDEX_BACKEND", "sqlite")
    )  # "sqlite", "opensearch", "pgvector", or "hybrid"

    # API settings
    api_host: str = Field(default_factory=lambda: os.getenv("API_HOST", "0.0.0.0"))
    api_port: int = Field(default_factory=lambda: int(os.getenv("API_PORT", "8000")))


settings = ServiceSettings()
