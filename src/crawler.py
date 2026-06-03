"""
Playwright-based web page crawler with stealth fallback.
Fetches pages using a real browser, handles JS rendering, 
redirects, and bot detection.

Stealth is "sticky" per domain: once bot detection triggers stealth
for any page on a domain, ALL subsequent requests to that domain
automatically use stealth from the start.
"""

import asyncio
import time
import re
import logging
from typing import Optional
from urllib.parse import urlparse

from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from playwright_stealth import Stealth

from src.validators import CrawledPage

logger = logging.getLogger(__name__)


# ── Markers that indicate bot protection ──
BOT_DETECTION_MARKERS = [
    "cf-browser-verification",   # Cloudflare
    "challenge-platform",        # Cloudflare challenge
    "just a moment",             # Cloudflare "Just a moment..."
    "checking your browser",     # Generic bot check
    "enable javascript",         # JS-required page
    "access denied",             # WAF block
    "captcha",                   # CAPTCHA
]

# ── Auth wall indicators in HTML ──
AUTH_WALL_PATTERNS = [
    re.compile(r'<input[^>]+type=["\']password["\']', re.IGNORECASE),
    re.compile(r'<form[^>]+login', re.IGNORECASE),
    re.compile(r'<form[^>]+signin', re.IGNORECASE),
    re.compile(r'sign\s*in\s*to\s*(your|the)', re.IGNORECASE),
]


class PlaywrightCrawler:
    """
    Fetches web pages using Playwright with headful browser.
    
    Strategy:
    1. First attempt: Standard Playwright (Chromium)
    2. If bot-detected (403, challenge page): Retry with playwright-stealth
    3. If still blocked: Log warning, skip page
    """

    def __init__(self, headless: bool = False, timeout_ms: int = 30000):
        """
        Args:
            headless: If False, browser window is visible (headful mode).
            timeout_ms: Max time to wait for page load (default 30s).
        """
        self.headless = headless
        self.timeout_ms = timeout_ms
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._stealth_domains: set[str] = set()  # Domains that need stealth
        self._page_cache: dict[str, Optional[CrawledPage]] = {}  # URL -> result cache
        self._failed_urls: set[str] = set()  # URLs that already failed — skip rerequests

    async def start(self):
        """Launch browser and create context."""
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
            ]
        )
        self._context = await self._browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            java_script_enabled=True,
        )
        logger.info("Browser started (headless=%s)", self.headless)

    async def stop(self):
        """Close browser and cleanup."""
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("Browser stopped")

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.stop()

    @staticmethod
    def _get_domain(url: str) -> str:
        """Extract domain from URL for stealth tracking."""
        return urlparse(url).netloc.lower()

    async def fetch_page(self, url: str, depth: int = 0) -> Optional[CrawledPage]:
        """
        Fetch a single page. Returns CrawledPage or None on failure.
        
        Flow:
        1. Check if domain is already marked for stealth
        2. If yes -> stealth directly (no wasted standard attempt)
        3. If no -> standard fetch first, stealth fallback on bot detection
        4. If stealth triggered -> remember domain for all future requests
        """
        # ── Cache hit: already fetched this URL ──
        if url in self._page_cache:
            logger.debug("Cache hit for %s", url)
            return self._page_cache[url]

        if url in self._failed_urls:
            logger.debug("Skipping known-failed URL: %s", url)
            return None

        domain = self._get_domain(url)

        # Domain already known to need stealth — skip standard attempt
        if domain in self._stealth_domains:
            logger.info("Domain '%s' requires stealth — using directly", domain)
            result = await self._try_fetch(url, depth, use_stealth=True)
            if result is None:
                logger.error("Stealth fetch failed for %s", url)
                self._failed_urls.add(url)
            else:
                self._page_cache[url] = result
            return result

        # Attempt 1: Standard fetch
        result = await self._try_fetch(url, depth, use_stealth=False)

        if result is None:
            logger.warning("Standard fetch failed for %s, trying stealth...", url)
            # Attempt 2: Stealth fetch
            result = await self._try_fetch(url, depth, use_stealth=True)

            if result is not None:
                # Stealth worked — mark this domain for all future requests
                self._stealth_domains.add(domain)
                logger.info(
                    "Domain '%s' added to stealth list — all future requests will use stealth",
                    domain
                )

        if result is None:
            logger.error("All fetch attempts failed for %s", url)
            self._failed_urls.add(url)
        else:
            self._page_cache[url] = result

        return result

    async def _try_fetch(
        self, url: str, depth: int, use_stealth: bool
    ) -> Optional[CrawledPage]:
        """Single fetch attempt with optional stealth."""
        page: Optional[Page] = None
        start_time = time.perf_counter()

        try:
            page = await self._context.new_page()

            # Apply stealth patches if requested
            if use_stealth:
                await Stealth().apply_stealth_async(page)
                logger.info("Stealth mode applied for %s", url)

            # Navigate
            response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=self.timeout_ms,
            )

            if response is None:
                logger.warning("No response from %s", url)
                return None

            status_code = response.status

            # Smart wait: use networkidle instead of fixed 2s delay
            # Most pages settle in <500ms; only JS-heavy ones need longer
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                # Timeout is fine — page still has useful content
                await page.wait_for_timeout(500)

            # Get page content
            html = await page.content()
            title = await page.title()
            final_url = page.url
            content_type = response.headers.get("content-type", "text/html")

            # Ensure content-type includes text/html
            if "text/html" not in content_type.lower():
                # Playwright always returns HTML DOM, so force it
                content_type = "text/html; charset=utf-8"

            fetch_time_ms = int((time.perf_counter() - start_time) * 1000)

            # Check for bot detection
            if self._is_bot_blocked(html, status_code) and not use_stealth:
                logger.warning("Bot detection triggered on %s", url)
                return None

            # Check for auth walls
            if self._is_auth_wall(html, final_url):
                logger.warning("Auth wall detected on %s -- skipping", url)
                return None

            # Validate with Pydantic model
            try:
                crawled = CrawledPage(
                    url=url,
                    final_url=final_url,
                    status_code=status_code,
                    content_type=content_type,
                    html=html,
                    title=title,
                    fetch_time_ms=fetch_time_ms,
                    depth=depth,
                )
                return crawled
            except Exception as e:
                logger.warning("Page validation failed for %s: %s", url, str(e)[:200])
                return None

        except Exception as e:
            logger.warning("Fetch error for %s: %s", url, e)
            return None
        finally:
            if page:
                await page.close()

    def _is_bot_blocked(self, html: str, status_code: int) -> bool:
        """Detect if the page is a bot-challenge or block page."""
        if status_code == 403:
            return True

        html_lower = html.lower()
        for marker in BOT_DETECTION_MARKERS:
            if marker in html_lower:
                return True

        return False

    def _is_auth_wall(self, html: str, url: str) -> bool:
        """Detect if the page requires authentication."""
        # Check URL path
        url_lower = url.lower()
        auth_segments = {"login", "signin", "sign-in", "sso", "cas", "auth", "saml"}
        for segment in auth_segments:
            if f"/{segment}" in url_lower:
                return True

        # Check HTML for login forms
        for pattern in AUTH_WALL_PATTERNS:
            if pattern.search(html):
                return True

        return False

    async def fetch_pages_parallel(
        self, urls: list[str], max_concurrent: int = 3, depth: int = 0
    ) -> list[CrawledPage]:
        """
        Fetch multiple pages with bounded concurrency.
        Uses asyncio.Semaphore to limit concurrent browser tabs.
        
        Args:
            urls: List of URLs to fetch
            max_concurrent: Max simultaneous tabs
            depth: Crawl depth for these pages
            
        Returns:
            List of successfully fetched CrawledPage objects
        """
        semaphore = asyncio.Semaphore(max_concurrent)
        results = []

        async def _fetch_with_semaphore(url: str):
            async with semaphore:
                # Polite delay between requests
                await asyncio.sleep(1)
                return await self.fetch_page(url, depth=depth)

        tasks = [_fetch_with_semaphore(u) for u in urls]
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)

        for url, result in zip(urls, raw_results):
            if isinstance(result, Exception):
                logger.error("Exception fetching %s: %s", url, result)
            elif result is not None:
                results.append(result)
            else:
                logger.warning("No result for %s", url)

        logger.info("Fetched %d/%d pages successfully", len(results), len(urls))
        return results
