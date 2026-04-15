import asyncio
import logging
import os
import sys
import tempfile
import mimetypes
import json
from datetime import datetime

import db
import extractor
import login as login_mod
import pagination as pag_mod
from browser_pool import init_browser, close_browser, acquire_tab
from notify import send_digest

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WORKER] %(message)s",
    handlers=[
        logging.FileHandler("worker.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

API_KEY = os.environ["ANTHROPIC_API_KEY"]
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "30"))
VISIT_DETAIL_PAGES = os.getenv("VISIT_DETAIL_PAGES", "true").lower() == "true"
DOWNLOAD_DOCUMENTS = os.getenv("DOWNLOAD_DOCUMENTS", "true").lower() == "true"
MAX_DOCS_PER_TENDER = int(os.getenv("MAX_DOCS_PER_TENDER", "3"))

DETAIL_LINK_PATTERNS = [
    "tender", "tawaran", "perolehan", "sebutharga", "procurement",
    "announcement", "notice", "iklan", "finance/",
]


async def expand_page_content(page):
    """Expand accordions and hidden content before scraping."""
    try:
        await page.evaluate("""
            document.querySelectorAll('details').forEach(d => d.open = true);
            document.querySelectorAll(
                '[class*="expand"], [class*="accordion"], [class*="collapse"]'
            ).forEach(el => { try { el.click(); } catch(e) {} });
        """)
        await asyncio.sleep(1)
    except Exception as e:
        log.debug(f"  Accordion expansion: {e}")


async def load_page(page, url, site):
    """Navigate to URL and wait for full content load."""
    wait_sel = site.get("wait_for_selector")
    delay = site.get("delay_ms", 3000) / 1000

    await page.goto(url, wait_until="networkidle", timeout=60000)

    if wait_sel:
        try:
            await page.wait_for_selector(wait_sel, timeout=20000)
            log.info(f"  Selector '{wait_sel}' found.")
        except Exception:
            log.warning(f"  Selector '{wait_sel}' timed out — continuing.")

    await asyncio.sleep(delay)

    # Scroll to trigger lazy loading
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await asyncio.sleep(0.5)
    await page.evaluate("window.scrollTo(0, 0)")

    await expand_page_content(page)

    html = await page.content()
    log.info(f"  Page HTML size: {len(html)} chars")

    if len(html) < 5000:
        raise RuntimeError(f"Page too small ({len(html)} chars) — possible IP block or JS failure")

    return html


async def find_detail_links_from_images(html, base_url):
    """When listing has images, find links to detail pages by URL pattern."""
    from bs4 import BeautifulSoup
    from urllib.parse import urljoin
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        full = urljoin(base_url, href)
        text = a.get_text(strip=True).lower()
        if any(p in full.lower() or p in text for p in DETAIL_LINK_PATTERNS):
            links.append(full)
    seen = set()
    return [l for l in links if not (l in seen or seen.add(l))]


async def download_file(page, url, tmp_dir):
    try:
        filename = url.split("/")[-1].split("?")[0] or "document"
        if "." not in filename:
            filename += ".pdf"
        file_path = os.path.join(tmp_dir, filename)
        response = await page.request.get(url)
        if response.ok:
            content = await response.body()
            with open(file_path, "wb") as f:
                f.write(content)
            log.info(f"    Downloaded: {filename}")
            return file_path, filename
        else:
            log.warning(f"    Download HTTP {response.status}: {url}")
    except Exception as e:
        log.warning(f"    Download failed: {e}")
    return None, None


def upload_to_storage(db_client, file_path, filename, fingerprint):
    try:
        storage_path = f"tender-docs/{fingerprint}/{filename}"
        with open(file_path, "rb") as f:
            content = f.read()
        mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        db_client.storage.from_("tender-documents").upload(
            path=storage_path,
            file=content,
            file_options={"content-type": mime_type, "upsert": "true"},
        )
        return db_client.storage.from_("tender-documents").get_public_url(storage_path)
    except Exception as e:
        log.warning(f"    Storage upload failed: {e}")
        return None


def extract_file_content(file_path, file_url):
    lower = file_path.lower()
    if lower.endswith(".pdf"):
        return extractor.extract_from_pdf(file_path, file_url, API_KEY)
    elif lower.endswith(".docx") or lower.endswith(".doc"):
        return extractor.extract_from_docx(file_path, file_url, API_KEY)
    return {}


async def enrich_tender(tender, site_url, supabase, page):
    """Visit detail page and download documents for a single tender."""
    detail_url = tender.get("url")
    if not detail_url or not detail_url.startswith("http"):
        return tender
    if detail_url.rstrip("/") == site_url.rstrip("/"):
        log.info(f"  Skipping detail — same as listing URL")
        return tender

    log.info(f"  Detail page: {detail_url}")
    try:
        await page.goto(detail_url, wait_until="networkidle", timeout=30000)
        await asyncio.sleep(2)
        await expand_page_content(page)
        html = await page.content()

        if VISIT_DETAIL_PAGES:
            detail_info = extractor.extract_detail_page(html, detail_url, API_KEY)
            tender = extractor.merge_detail_into_tender(tender, detail_info)

        if DOWNLOAD_DOCUMENTS:
            download_links = extractor.find_download_links(html, detail_url)
            if download_links:
                log.info(f"  Found {len(download_links)} document(s)")
                doc_urls = []
                with tempfile.TemporaryDirectory() as tmp_dir:
                    for link in download_links[:MAX_DOCS_PER_TENDER]:
                        file_path, filename = await download_file(page, link["url"], tmp_dir)
                        if file_path and os.path.exists(file_path):
                            file_info = extract_file_content(file_path, link["url"])
                            tender = extractor.merge_detail_into_tender(tender, file_info)
                            pub_url = upload_to_storage(
                                supabase, file_path, filename,
                                tender.get("fingerprint", "unknown")
                            )
                            if pub_url:
                                doc_urls.append({"name": link["text"], "url": pub_url})
                if doc_urls:
                    tender["document_urls"] = json.dumps(doc_urls)
    except Exception as e:
        log.warning(f"  Detail page failed: {e}")
    return tender


async def process_job(job, supabase):
    job_id = job["id"]
    site_id = job["site_id"]

    supabase.table("scrape_queue").update({
        "status": "running",
        "started_at": datetime.utcnow().isoformat(),
    }).eq("id", job_id).execute()

    res = supabase.table("sites").select("*").eq("id", site_id).single().execute()
    if not res.data:
        supabase.table("scrape_queue").update({
            "status": "error", "error": "Site not found",
            "finished_at": datetime.utcnow().isoformat(),
        }).eq("id", job_id).execute()
        return 0

    site = res.data
    site_url = site["url"]
    extract_hint = site.get("extract_hint", "")
    log.info(f"  Scraping: {site['name']} — {site_url}")

    seen_fps = db.fetch_seen_fingerprints(supabase)
    all_new = []

    try:
        saved_cookies = db.load_cookies(supabase, site_id)
        async with acquire_tab(cookies=saved_cookies) as (context, page):
            logged_in = await login_mod.ensure_logged_in(page, context, site, supabase)
            if not logged_in:
                raise RuntimeError("Login failed")

            async for page_num in pag_mod.paginate(page, site):
                log.info(f"  Scraping page {page_num}...")

                # Load page with full wait logic
                current_url = page.url
                if page_num == 1:
                    html = await load_page(page, site_url, site)
                else:
                    await asyncio.sleep(site.get("delay_ms", 3000) / 1000)
                    await expand_page_content(page)
                    html = await page.content()
                    log.info(f"  Page HTML size: {len(html)} chars")

                # Extract tenders from listing
                tenders = extractor.extract_from_html(html, page.url, API_KEY, extract_hint)

                # IMAGE-BASED FALLBACK: if 0 tenders extracted, try following detail links
                if len(tenders) == 0 and page_num == 1:
                    log.info("  0 tenders from listing — trying image-based fallback (detail links)")
                    detail_links = await find_detail_links_from_images(html, page.url)
                    log.info(f"  Found {len(detail_links)} potential detail links")
                    for link in detail_links[:20]:
                        try:
                            await page.goto(link, wait_until="networkidle", timeout=20000)
                            await asyncio.sleep(1)
                            detail_html = await page.content()
                            detail_tenders = extractor.extract_from_html(
                                detail_html, link, API_KEY, extract_hint
                            )
                            for t in detail_tenders:
                                t["url"] = link
                            tenders.extend(detail_tenders)
                        except Exception as e:
                            log.warning(f"  Detail link failed: {e}")
                    # Navigate back to listing for pagination
                    await page.goto(site_url, wait_until="networkidle", timeout=30000)

                # Deduplicate
                new_this_page = []
                for t in tenders:
                    fp = t.get("fingerprint") or extractor.fingerprint(t)
                    if fp not in seen_fps:
                        seen_fps.add(fp)
                        t.update({
                            "fingerprint": fp,
                            "site_id": site_id,
                            "source_site": site["name"],
                            "scraped_at": datetime.utcnow().isoformat(),
                        })
                        new_this_page.append(t)

                log.info(f"  Page {page_num}: {len(new_this_page)} new tenders")

                # Enrich each new tender with detail page + documents
                for t in new_this_page:
                    if t.get("url") and (VISIT_DETAIL_PAGES or DOWNLOAD_DOCUMENTS):
                        t = await enrich_tender(t, site_url, supabase, page)
                    all_new.append(t)

                # Stop paginating if no new tenders found after page 1
                if len(new_this_page) == 0 and page_num > 1:
                    log.info("  No new tenders on this page — stopping pagination")
                    break

        if all_new:
            db.upsert_tenders(supabase, all_new)

        supabase.table("scrape_queue").update({
            "status": "done",
            "finished_at": datetime.utcnow().isoformat(),
        }).eq("id", job_id).execute()

    except Exception as e:
        log.error(f"Job {job_id} failed: {e}", exc_info=True)
        supabase.table("scrape_queue").update({
            "status": "error", "error": str(e),
            "finished_at": datetime.utcnow().isoformat(),
        }).eq("id", job_id).execute()

    return len(all_new)


async def run_worker():
    log.info("Worker started. Polling every %ds for queued jobs.", POLL_INTERVAL)
    supabase = db.get_client()
    await init_browser()

    try:
        while True:
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
                if n > 0:
                    try:
                        send_digest(n)
                    except Exception as e:
                        log.warning(f"Email digest failed: {e}")
            else:
                log.debug("No pending jobs.")
            await asyncio.sleep(POLL_INTERVAL)

    except asyncio.CancelledError:
        log.info("Worker shutting down.")
    finally:
        await close_browser()


if __name__ == "__main__":
    asyncio.run(run_worker())
