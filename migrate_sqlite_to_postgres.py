from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

from pgvector import Vector

from services.common.config import settings
from services.indexer.postgres_index import PostgresSearchIndex
from services.indexer.semantic_embedder import SemanticEmbedder

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("migrator")


async def migrate(
    sqlite_path: str = "data/crawler.sqlite3",
    postgres_url: str = settings.postgres_url,
    embed_limit: int = 500,
    batch_size: int = 1000,
) -> None:
    db_file = Path(sqlite_path)
    if not db_file.exists():
        logger.error("SQLite database not found at %s", sqlite_path)
        return

    logger.info("Starting migration from SQLite (%s) -> PostgreSQL (%s)", sqlite_path, postgres_url)
    index = PostgresSearchIndex(dsn=postgres_url)
    pool = await index.get_pool()

    sqlite_conn = sqlite3.connect(sqlite_path)
    cur = sqlite_conn.cursor()

    # 1. Migrate Documents
    cur.execute(
        "SELECT url, canonical_url, title, text, markdown, domain, content_hash, changed_at, fetched_at FROM documents"
    )
    docs = cur.fetchall()
    logger.info("Migrating %d documents to PostgreSQL...", len(docs))

    async with pool.acquire() as conn:
        doc_records = [
            (
                d[0],  # url
                d[1],  # canonical_url
                d[2],  # title
                d[3],  # clean_text
                d[4] or d[3],  # markdown
                d[5] or (urlsplit(d[0]).hostname or "").lower(),  # domain
                None,  # author
                None,  # published_date
                None,  # sitename
                None,  # description
                d[6] or "",  # content_hash
                0,  # simhash
            )
            for d in docs
        ]

        # Bulk upsert documents in batches of 500
        for i in range(0, len(doc_records), 500):
            batch = doc_records[i : i + 500]
            await conn.executemany(
                """
                INSERT INTO documents (
                    url, canonical_url, title, clean_text, markdown, domain,
                    author, published_date, sitename, description, content_hash, simhash,
                    changed_at, fetched_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, NOW(), NOW())
                ON CONFLICT (url) DO UPDATE SET
                    canonical_url = EXCLUDED.canonical_url,
                    title = EXCLUDED.title,
                    clean_text = EXCLUDED.clean_text,
                    markdown = EXCLUDED.markdown,
                    domain = EXCLUDED.domain,
                    content_hash = EXCLUDED.content_hash;
                """,
                batch,
            )
    logger.info("Successfully migrated %d documents.", len(docs))

    # 2. Fast Bulk Migrate All Passages (Text + Offsets + Full-Text Search)
    cur.execute("SELECT document_url, ordinal, text, evidence_id, start_offset, end_offset FROM passages")
    passages = cur.fetchall()
    total_passages = len(passages)
    logger.info("Migrating all %d passages to PostgreSQL (batch size: %d)...", total_passages, batch_size)

    for i in range(0, total_passages, batch_size):
        chunk = passages[i : i + batch_size]
        passage_records = [
            (
                p[0],  # document_url
                p[1],  # ordinal
                p[2],  # text
                p[3],  # evidence_id
                p[4],  # start_offset
                p[5],  # end_offset
            )
            for p in chunk
        ]

        async with pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO passages (
                    document_url, ordinal, text, evidence_id,
                    start_offset, end_offset
                ) VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (evidence_id) DO UPDATE SET
                    text = EXCLUDED.text;
                """,
                passage_records,
            )

    logger.info("Successfully migrated all %d passages into PostgreSQL with tsvector index.", total_passages)

    # 3. Compute and Store pgvector Dense Embeddings for Top Passages
    if embed_limit > 0:
        logger.info(
            "Generating pgvector dense embeddings for top %d passages...", min(embed_limit, total_passages)
        )
        embedder = SemanticEmbedder()
        embed_chunk = passages[:embed_limit]

        embed_batch_size = 50
        for i in range(0, len(embed_chunk), embed_batch_size):
            sub_chunk = embed_chunk[i : i + embed_batch_size]
            texts = [p[2] for p in sub_chunk]
            embeddings = embedder.embed_passages(texts)

            update_records = [
                (Vector(emb), p[3])  # (embedding, evidence_id)
                for p, emb in zip(sub_chunk, embeddings, strict=False)
            ]

            async with pool.acquire() as conn:
                await conn.executemany(
                    """
                    UPDATE passages
                    SET embedding = $1
                    WHERE evidence_id = $2;
                    """,
                    update_records,
                )
            logger.info(
                "Embedded %d / %d passages with pgvector",
                min(i + embed_batch_size, len(embed_chunk)),
                len(embed_chunk),
            )

    # 4. Migrate Frontier URLs
    try:
        cur.execute("SELECT url, domain, source_url, depth, status, discovered_at FROM frontier_urls")
        frontier_rows = cur.fetchall()
        logger.info("Migrating %d frontier URLs to PostgreSQL...", len(frontier_rows))

        frontier_records = [(r[0], r[1], r[2], r[3], r[4]) for r in frontier_rows]

        async with pool.acquire() as conn:
            for i in range(0, len(frontier_records), 500):
                batch_f = frontier_records[i : i + 500]
                await conn.executemany(
                    """
                    INSERT INTO frontier_urls (url, domain, source_url, depth, status, discovered_at)
                    VALUES ($1, $2, $3, $4, $5, NOW())
                    ON CONFLICT (url) DO UPDATE SET status = EXCLUDED.status;
                    """,
                    batch_f,
                )
        logger.info("Successfully migrated %d frontier URLs.", len(frontier_rows))
    except Exception as e:
        logger.warning("No frontier_urls table found in SQLite or error reading it: %s", e)

    sqlite_conn.close()
    await index.close()
    logger.info("Migration to PostgreSQL with pgvector and full-text search complete!")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate SQLite crawler database to PostgreSQL with pgvector"
    )
    parser.add_argument("--sqlite-path", default="data/crawler.sqlite3", help="Path to SQLite database")
    parser.add_argument("--postgres-url", default=settings.postgres_url, help="PostgreSQL connection string")
    parser.add_argument(
        "--embed-limit", type=int, default=300, help="Number of passages to immediately embed with pgvector"
    )
    args = parser.parse_args()

    asyncio.run(
        migrate(
            sqlite_path=args.sqlite_path,
            postgres_url=args.postgres_url,
            embed_limit=args.embed_limit,
        )
    )


if __name__ == "__main__":
    main()
