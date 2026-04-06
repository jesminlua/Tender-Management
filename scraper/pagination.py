"""
pagination.py — Universal pagination engine
"""

import asyncio
import logging
import re
from playwright.async_api import Page

log = logging.getLogger(__name__)


async def paginate(page: Page, site: dict):
    cfg       = site.get("pagination", {})
    max_pages = int(cfg.get("max_pages", 20))
    tab_urls  = site.get("tab_urls", [])

    if tab_urls:
        for tab_num, tab_url in enumerate(tab_urls, 1):
            log.info(f"  Tab {tab_num}/{len(tab_urls)}: {tab_url}")
            await page.goto(tab_url, wait_until="networkidle", timeout=30_000)
            await _wait_for_content(page, site)
            async for page_num in _paginate_strategy(page, site, max_pages):
                yield page_num
        return

    async for page_num in _paginate_strategy(page, site, max_pages):
        yield page_num


async def _paginate_strategy(page: Page, site: dict, max_pages: int):
    cfg      = site.get("pagination", {})
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
        log.warning(f"Unknown pagination strategy '{strategy}' — scraping single page.")
        yield 1


async def _next_button(page: Page, cfg: dict, max_pages: int):
    next_sel = cfg.get(
        "next_sele
