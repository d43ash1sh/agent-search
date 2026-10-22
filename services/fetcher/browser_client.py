from __future__ import annotations

import logging
import time
from typing import Any

from services.common.models import FailedFetchRecord, RawPageRecord

logger = logging.getLogger(__name__)


class PlaywrightBrowserFetcher:
    """Headless browser fetcher for JavaScript-heavy SPAs and dynamic web apps."""

    def __init__(self, headless: bool = True, timeout_ms: int = 15000) -> None:
        self.headless = headless
        self.timeout_ms = timeout_ms
        self._playwright: Any = None
        self._browser: Any = None

    async def _ensure_browser(self) -> Any:
        if self._browser is None:
            try:
                from playwright.async_api import async_playwright  # type: ignore[import-not-found]

                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    headless=self.headless,
                    args=["--disable-dev-shm-usage", "--no-sandbox"],
                )
            except ImportError as err:
                raise RuntimeError(
                    "Playwright is not installed. Run `pip install playwright && playwright install chromium`"
                ) from err
        return self._browser

    async def fetch(self, url: str) -> tuple[RawPageRecord | None, FailedFetchRecord | None]:
        start_time = time.monotonic()
        try:
            browser = await self._ensure_browser()
            page = await browser.new_page()
            try:
                response = await page.goto(
                    url,
                    timeout=self.timeout_ms,
                    wait_until="domcontentloaded",
                )
                status_code = response.status if response else 200
                rendered_html = await page.content()
                elapsed = time.monotonic() - start_time

                if status_code >= 400:
                    return None, FailedFetchRecord(
                        url=url,
                        error_type=f"http_{status_code}",
                        error_message=f"Browser received status {status_code}",
                    )

                record = RawPageRecord(
                    url=url,
                    canonical_url=page.url or url,
                    status_code=status_code,
                    raw_html=rendered_html,
                    fetcher_engine="playwright",
                    elapsed_seconds=elapsed,
                )
                return record, None
            finally:
                await page.close()
        except Exception as error:
            return None, FailedFetchRecord(
                url=url,
                error_type="browser_render_error",
                error_message=str(error),
            )

    async def close(self) -> None:
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
