"""
worker.py — Scrape queue consumer

Runs continuously on Railway/server, polling the scrape_queue table
every 30 seconds. When it finds a pending job, it runs the scraper
for that site and marks the job done.

This is the bridge between the Lovable "Run Scraper" button
(which creates a queue entry via the Edge Function)
and the actual Python scraper.

Start with: python worker.py
"""

import asyncio
import logging
import os
import sys
from datetime import datetime

import db
import extractor
import login as login_mod
import pagination as pag_mod
from browser_pool import init_browser, close_browser, acquire_tab

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WORKER] %(message)s",
    handlers=[
        logging.FileHandler("worker.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "30"))
API_KEY       = os.environ["ANTHROPIC_API_KEY"]


async def process_job(job: dict, supabase) -> int:
    """Run the scraper for a single queued job. Returns number of new tenders."""
    job_id  = job["id"]
    site_id = job["site_id"]

    # Mark job as running
    supabase.table("scrape_queue").update({
        "status":     "running",
        "started_at": datetime.utcnow().isoformat(),
    }).eq("id", job_id).execute()

    # Fetch site config
    res = supabase.table("sites").select("*").eq("id", site_id).single().execute()
    if not res.data:
        log.error(f"Site {site_id} not found — skipping job {job_id}")
        supabase.table("scrape_queue").update({
            "status": "error",
            "error":  "Site not found",
            "finished_at": datetime.utcnow().isoformat(),
        }).eq("id", job_id).execute()
        return 0

    site     = res.data
    seen_fps = db.fetch_seen_fingerprints(supabase)
    all_new  = []
    error    = None

    try:
        saved_cookies = db.load_cookies(supabase, site_id)
        async with acquire_tab(cookies=saved_cookies) as (context, page):
            logged_in = await login_mod.ensure_logged_in(page, context, site, supabase)
            if not logged_in:
                raise RuntimeError("Login failed")

            if page.url != site["url"]:
                await page.goto(site["url"], wait_until="networkidle", timeout=30_000)

            async for page_num in pag_mod.paginate(page, site):
                html    = await page.content()
                tenders = extractor.extract_from_html(html, page.url, API_KEY)

                new_this_page = []
                for t in tenders:
                    fp = t.get("fingerprint") or extractor.fingerprint(t)
                    if fp not in seen_fps:
                        seen_fps.add(fp)
                        t.update({
                            "fingerprint": fp,
                            "site_id":     site_id,
                            "source_site": site["name"],
                            "scraped_at":  datetime.utcnow().isoformat(),
                        })
                        new_this_page.append(t)

                all_new.extend(new_this_page)

                if not new_this_page and page_num > 1:
                    break

                await asyncio.sleep(site.get("delay_ms", 2000) / 1000)

        if all_new:
            db.upsert_tenders(supabase, all_new)

        supabase.table("scrape_queue").update({
            "status":      "done",
            "finished_at": datetime.utcnow().isoformat(),
        }).eq("id", job_id).execute()

    except Exception as e:
        error = str(e)
        log.error(f"Job {job_id} failed: {e}", exc_info=True)
        supabase.table("scrape_queue").update({
            "status":      "error",
            "error":       error,
            "finished_at": datetime.utcnow().isoformat(),
        }).eq("id", job_id).execute()

    return len(all_new)


async def run_worker():
    log.info("Worker started. Polling every %ds for queued jobs.", POLL_INTERVAL)
    supabase = db.get_client()
    await init_browser()

    try:
        while True:
            # Fetch oldest pending job
            res = (
                supabase.table("scrape_queue")
                .select("*")
                .eq("status", "pending")
                .order("created_at")
                .limit(1)
                .execute()
            )

            if res.data:
                job = res.data[0]
                log.info(f"Processing job {job['id']} for site {job['site_id']}")
                n = await process_job(job, supabase)
                log.info(f"Job done — {n} new tenders.")
            else:
                log.debug("No pending jobs.")

            await asyncio.sleep(POLL_INTERVAL)

    except asyncio.CancelledError:
        log.info("Worker shutting down.")
    finally:
        await close_browser()


if __name__ == "__main__":
    asyncio.run(run_worker())
