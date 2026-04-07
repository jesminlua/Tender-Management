import asyncio
import logging
import os
from contextlib import asynccontextmanager
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

log = logging.getLogger(__name__)

MAX_TABS = int(os.getenv("MAX_BROWSER_TABS", "5"))
_semaphore = None
_playwright = None
_browser = None
_stealth = Stealth()


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
            ignore_https_errors=True,
        )

        if cookies:
            await context.add_cookies(cookies)

        page = await context.new_page()

        # Apply stealth to every page — hides all Playwright bot signals
        await _stealth.apply_stealth_async(page)

        try:
            yield context, page
        finally:
            await context.close()
