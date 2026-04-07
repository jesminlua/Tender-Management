import asyncio
import logging
import os
import sys
import tempfile
import mimetypes
from datetime import datetime
from pathlib import Path

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

# Whether to visit each tender detail page (slower but more complete)
VISIT_DETAIL_PAGES = os.getenv("VISIT_DETAIL_PAGES", "true").lower() == "true"

# Whether to download and extract tender documents
DOWNLOAD_DOCUMENTS = os.getenv("DOWNLOAD_DOCUMENTS", "true").lower() == "true"

# Max documents to download per tender
MAX_DOCS_PER_TENDER = int(os.getenv("MAX_DOCS_PER_TENDER", "3"))


async def download_file(page, url, tmp_dir):
    try:
        async with page.expect_download(timeout=30000) as download_info:
            await page.goto(url, timeout=30000)
        download = await download_info.value
        filename = download.suggested_filename or url.split("/")[-1] or "document"
        file_path = os.path.join(tmp_dir, filename)
        await download.save_as(file_path)
        log.info(f"    Downloaded: {filename}")
        return file_path, filename
    except Exception:
        try:
            response = await page.goto(url, timeout=30000)
            if response and response.ok:
                content = await response.body()
                filename = url.split("/")[-1] or "document"
                if "." not in filename:
                    ct = response.headers.get("content-type", "")
                    if "pdf" in ct:
                        filename += ".pdf"
                    elif "word" in ct or "docx" in ct:
                        filename += ".docx"
                file_path = os.path.join(tmp_dir, filename)
                with open(file_path, "wb") as f:
                    f.write(content)
                log.info(f"    Downloaded via GET: {filename}")
                return file_path, filename
        except Exception as e:
            log.warning(f"    Download failed for {url}: {e}")
    return None, None


def upload_to_supabase_storage(db_client, file_path, filename, tender_fingerprint):
    try:
        storage_path = f"tender-docs/{tender_fingerprint}/{filename}"
        with open(file_path, "rb") as f:
            content = f.read()
        mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        db_client.storage.from_("tender-documents").upload(
            path=storage_path,
            file=content,
            file_options={"content-type": mime_type, "upsert": "true"},
        )
        public_url = db_client.storage.from_("tender-documents").get_public_url(storage_path)
        log.info(f"    Uploaded to storage: {storage_path}")
        return public_url
    except Exception as e:
        log.warning(f"    Storage upload failed: {e}")
        return None


def extract_file_content(file_path, file_url, api_key):
    lower = file_path.lower()
    if lower.endswith(".pdf"):
        return extractor.extract_from_pdf(file_path, file_url, api_key)
    elif lower.endswith(".docx") or lower.endswith(".doc"):
        return extractor.extract_from_docx(file_path, file_url, api_key)
    return {}


async def enrich_tender_with_detail(tender, context, supabase, api_key):
    detail_url = tender.get("url")
    if not detail_url or not detail_url.startswith("http"):
        return tender

    log.info(f"  Visiting detail page: {detail_url}")

    async with acquire_tab() as (ctx, page):
        try:
            await page.goto(detail_url, wait_until="networkidle", timeout=30000)
            await asyncio.sleep(1)

            html = await page.content()

            # Extract detail page info
            if VISIT_DETAIL_PAGES:
                detail_info = extractor.extract_detail_page(html, detail_url, api_key)
                tender = extractor.merge_detail_into_tender(tender, detail_info)

            # Find and download documents
            if DOWNLOAD_DOCUMENTS:
                download_links = extractor.find_download_links(html, detail_url)
                if download_links:
                    log.info(f"  Found {len(download_links)} document(s) to download")
                    doc_urls = []

                    with tempfile.TemporaryDirectory() as tmp_dir:
                        for i, link in enumerate(download_links[:MAX_DOCS_PER_TENDER]):
                            log.info(f"  Downloading: {link['text']} — {link['url']}")
                            file_path, filename = await download_file(page, link["url"], tmp_dir)

                            if file_path and os.path.exists(file_path):
                                # Extract text from the file
                                file_info = extract_file_content(file_path, link["url"], api_key)
                                tender = extractor.merge_detail_into_tender(tender, file_info)

                                # Upload to Supabase Storage
                                public_url = upload_to_supabase_storage(
                                    supabase, file_path, filename, tender.get("fingerprint", "unknown")
                                )
                                if public_url:
                                    doc_urls.append({"name": link["text"], "url": public_url, "original": link["url"]})

                    if doc_urls:
                        import json
                        tender["document_urls"] = json.dumps(doc_urls)

        except Exception as e:
            log.warning(f"  Detail page failed for {detail_url}: {e}")

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
            "status": "error",
            "error": "Site not found",
            "finished_at": datetime.utcnow().isoformat(),
        }).eq("id", job_id).execute()
        return 0

    site = res.data
    seen_fps = db.fetch_seen_fingerprints(supabase)
    all_new = []
    error = None

    try:
        saved_cookies = db.load_cookies(supabase, site_id)
        async with acquire_tab(cookies=saved_cookies) as (context, page):
            logged_in = await login_mod.ensure_logged_in(page, context, site, supabase)
            if not logged_in:
                raise RuntimeError("Login failed")

            if page.url != site["url"]:
                await page.goto(site["url"], wait_until="networkidle", timeout=30000)

            async for page_num in pag_mod.paginate(page, site):
                html = await page.content()
                tenders = extractor.extract_from_html(html, page.url, API_KEY)

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

                # Enrich each new tender with detail page + documents
                enriched = []
                for t in new_this_page:
                    if t.get("url") and (VISIT_DETAIL_PAGES or DOWNLOAD_DOCUMENTS):
                        t = await enrich_tender_with_detail(t, context, supabase, API_KEY)
                    enriched.append(t)

                all_new.extend(enriched)

                if not new_this_page and page_num > 1:
                    break

                await asyncio.sleep(site.get("delay_ms", 2000) / 1000)

        if all_new:
            db.upsert_tenders(supabase, all_new)

        supabase.table("scrape_queue").update({
            "status": "done",
            "finished_at": datetime.utcnow().isoformat(),
        }).eq("id", job_id).execute()

    except Exception as e:
        error = str(e)
        log.error(f"Job {job_id} failed: {e}", exc_info=True)
        supabase.table("scrape_queue").update({
            "status": "error",
            "error": error,
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
            else:
                log.debug("No pending jobs.")

            await asyncio.sleep(POLL_INTERVAL)

    except asyncio.CancelledError:
        log.info("Worker shutting down.")
    finally:
        await close_browser()


if __name__ == "__main__":
    asyncio.run(run_worker())
