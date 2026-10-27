from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from services.common.config import settings
from services.common.models import FailedFetchRecord, RawPageRecord
from services.fetcher.security import is_ssrf_safe_url

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


class PlaywrightBrowserFetcher:
    """Headless browser fetcher managing a bounded pool of concurrent Playwright workers.

    Defaults to 3 concurrent browser workers (scalable via settings.playwright_workers)
    to render JavaScript-heavy SPAs and bypass basic client-side bot checks.
    """

    def __init__(
        self,
        headless: bool = True,
        timeout_ms: int | None = None,
        max_workers: int | None = None,
        proxy_url: str | None = None,
        recycle_after_renders: int | None = None,
    ) -> None:
        self.headless = headless
        self.timeout_ms = timeout_ms or settings.playwright_timeout_ms
        self.max_workers = max_workers or settings.playwright_workers
        self.proxy_url = proxy_url or settings.proxy_url
        self.recycle_after_renders = recycle_after_renders or settings.recycle_browser_after
        self._playwright: Any = None
        self._browser: Any = None
        self._semaphore: asyncio.Semaphore | None = None
        self._active_workers: int = 0
        self._total_rendered: int = 0
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._lock = asyncio.Lock()

    def _get_semaphore(self) -> asyncio.Semaphore:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.max_workers)
        return self._semaphore

    async def _recycle_browser(self) -> None:
        """Gracefully close the current Chromium browser so a fresh, clean instance is spawned."""
        async with self._lock:
            if self._browser:
                try:
                    await self._browser.close()
                except Exception as exc:
                    logger.debug("Browser recycling close exception: %s", exc)
                self._browser = None
                logger.info("Playwright browser instance recycled successfully.")

    async def _ensure_browser(self) -> Any:
        if self._browser is None:
            async with self._lock:
                if self._browser is None:
                    try:
                        from playwright.async_api import async_playwright

                        self._playwright = await async_playwright().start()
                        launch_kwargs: dict[str, Any] = {
                            "headless": self.headless,
                            "args": [
                                "--disable-dev-shm-usage",
                                "--no-sandbox",
                                "--disable-blink-features=AutomationControlled",
                                "--disable-infobars",
                            ],
                        }
                        if self.proxy_url:
                            launch_kwargs["proxy"] = {"server": self.proxy_url}

                        self._browser = await self._playwright.chromium.launch(**launch_kwargs)
                        logger.info(
                            "Initialized Playwright Chromium engine with pool of %d browser workers (proxy=%s)",
                            self.max_workers,
                            bool(self.proxy_url),
                        )
                    except ImportError as err:
                        raise RuntimeError(
                            "Playwright is not installed. Run `pip install playwright && playwright install chromium`"
                        ) from err
        return self._browser

    async def fetch(self, url: str) -> tuple[RawPageRecord | None, FailedFetchRecord | None]:
        """Fetch a webpage using an isolated Playwright browser worker slot with SSRF & timeout guards."""
        if settings.enable_ssrf_protection:
            safe, reason = is_ssrf_safe_url(url)
            if not safe:
                return None, FailedFetchRecord(
                    url=url,
                    error_type="ssrf_blocked",
                    error_message=f"SSRF Protection blocked URL: {reason}",
                )

        start_time = time.monotonic()
        sem = self._get_semaphore()

        async with sem:
            self._active_workers += 1
            context = None
            try:
                browser = await self._ensure_browser()
                context_kwargs: dict[str, Any] = {
                    "user_agent": DEFAULT_USER_AGENT,
                    "viewport": {"width": 1920, "height": 1080},
                    "locale": "en-US",
                }
                if self.proxy_url:
                    context_kwargs["proxy"] = {"server": self.proxy_url}

                context = await browser.new_context(**context_kwargs)
                page = await context.new_page()

                # Mask webdriver flag to prevent bot blocking on JS apps
                await page.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
                )

                response = await page.goto(
                    url,
                    timeout=self.timeout_ms,
                    wait_until="domcontentloaded",
                )

                # Brief wait for client-side JavaScript hydration if needed
                with contextlib.suppress(Exception):
                    await page.wait_for_load_state("networkidle", timeout=2500)

                status_code = response.status if response else 200
                rendered_html = await page.content()
                canonical_url = page.url or url
                elapsed = time.monotonic() - start_time
                self._total_rendered += 1

                if status_code >= 400:
                    return None, FailedFetchRecord(
                        url=url,
                        error_type=f"http_{status_code}",
                        error_message=f"Playwright worker received status {status_code}",
                    )

                record = RawPageRecord(
                    url=url,
                    canonical_url=canonical_url,
                    status_code=status_code,
                    raw_html=rendered_html,
                    fetcher_engine="playwright",
                    elapsed_seconds=elapsed,
                )
                return record, None

            except Exception as error:
                elapsed = time.monotonic() - start_time
                return None, FailedFetchRecord(
                    url=url,
                    error_type="browser_render_error",
                    error_message=f"Playwright render failed after {elapsed:.1f}s: {error}",
                )
            finally:
                self._active_workers -= 1
                if context:
                    with contextlib.suppress(Exception):
                        await context.close()

                # Recycle browser process periodically when idle to reclaim memory
                if (
                    self._total_rendered > 0
                    and (self._total_rendered % self.recycle_after_renders == 0)
                    and self._active_workers <= 0
                ):
                    recycle_task = asyncio.create_task(self._recycle_browser())
                    self._background_tasks.add(recycle_task)
                    recycle_task.add_done_callback(self._background_tasks.discard)

    def get_stats(self) -> dict[str, Any]:
        """Return live metrics for the Playwright worker pool."""
        return {
            "max_workers": self.max_workers,
            "active_workers": self._active_workers,
            "total_rendered": self._total_rendered,
            "browser_online": self._browser is not None,
        }

    async def close(self) -> None:
        """Gracefully terminate browser instances and Playwright runtime."""
        async with self._lock:
            if self._browser:
                try:
                    await self._browser.close()
                except Exception as exc:
                    logger.warning("Error closing Playwright browser: %s", exc)
                self._browser = None
            if self._playwright:
                try:
                    await self._playwright.stop()
                except Exception as exc:
                    logger.warning("Error stopping Playwright runtime: %s", exc)
                self._playwright = None
