import asyncio
import logging
import os
from contextlib import asynccontextmanager
from playwright.async_api import async_playwright

log = logging.getLogger(__name__)

MAX_TABS = int(os.getenv("MAX_BROWSER_TABS", "5"))
_semaphore = None
_playwright = None
_browser = None


async def init_browser():
    global _playwright, _browser, _semaphore
    _semaphore = asyncio.Semaphore(MAX_TABS)
    _playwright = await async_playwright().start()
    _browser = await _playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--window-size=1920,1080",
        ],
    )
    log.info(f"Browser launched (max {MAX_TABS} concurrent tabs).")


async def close_browser():
    global _playwright, _browser
    if _browser:
        await _browser.close()
    if _playwright:
        await _playwright.stop()


@asynccontextmanager
async def acquire_tab(cookies=None):
    async with _semaphore:
        context = await _browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="Asia/Kuala_Lumpur",
            extra_http_headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
            },
            java_script_enabled=True,
            ignore_https_errors=True,
        )

        # Remove webdriver fingerprint
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            window.chrome = { runtime: {} };
        """)

        if cookies:
            await context.add_cookies(cookies)

        page = await context.new_page()

        try:
            yield context, page
        finally:
            await context.close()
