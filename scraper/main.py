"""
main.py — Tender Agent orchestrator

Runs all configured sites concurrently (bounded by MAX_TABS),
handles login, pagination, extraction, deduplication, and Supabase upsert.

Usage:
    python main.py               # scrape all active sites
    python main.py --site "UK Find a Tender"   # scrape one site by name
    python main.py --dry-run     # scrape but don't write to DB
"""

import asyncio
import argparse
import logging
import os
import sys
from datetime import datetime

import db
import extractor
import login as login_mod
import pagination as pag_mod
from browser_pool import init_browser, close_browser, acquire_tab
from notify import send_digest

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("scraper.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

API_KEY = os.environ["ANTHROPIC_API_KEY"]


# ══════════════════════════════════════════════════════════════════════════════
# SINGLE SITE SCRAPER
# ══════════════════════════════════════════════════════════════════════════════

async def scrape_site(site: dict, seen_fps: set[str], supabase, dry_run: bool) -> list[dict]:
    """
    Fully scrape one site: login → paginate → extract → deduplicate → upsert.
    Returns list of newly found tenders.
    """
    site_name = site["name"]
    run_id    = None if dry_run else db.start_run(supabase, site["id"])

    pages_scraped = 0
    all_new: list[dict] = []
    error_msg = None

    try:
        # Load any saved session cookies
        saved_cookies = db.load_cookies(supabase, site["id"]) if not dry_run else None

        async with acquire_tab(cookies=saved_cookies) as (context, page):

            # ── Login ─────────────────────────────────────────────────────────
            logged_in = await login_mod.ensure_logged_in(page, context, site, supabase)
            if not logged_in:
                raise RuntimeError(f"Login failed for {site_name}")

            # Navigate to the listing URL (login may have redirected elsewhere)
            listing_url = site["url"]
            if page.url != listing_url:
                await page.goto(listing_url, wait_until="networkidle", timeout=30_000)

            delay_s = site.get("delay_ms", 2000) / 1000

            # ── Pagination loop ───────────────────────────────────────────────
            async for page_num in pag_mod.paginate(page, site):
                current_url = page.url
                log.info(f"  [{site_name}] Page {page_num}: {current_url}")

                html     = await page.content()
                tenders  = extractor.extract_from_html(html, current_url, API_KEY)
                pages_scraped += 1

                # Annotate and deduplicate
                new_this_page = []
                for t in tenders:
                    fp = t.get("fingerprint") or extractor.fingerprint(t)
                    if fp not in seen_fps:
                        seen_fps.add(fp)
                        t.update({
                            "fingerprint": fp,
                            "site_id":     site["id"],
                            "source_site": site_name,
                            "scraped_at":  datetime.utcnow().isoformat(),
                        })
                        new_this_page.append(t)

                log.info(f"  [{site_name}] Page {page_num}: {len(new_this_page)} new "
                         f"(of {len(tenders)} extracted)")

                # Stop paginating if this page yielded nothing new
                if not new_this_page and page_num > 1:
                    log.info(f"  [{site_name}] No new tenders on page {page_num} — stopping.")
                    break

                all_new.extend(new_this_page)

                # Write to DB in batches of 50 to avoid large payloads
                if not dry_run and len(all_new) >= 50:
                    db.upsert_tenders(supabase, all_new)
                    all_new = []

                await asyncio.sleep(delay_s)

        # Final batch write
        if not dry_run and all_new:
            db.upsert_tenders(supabase, all_new)

        status = "success"

    except Exception as e:
        error_msg = str(e)
        log.error(f"[{site_name}] FAILED: {e}", exc_info=True)
        status = "error"

    finally:
        if run_id:
            db.finish_run(supabase, run_id, status, pages_scraped, len(all_new), error_msg)

    return all_new


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

async def main(site_filter: str = None, dry_run: bool = False):
    log.info("=" * 60)
    log.info(f"Tender Agent starting — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    if dry_run:
        log.info("DRY RUN — no data will be written to the database.")

    supabase = db.get_client()
    sites    = db.fetch_active_sites(supabase)

    if site_filter:
        sites = [s for s in sites if site_filter.lower() in s["name"].lower()]
        if not sites:
            log.error(f"No active site matching '{site_filter}'")
            return []

    log.info(f"Sites to scrape: {[s['name'] for s in sites]}")

    # Pre-load all known fingerprints for deduplication
    seen_fps = db.fetch_seen_fingerprints(supabase)
    log.info(f"Known tenders in DB: {len(seen_fps)}")

    await init_browser()

    # Scrape all sites concurrently — tab limit is enforced by the semaphore
    tasks = [
        scrape_site(site, seen_fps, supabase, dry_run)
        for site in sites
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    await close_browser()

    all_new = []
    for site, result in zip(sites, results):
        if isinstance(result, Exception):
            log.error(f"[{site['name']}] Unhandled exception: {result}")
        else:
            all_new.extend(result)

    log.info(f"Run complete — {len(all_new)} new tenders across {len(sites)} site(s).")

    # Send email digest if configured
    if all_new and not dry_run:
        send_digest(all_new)

    return all_new


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--site",    help="Filter to a specific site name")
    parser.add_argument("--dry-run", action="store_true", help="Don't write to DB")
    args = parser.parse_args()

    asyncio.run(main(site_filter=args.site, dry_run=args.dry_run))
