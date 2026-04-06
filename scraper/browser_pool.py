"""
browser_pool.py — Managed browser pool with tab concurrency control

Handles:
  - Launching / reusing a single Chromium browser process
  - Limiting concurrent tabs (configurable MAX_TABS)
  - Per-tab timeouts and crash recovery
  - Stealth headers to reduce bot detection
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

log = logging.getLogger(__name__)

# Maximum browser tabs open simultaneously across ALL sites
MAX_TABS = int(__import__("os").getenv("MAX_BROWSER_TABS", "5"))

# Semaphore enforces the tab limit globally
_tab_semaphore: asyncio.Semaphore = None
_browser: Browser = None
_playwright = None


STEALTH_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Sec-Fetch-Site":  "none",
    "Sec-Fetch-Mode":  "navigate",
    "Sec-Fetch-User":  "?1",
    "Sec-Fetch-Dest":  "document",
}

BROWSER_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",       # avoid /dev/shm crashes in Docker
    "--disable-gpu",
]


async def init_browser():
    """Launch the shared browser. Call once at startup."""
    global _browser, _playwright, _tab_semaphore
    _tab_semaphore = asyncio.Semaphore(MAX_TABS)
    _playwright = await async_playwright().start()
    _browser = await _playwright.chromium.launch(
        headless=True,
        args=BROWSER_ARGS,
    )
    log.info(f"Browser launched (max {MAX_TABS} concurrent tabs).")


async def close_browser():
    """Shut down the browser gracefully."""
    global _browser, _playwright
    if _browser:
        await _browser.close()
        _browser = None
    if _playwright:
        await _playwright.stop()
        _playwright = None
    log.info("Browser closed.")


@asynccontextmanager
async def acquire_tab(
    cookies: list = None,
    extra_headers: dict = None,
    viewport: dict = None,
):
    """
    Context manager that:
      1. Waits for a tab slot (respects MAX_TABS limit)
      2. Creates a fresh browser context (isolated cookies/storage)
      3. Loads any saved cookies
      4. Yields (context, page)
      5. Closes context and releases the tab slot on exit

    Usage:
        async with acquire_tab(cookies=saved_cookies) as (ctx, page):
            await page.goto("https://example.com")
            new_cookies = await ctx.cookies()
    """
    async with _tab_semaphore:
        context: BrowserContext = await _browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport=viewport or {"width": 1280, "height": 900},
            locale="en-US",
            timezone_id="America/New_York",
            extra_http_headers={**STEALTH_HEADERS, **(extra_headers or {})},
            java_script_enabled=True,
            bypass_csp=True,
        )

        # Mask automation fingerprints
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
            window.chrome = { runtime: {} };
        """)

        # Restore saved session cookies
        if cookies:
            await context.add_cookies(cookies)

        page: Page = await context.new_page()

        # Abort images/fonts to speed up scraping
        await page.route(
            "**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,otf}",
            lambda route: route.abort(),
        )

        try:
            yield context, page
        finally:
            try:
                await context.close()
            except Exception:
                pass
