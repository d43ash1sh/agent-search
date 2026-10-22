from __future__ import annotations

import logging
import time
from typing import Any

from services.common.models import FailedFetchRecord, RawPageRecord

logger = logging.getLogger(__name__)


class TlsImpersonationFetcher:
    """Async HTTP fetcher impersonating modern Chrome TLS (JA3/JA4) and HTTP/2 signatures."""

    def __init__(self, browser: str = "chrome124", timeout: float = 12.0) -> None:
        self.browser = browser
        self.timeout = timeout
        self._session: Any = None

    async def get_session(self) -> Any:
        if self._session is None:
            try:
                from curl_cffi.requests import AsyncSession

                self._session = AsyncSession(
                    impersonate=self.browser,
                    timeout=self.timeout,
                    verify=True,
                )
            except ImportError as err:
                raise RuntimeError(
                    "curl_cffi is not installed. Install with `pip install curl_cffi`"
                ) from err
        return self._session

    async def fetch(self, url: str) -> tuple[RawPageRecord | None, FailedFetchRecord | None]:
        """Fetch URL with browser TLS handshake to bypass Cloudflare / anti-bot 403s."""
        start_time = time.monotonic()
        try:
            session = await self.get_session()
            response = await session.get(url)
            elapsed = time.monotonic() - start_time

            status_code = response.status_code
            if status_code >= 400:
                return None, FailedFetchRecord(
                    url=url,
                    error_type=f"http_{status_code}",
                    error_message=f"TLS Fetcher received HTTP {status_code}",
                )

            record = RawPageRecord(
                url=url,
                canonical_url=str(response.url or url),
                status_code=status_code,
                headers=dict(response.headers),
                raw_html=response.text,
                fetcher_engine="httpx",  # tagged for compatibility
                elapsed_seconds=elapsed,
            )
            return record, None

        except Exception as error:
            return None, FailedFetchRecord(
                url=url,
                error_type="tls_fetch_error",
                error_message=str(error),
            )

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None
