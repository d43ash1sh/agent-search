from __future__ import annotations

from fastapi import FastAPI

from services.api.routes import create_api_router
from services.common.config import settings
from services.indexer.sqlite_index import SQLiteSearchIndex
from services.queue.memory_broker import InMemoryQueueBroker

app = FastAPI(
    title="Agent Search Distributed API",
    description="Decoupled search, crawl staging, and retrieval service.",
    version="2.0.0",
)

# Default to in-memory broker & SQLite index unless overridden
broker = InMemoryQueueBroker()
index = SQLiteSearchIndex(settings.sqlite_path)

api_router = create_api_router(broker, index)
app.include_router(api_router)
