"""
pagination.py — Universal pagination engine

Supported strategies:
  1. none          — single page, no pagination
  2. next_button   — click a "Next" link/button until it disappears
  3. url_param     — increment ?page=N or ?offset=N in the URL
  4. load_more     — click a "Load more" / "Show more" button repeatedly
  5. infinite      — scroll to bottom to trigger lazy loading
  6. tab_links     — site has multiple category/tab links to visit in sequence

Each strategy yields successive (url_or_action, page_number) states.
The scraper calls advance() after processing each page.
"""

import asyncio
import logging
import re
from typing import AsyncGenerator, Optional
from playwright.async_api import Page

log = logging.getLogger(__name__)


async def paginate(page: Page, site: dict) -> AsyncGenerator[int, None]:
    """
    Async generator. Caller does:

        async for page_num in paginate(page, site):
            html = await page.content()
            tenders = extract(html)
            if not tenders:
                break   # stop early if page is empty

    The generator navigates the page between yields.
    page_num is 1-indexed for logging.
    """
    cfg      = site.get("pagination", {})
    strategy = cfg.get("strategy", "none")
    max_pages = int(cfg.get("max_pages", 20))

    # ── Tab links — visit each tab URL before paginating within ──────────────
    tab_urls = site.get("tab_urls", [])
    if tab_urls:
        for tab_num, tab_url in enumerate(tab_urls, 1):
            log.info(f"  Tab {tab_num}/{len(tab_urls)}: {tab_url}")
            await page.goto(tab_url, wait_until="networkidle", timeout=30_000)
            await _wait_for_content(page, site)
            async for page_num in _paginate_strategy(page, site, max_pages):
                yield page_num
        return

    # ── Normal single-section pagination ─────────────────────────────────────
    async for page_num in _paginate_strategy(page, site, max_pages):
        yield page_num


async def _paginate_strategy(page: Page, site: dict, max_pages: int):
    cfg      = site.get("pagination", {})
    strategy = cfg.get("strategy", "none")

    if strategy == "none":
        yield 1

    elif strategy == "next_button":
        yield from await _next_button(page, cfg, max_pages)

    elif strategy == "url_param":
        yield from await _url_param(page, site, cfg, max_pages)

    elif strategy == "load_more":
        yield from await _load_more(page, cfg, max_pages)

    elif strategy == "infinite":
        yield from await _infinite_scroll(page, cfg, max_pages)

    else:
        log.warning(f"Unknown pagination strategy '{strategy}' — scraping single page.")
        yield 1


# ── Strategy implementations ──────────────────────────────────────────────────

async def _next_button(page: Page, cfg: dict, max_pages: int):
    """Click Next button repeatedly until it disappears or is disabled."""
    next_sel = cfg.get(
        "next_selector",
        'a[rel="next"], .pagination-next, a.next, button.next, [aria-label="Next page"]',
    )
    prev_url = None

    for page_num in range(1, max_pages + 1):
        yield page_num

        # Find next button
        clicked = False
        for sel in [s.strip() for s in next_sel.split(",")]:
            try:
                btn = page.locator(sel).first
                if await btn.count() == 0:
                    continue
                if not await btn.is_visible(timeout=3_000):
                    continue
                if not await btn.is_enabled():
                    log.info("  Next button disabled — end of pages.")
                    return

                current_url = page.url
                await btn.click()
                await page.wait_for_load_state("networkidle", timeout=20_000)
                await asyncio.sleep(cfg.get("delay_ms", 1500) / 1000)

                # Check URL actually changed (avoid infinite loops)
                if page.url == current_url or page.url == prev_url:
                    log.info("  URL did not change after Next click — stopping.")
                    return

                prev_url = current_url
                clicked = True
                break
            except Exception:
                continue

        if not clicked:
            log.info(f"  No next button after page {page_num} — done.")
            return


async def _url_param(page: Page, site: dict, cfg: dict, max_pages: int):
    """Increment a URL query parameter (page, offset, start, p, etc.)."""
    base_url  = site["url"]
    param     = cfg.get("param", "page")
    start     = int(cfg.get("start", 1))
    increment = int(cfg.get("increment", 1))
    delay_s   = cfg.get("delay_ms", 1500) / 1000

    for page_num in range(1, max_pages + 1):
        value = start + (page_num - 1) * increment

        # Replace existing param or append
        if f"{param}=" in base_url:
            url = re.sub(rf"({re.escape(param)}=)\d+", rf"\g<1>{value}", base_url)
        else:
            sep = "&" if "?" in base_url else "?"
            url = f"{base_url}{sep}{param}={value}"

        await page.goto(url, wait_until="networkidle", timeout=30_000)
        await _wait_for_content(page, site)
        await asyncio.sleep(delay_s)
        yield page_num


async def _load_more(page: Page, cfg: dict, max_pages: int):
    """Click a 'Load More' / 'Show More' button until it disappears."""
    btn_sel = cfg.get(
        "load_more_selector",
        'button:text("Load more"), button:text("Show more"), .load-more, [data-action="load-more"]',
    )
    delay_s = cfg.get("delay_ms", 2000) / 1000
    yield 1   # first page already loaded by caller

    for page_num in range(2, max_pages + 1):
        clicked = False
        for sel in [s.strip() for s in btn_sel.split(",")]:
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible(timeout=3_000):
                    await btn.scroll_into_view_if_needed()
                    await btn.click()
                    await asyncio.sleep(delay_s)
                    clicked = True
                    break
            except Exception:
                continue

        if not clicked:
            log.info(f"  Load More not found after {page_num - 1} clicks — done.")
            return
        yield page_num


async def _infinite_scroll(page: Page, cfg: dict, max_pages: int):
    """Scroll to bottom of page repeatedly to trigger lazy-load."""
    delay_s = cfg.get("delay_ms", 2000) / 1000
    yield 1

    prev_height = 0
    for page_num in range(2, max_pages + 1):
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(delay_s)

        new_height = await page.evaluate("document.body.scrollHeight")
        if new_height == prev_height:
            log.info(f"  Page height stopped growing after scroll {page_num - 1} — done.")
            return
        prev_height = new_height
        yield page_num


async def _wait_for_content(page: Page, site: dict):
    """Wait for a site-specific selector that signals content is loaded."""
    sel = site.get("wait_for_selector")
    if sel:
        try:
            await page.wait_for_selector(sel, timeout=15_000)
        except Exception:
            log.warning(f"  wait_for_selector '{sel}' timed out — continuing anyway.")
