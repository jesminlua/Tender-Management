import asyncio
import re
import logging
from playwright.async_api import Page

log = logging.getLogger(__name__)


async def paginate(page, site):
    cfg = site.get("pagination") or {}
    strategy = cfg.get("strategy", "none")
    max_pages = int(cfg.get("max_pages", 20))
    tab_urls = site.get("tab_urls") or []

    if tab_urls:
        for tab_url in tab_urls:
            await page.goto(tab_url, wait_until="networkidle", timeout=30000)
            await _wait(page, site)
            async for p in _run(page, site, strategy, cfg, max_pages):
                yield p
        return

    async for p in _run(page, site, strategy, cfg, max_pages):
        yield p


async def _run(page, site, strategy, cfg, max_pages):
    if strategy == "url_param":
        async for p in _url_param(page, site, cfg, max_pages):
            yield p
    elif strategy == "next_button":
        async for p in _next_button(page, cfg, max_pages):
            yield p
    elif strategy == "load_more":
        async for p in _load_more(page, cfg, max_pages):
            yield p
    elif strategy == "infinite":
        async for p in _infinite(page, cfg, max_pages):
            yield p
    else:
        yield 1


async def _url_param(page, site, cfg, max_pages):
    base = site["url"]
    param = cfg.get("param", "page")
    start = int(cfg.get("start", 1))
    inc = int(cfg.get("increment", 1))
    delay = cfg.get("delay_ms", 1500) / 1000
    for n in range(1, max_pages + 1):
        val = start + (n - 1) * inc
        if param + "=" in base:
            url = re.sub(param + r"=\d+", param + "=" + str(val), base)
        else:
            sep = "&" if "?" in base else "?"
            url = base + sep + param + "=" + str(val)
        await page.goto(url, wait_until="networkidle", timeout=30000)
        await _wait(page, site)
        await asyncio.sleep(delay)
        yield n


async def _next_button(page, cfg, max_pages):
    sel = cfg.get("next_selector", "a[rel=next]")
    prev = None
    delay = cfg.get("delay_ms", 1500) / 1000
    for n in range(1, max_pages + 1):
        yield n
        found = False
        for s in [x.strip() for x in sel.split(",")]:
            try:
                btn = page.locator(s).first
                if await btn.count() == 0:
                    continue
                if not await btn.is_visible(timeout=3000):
                    continue
                if not await btn.is_enabled():
                    return
                cur = page.url
                await btn.click()
                await page.wait_for_load_state("networkidle", timeout=20000)
                await asyncio.sleep(delay)
                nxt = page.url
                if nxt == cur or nxt == prev:
                    return
                prev = cur
                found = True
                break
            except Exception:
                continue
        if not found:
            return


async def _load_more(page, cfg, max_pages):
    sel = cfg.get("load_more_selector", ".load-more")
    delay = cfg.get("delay_ms", 2000) / 1000
    yield 1
    for n in range(2, max_pages + 1):
        found = False
        for s in [x.strip() for x in sel.split(",")]:
            try:
                btn = page.locator(s).first
                if await btn.count() > 0:
                    if await btn.is_visible(timeout=3000):
                        await btn.scroll_into_view_if_needed()
                        await btn.click()
                        await asyncio.sleep(delay)
                        found = True
                        break
            except Exception:
                continue
        if not found:
            return
        yield n


async def _infinite(page, cfg, max_pages):
    delay = cfg.get("delay_ms", 2000) / 1000
    yield 1
    prev = 0
    for n in range(2, max_pages + 1):
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(delay)
        cur = await page.evaluate("document.body.scrollHeight")
        if cur == prev:
            return
        prev = cur
        yield n


async def _wait(page, site):
    sel = site.get("wait_for_selector")
    if sel:
        try:
            await page.wait_for_selector(sel, timeout=15000)
        except Exception:
            pass
