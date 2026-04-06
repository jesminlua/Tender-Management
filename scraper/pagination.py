import asyncio
import logging
import re
from playwright.async_api import Page

log = logging.getLogger(__name__)


async def paginate(page, site):
    cfg = site.get("pagination", {})
    max_pages = int(cfg.get("max_pages", 20))
    tab_urls = site.get("tab_urls", [])
    if tab_urls:
        for tab_url in tab_urls:
            await page.goto(tab_url, wait_until="networkidle", timeout=30000)
            sel = site.get("wait_for_selector")
            if sel:
                try:
                    await page.wait_for_selector(sel, timeout=15000)
                except Exception:
                    pass
            async for p in _paginate_strategy(page, site, max_pages):
                yield p
        return
    async for p in _paginate_strategy(page, site, max_pages):
        yield p


async def _paginate_strategy(page, site, max_pages):
    strategy = site.get("pagination", {}).get("strategy", "none")
    if strategy == "next_button":
        async for p in _next_button(page, site.get("pagination", {}), max_pages):
            yield p
    elif strategy == "url_param":
        async for p in _url_param(page, site, site.get("pagination", {}), max_pages):
            yield p
    elif strategy == "load_more":
        async for p in _load_more(page, site.get("pagination", {}), max_pages):
            yield p
    elif strategy == "infinite":
        async for p in _infinite_scroll(page, site.get("pagination", {}), max_pages):
            yield p
    else:
        yield 1


async def _next_button(page, cfg, max_pages):
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
                visible = await btn.is_visible(timeout=3000)
                if not visible:
                    continue
                enabled = await btn.is_enabled()
                if not enabled:
                    return
                current_url = page.url
                await btn.click()
                await page.wait_for_load_state("networkidle", timeout=20000)
                await asyncio.sleep(cfg.get("delay_ms", 1500) / 1000)
                new_url = page.url
                if new_url == current_url:
                    return
