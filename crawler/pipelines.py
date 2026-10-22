import hashlib
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from crawler.items import DocumentItem


class SQLitePipeline:
    def __init__(self, database: str) -> None:
        self.database = database
        self.connection: sqlite3.Connection | None = None

    @classmethod
    def from_crawler(cls, crawler: Any) -> "SQLitePipeline":
        return cls(crawler.settings.get("DATABASE_PATH", "data/crawler.sqlite3"))

    def open_spider(self) -> None:
        Path(self.database).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.database)
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS documents (
                url TEXT PRIMARY KEY,
                canonical_url TEXT NOT NULL,
                title TEXT NOT NULL,
                text TEXT NOT NULL,
                domain TEXT NOT NULL DEFAULT '',
                content_hash TEXT NOT NULL DEFAULT '',
                changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(documents)")}
        if "domain" not in columns:
            self.connection.execute("ALTER TABLE documents ADD COLUMN domain TEXT NOT NULL DEFAULT ''")
        if "content_hash" not in columns:
            self.connection.execute("ALTER TABLE documents ADD COLUMN content_hash TEXT NOT NULL DEFAULT ''")
        if "changed_at" not in columns:
            self.connection.execute("ALTER TABLE documents ADD COLUMN changed_at TEXT NOT NULL DEFAULT ''")
        if "markdown" not in columns:
            self.connection.execute("ALTER TABLE documents ADD COLUMN markdown TEXT NOT NULL DEFAULT ''")
        rows: list[tuple[str]] = self.connection.execute(
            "SELECT url FROM documents WHERE domain = ''"
        ).fetchall()
        self.connection.executemany(
            "UPDATE documents SET domain = ? WHERE url = ?",
            [((urlsplit(row[0]).hostname or "").lower(), row[0]) for row in rows],
        )
        self.connection.execute("UPDATE documents SET changed_at = fetched_at WHERE changed_at = ''")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS passages (
                id INTEGER PRIMARY KEY,
                document_url TEXT NOT NULL REFERENCES documents(url) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                text TEXT NOT NULL,
                evidence_id TEXT NOT NULL DEFAULT '',
                start_offset INTEGER NOT NULL DEFAULT 0,
                end_offset INTEGER NOT NULL DEFAULT 0,
                UNIQUE(document_url, ordinal)
            )
            """
        )
        passage_columns = {row[1] for row in self.connection.execute("PRAGMA table_info(passages)")}
        passage_schema_changed = False
        if "evidence_id" not in passage_columns:
            self.connection.execute("ALTER TABLE passages ADD COLUMN evidence_id TEXT NOT NULL DEFAULT ''")
            passage_schema_changed = True
        if "start_offset" not in passage_columns:
            self.connection.execute("ALTER TABLE passages ADD COLUMN start_offset INTEGER NOT NULL DEFAULT 0")
            passage_schema_changed = True
        if "end_offset" not in passage_columns:
            self.connection.execute("ALTER TABLE passages ADD COLUMN end_offset INTEGER NOT NULL DEFAULT 0")
            passage_schema_changed = True
        documents_without_hash: list[tuple[str, str, str, str]] = self.connection.execute(
            """
            SELECT url, canonical_url, title, text FROM documents
            WHERE content_hash = ''
            """
        ).fetchall()
        self.connection.executemany(
            "UPDATE documents SET content_hash = ? WHERE url = ?",
            [
                (
                    hashlib.sha256("\0".join((canonical_url, title, text)).encode("utf-8")).hexdigest(),
                    url,
                )
                for url, canonical_url, title, text in documents_without_hash
            ],
        )
        self.connection.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS passages_fts USING fts5(text)
            """
        )
        self.connection.executescript(
            """
            CREATE TRIGGER IF NOT EXISTS passages_ai AFTER INSERT ON passages BEGIN
                INSERT INTO passages_fts(rowid, text) VALUES (new.id, new.text);
            END;
            CREATE TRIGGER IF NOT EXISTS passages_ad AFTER DELETE ON passages BEGIN
                DELETE FROM passages_fts WHERE rowid = old.id;
            END;
            CREATE TRIGGER IF NOT EXISTS passages_au AFTER UPDATE ON passages BEGIN
                DELETE FROM passages_fts WHERE rowid = old.id;
                INSERT INTO passages_fts(rowid, text) VALUES (new.id, new.text);
            END;
            """
        )
        self.connection.execute("CREATE INDEX IF NOT EXISTS passages_document_idx ON passages(document_url)")
        if passage_schema_changed:
            legacy_documents: list[tuple[str, str]] = self.connection.execute(
                "SELECT url, text FROM documents"
            ).fetchall()
            for url, text in legacy_documents:
                self.connection.execute("DELETE FROM passages WHERE document_url = ?", (url,))
                self.connection.executemany(
                    """
                    INSERT INTO passages(
                        document_url, ordinal, text, evidence_id, start_offset, end_offset
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (url, ordinal, passage, evidence_id, start, end)
                        for ordinal, (passage, evidence_id, start, end) in enumerate(
                            _passage_records(url, text)
                        )
                    ],
                )
        self.connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS passages_evidence_id_idx ON passages(evidence_id)"
        )
        documents_without_passages: list[tuple[str, str]] = self.connection.execute(
            """
            SELECT d.url, d.text FROM documents AS d
            WHERE NOT EXISTS (
                SELECT 1 FROM passages AS p WHERE p.document_url = d.url
            )
            """
        ).fetchall()
        for url, text in documents_without_passages:
            self.connection.executemany(
                """
                INSERT INTO passages(
                    document_url, ordinal, text, evidence_id, start_offset, end_offset
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (url, ordinal, passage, evidence_id, start, end)
                    for ordinal, (passage, evidence_id, start, end) in enumerate(_passage_records(url, text))
                ],
            )
        self.connection.execute(
            """
            INSERT INTO passages_fts(rowid, text)
            SELECT p.id, p.text FROM passages AS p
            WHERE NOT EXISTS (SELECT 1 FROM passages_fts WHERE rowid = p.id)
            """
        )
        self.connection.commit()

    def process_item(self, item: DocumentItem) -> DocumentItem:
        if self.connection is None:
            raise RuntimeError("SQLite pipeline was used before it was opened")

        if item.get("markdown"):
            text = item["markdown"]
            markdown_content = item["markdown"]
            blocks = item.get("blocks") or [b.strip() for b in text.split("\n\n") if b.strip()]
        elif "\n\n" in item.get("text", ""):
            text = item["text"]
            markdown_content = item["text"]
            blocks = item.get("blocks") or [b.strip() for b in text.split("\n\n") if b.strip()]
        else:
            chunks = _clean_blocks(item.get("blocks") or [item["text"]])
            text = " ".join(chunks)
            markdown_content = text
            blocks = chunks

        passage_records = _passage_records(item["url"], text, blocks)
        content_hash = hashlib.sha256(
            "\0".join((item["canonical_url"], item["title"], text)).encode("utf-8")
        ).hexdigest()
        domain = (urlsplit(item["url"]).hostname or "").lower()
        self.connection.execute(
            """
            INSERT INTO documents(
                url, canonical_url, title, text, markdown, domain, content_hash, changed_at, fetched_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(url) DO UPDATE SET
                canonical_url = excluded.canonical_url,
                title = excluded.title,
                text = excluded.text,
                markdown = excluded.markdown,
                domain = excluded.domain,
                changed_at = CASE
                    WHEN documents.content_hash != excluded.content_hash THEN CURRENT_TIMESTAMP
                    ELSE documents.changed_at
                END,
                content_hash = excluded.content_hash,
                fetched_at = CURRENT_TIMESTAMP
            """,
            (item["url"], item["canonical_url"], item["title"], text, markdown_content, domain, content_hash),
        )
        self.connection.execute("DELETE FROM passages WHERE document_url = ?", (item["url"],))
        self.connection.executemany(
            """
            INSERT INTO passages(
                document_url, ordinal, text, evidence_id, start_offset, end_offset
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (item["url"], ordinal, passage, evidence_id, start, end)
                for ordinal, (passage, evidence_id, start, end) in enumerate(passage_records)
            ],
        )
        self.connection.commit()
        return item

    def close_spider(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None


def _make_passages(chunks: list[str], max_chars: int = 1200) -> list[str]:
    passages: list[str] = []
    current: list[str] = []
    current_len = 0

    is_markdown = any("\n" in c for c in chunks)
    sep = "\n\n" if is_markdown else " "

    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue

        if len(chunk) > max_chars:
            if current:
                passages.append(sep.join(current))
                current = []
                current_len = 0

            if "\n" in chunk:
                lines = chunk.splitlines()
                sub_current: list[str] = []
                sub_len = 0
                for line in lines:
                    line_len = len(line)
                    if sub_current and (sub_len + line_len + 1 > max_chars):
                        passages.append("\n".join(sub_current))
                        sub_current = [line]
                        sub_len = line_len
                    else:
                        sub_current.append(line)
                        sub_len += line_len + (1 if sub_len else 0)
                if sub_current:
                    passages.append("\n".join(sub_current))
            else:
                sub_chunk = chunk
                while len(sub_chunk) > max_chars:
                    boundary = sub_chunk.rfind(" ", 0, max_chars + 1)
                    boundary = boundary if boundary > 0 else max_chars
                    passages.append(sub_chunk[:boundary].strip())
                    sub_chunk = sub_chunk[boundary:].strip()
                if sub_chunk:
                    passages.append(sub_chunk)
            continue

        chunk_len = len(chunk)
        if current and (current_len + chunk_len + len(sep) > max_chars):
            passages.append(sep.join(current))
            current = [chunk]
            current_len = chunk_len
        else:
            current.append(chunk)
            current_len += chunk_len + (len(sep) if current_len else 0)

    if current:
        passages.append(sep.join(current))

    return passages


def _clean_blocks(blocks: list[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for block in blocks:
        normalized = " ".join(block.split())
        if normalized and normalized not in seen:
            cleaned.append(normalized)
            seen.add(normalized)
    return cleaned


def _passage_records(
    document_url: str, text: str, blocks: list[str] | None = None
) -> list[tuple[str, str, int, int]]:
    records: list[tuple[str, str, int, int]] = []
    cursor = 0
    chunks_to_use = blocks if (blocks and any("\n" in b for b in blocks)) else [text]
    for passage in _make_passages(chunks_to_use):
        passage = passage.strip()
        if not passage:
            continue
        start = text.find(passage, cursor)
        if start < 0:
            start = text.find(passage)
        if start < 0:
            start = cursor
            end = min(len(text), cursor + len(passage))
        else:
            end = start + len(passage)
        evidence_id = hashlib.sha256(
            "\0".join((document_url, str(start), passage)).encode("utf-8")
        ).hexdigest()
        records.append((passage, evidence_id, start, end))
        cursor = end
    return records
