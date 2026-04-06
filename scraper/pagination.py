import asyncio
import logging
import re
from playwright.async_api import Page

log = logging.getLogger(__name__)


async def paginate(page: Page, site: dict):
    cfg = site.get("pagination", {})
    max_pages = int(cfg.get("max_pages", 20))
    tab_urls = site.get("tab_urls", [])

    if tab_urls:
        for tab_num, tab_url in enumerate(tab_urls, 1):
            log.info(f"Tab {tab_num}/{len(tab_urls)}: {tab_url}")
            await page.goto(tab_url, wait_until="networkidle", timeout=30000)
            await _wait_for_content(page, site)
            async for page_num in _paginate_strategy(page, site, max_pages):
                yield page_num
        return

    async for page_num in _paginate_strategy(page, site, max_pages):
        yield page_num


async def _paginate_strategy(page: Page, site: dict, max_pages: int):
    cfg = site.get("pagination", {})
    strategy = cfg.get("strategy", "none")

    if strategy == "none":
        yield 1
    elif strategy == "next_button":
        async for p in _next_button(page, cfg, max_pages):
            yield p
    elif strategy == "url_param":
        async for p in _url_param(page, site, cfg, max_pages):
            yield p
    elif strategy == "load_more":
        async for p in _load_more(page, cfg, max_pages):
            yield p
    elif strategy == "infinite":
        async for p in _infinite_scroll(page, cfg, max_pages):
            yield p
    else:
        yield 1


async def _next_button(page: Page, cfg: dict, max_pages: int):
    next_sel = cfg.get("next_selector", "a[rel=next]")
    prev_url = None

    for page_num in range(1, max_pages + 1):
        yield page_num
        clicked = False
        for sel in [s.strip() for s in next_sel.split(",")]:
            try:
                btn = page.locator(sel).first
                if await btn.count() == 0:
                    continue
                if not await btn.is_visible(timeout=3000):
                    continue
                if not await btn.is_enabled():
                    return
                current_url = page.url
                await btn.click()
                await page.wait_for_load_state("networkidle", timeout=20000)
                await asyncio.sleep(cfg.get("delay_ms", 1500) / 1000)
                if page.url == current_url
