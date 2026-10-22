from __future__ import annotations

import logging
import time

import httpx

from services.common.config import settings
from services.common.models import FailedFetchRecord, RawPageRecord

logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36 (AgentSearchBot/2.0; +https://github.com/agent-search)"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
}


class AsyncHttpFetcher:
    """High-throughput async HTTP fetcher with connection pooling and fail-fast timeouts."""

    def __init__(
        self,
        max_connections: int | None = None,
        max_keepalive: int | None = None,
        timeout: float | None = None,
    ) -> None:
        self.limits = httpx.Limits(
            max_connections=max_connections or settings.max_connections,
            max_keepalive_connections=max_keepalive or settings.max_keepalive,
            keepalive_expiry=10.0,
        )
        self.timeout = httpx.Timeout(
            connect=settings.connect_timeout,
            read=timeout or settings.read_timeout,
            write=10.0,
            pool=5.0,
        )
        self._client: httpx.AsyncClient | None = None

    async def get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                limits=self.limits,
                timeout=self.timeout,
                headers=DEFAULT_HEADERS,
                follow_redirects=True,
                http2=True,
            )
        return self._client

    async def fetch(self, url: str) -> tuple[RawPageRecord | None, FailedFetchRecord | None]:
        """Fetch URL without throwing unhandled exceptions.

        Returns either (RawPageRecord, None) or (None, FailedFetchRecord).
        """
        client = await self.get_client()
        start_time = time.monotonic()
        try:
            response = await client.get(url)
            elapsed = time.monotonic() - start_time

            # Filter non-HTML content types (e.g. binary downloads, PDFs)
            content_type = response.headers.get("content-type", "").lower()
            if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
                return None, FailedFetchRecord(
                    url=url,
                    error_type="unsupported_content_type",
                    error_message=f"Ignored content-type: {content_type}",
                )

            if response.status_code >= 400:
                return None, FailedFetchRecord(
                    url=url,
                    error_type=f"http_{response.status_code}",
                    error_message=f"HTTP {response.status_code}: {response.reason_phrase}",
                )

            record = RawPageRecord(
                url=url,
                canonical_url=str(response.url),
                status_code=response.status_code,
                headers=dict(response.headers),
                raw_html=response.text,
                fetcher_engine="httpx",
                elapsed_seconds=elapsed,
            )
            return record, None

        except httpx.TimeoutException as error:
            return None, FailedFetchRecord(
                url=url,
                error_type="timeout",
                error_message=str(error),
            )
        except httpx.ConnectError as error:
            return None, FailedFetchRecord(
                url=url,
                error_type="connection_error",
                error_message=str(error),
            )
        except Exception as error:
            return None, FailedFetchRecord(
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
