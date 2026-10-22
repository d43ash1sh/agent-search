from __future__ import annotations

import asyncio
import logging

from services.common.hashing import compute_sha256, compute_simhash
from services.common.models import CleanDocument, RawPageRecord
from services.extractor.chunker import chunk_document
from services.extractor.dedup_filter import DeduplicationFilter
from services.extractor.fallback_cleaner import FallbackCleaner
from services.extractor.trafilatura_cleaner import TrafilaturaCleaner
from services.queue.base import AbstractQueueBroker
from services.queue.redis_broker import RedisQueueBroker

logger = logging.getLogger(__name__)


class ExtractorWorker:
    """CPU-bound worker that extracts, chunks, and deduplicates raw pages."""

    def __init__(self, broker: AbstractQueueBroker) -> None:
        self.broker = broker
        self.trafilatura = TrafilaturaCleaner()
        self.fallback = FallbackCleaner()
        self.dedup = DeduplicationFilter()
        self.is_running = False

    @staticmethod
    def extract_internal_links(html: str, base_url: str, allowed_domain: str) -> list[str]:
        import re
        from urllib.parse import urljoin, urlsplit

        from crawler.urls import host_is_allowed, normalize_url

        found: set[str] = set()
        hrefs = re.findall(r"""<a\b[^>]*href=["']([^"'#]+)["']""", html, flags=re.IGNORECASE)
        for raw_href in hrefs:
            full = urljoin(base_url, raw_href)
            normalized = normalize_url(full)
            if not normalized:
                continue

            ext = normalized.split("?")[0].split(".")[-1].lower()
            if ext in {"png", "jpg", "jpeg", "gif", "svg", "css", "js", "pdf", "zip", "exe", "tar", "gz"}:
                continue

            if allowed_domain:
                if host_is_allowed(normalized, [allowed_domain]):
                    found.add(normalized)
            else:
                base_host = (urlsplit(base_url).hostname or "").lower()
                if (urlsplit(normalized).hostname or "").lower() == base_host:
                    found.add(normalized)

        return list(found)

    def process_record(self, raw: RawPageRecord) -> CleanDocument | None:
        """Process a single raw HTML payload into a CleanDocument."""
        extracted = self.trafilatura.extract(raw.raw_html, raw.url)
        if not extracted:
            extracted = self.fallback.extract(raw.raw_html, raw.url)

        if not extracted or not extracted["clean_text"]:
            logger.info("No extractable text found in %s", raw.url)
            return None

        clean_text = extracted["clean_text"]
        is_dup, reason = self.dedup.is_duplicate(clean_text)
        if is_dup:
            logger.info("Dropping %s: %s", raw.url, reason)
            return None

        self.dedup.register(clean_text)
        passages = chunk_document(raw.canonical_url, clean_text)

        return CleanDocument(
            url=raw.url,
            canonical_url=raw.canonical_url,
            title=extracted["title"],
            author=extracted.get("author"),
            published_date=extracted.get("published_date"),
            sitename=extracted.get("sitename"),
            description=extracted.get("description"),
            clean_text=clean_text,
            markdown=extracted.get("markdown") or clean_text,
            passages=passages,
            content_hash=compute_sha256(clean_text),
            simhash=compute_simhash(clean_text),
        )

    async def run_once(self) -> bool:
        """Process one item from the staging queue. Returns True if an item was processed."""
        raw = await self.broker.dequeue_raw(timeout=1.0)
        if not raw:
            return False

        try:
            doc = self.process_record(raw)
            if doc:
                await self.broker.enqueue_document(doc)

            # Recursive crawling link extraction & queueing
            if raw.metadata and raw.metadata.get("recursive"):
                current_depth = int(raw.metadata.get("depth", 0))
                max_depth = int(raw.metadata.get("max_depth", 1))
                allowed_domain = str(raw.metadata.get("allowed_domain", ""))
                render_js = bool(raw.metadata.get("render_js", False))

                if current_depth < max_depth:
                    child_links = self.extract_internal_links(raw.raw_html, raw.url, allowed_domain)
                    from services.common.models import FetchRequest

                    for link in child_links:
                        child_meta = dict(raw.metadata)
                        child_meta["depth"] = current_depth + 1
                        await self.broker.enqueue_fetch(
                            FetchRequest(
                                url=link,
                                depth=current_depth + 1,
                                render_js=render_js,
                                metadata=child_meta,
                            )
                        )
            return True
        except Exception as error:
            logger.error("Extraction error for %s: %s", raw.url, error, exc_info=True)
            return False

    async def run_loop(self) -> None:
        """Continuously process items from the staging queue."""
        self.is_running = True
        logger.info("Extractor worker started.")
        while self.is_running:
            processed = await self.run_once()
            if not processed:
                await asyncio.sleep(0.1)

    def stop(self) -> None:
        self.is_running = False


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    broker = RedisQueueBroker()
    worker = ExtractorWorker(broker=broker)
    try:
        asyncio.run(worker.run_loop())
    except KeyboardInterrupt:
        logger.info("Extractor worker stopped by user.")


if __name__ == "__main__":
    main()
