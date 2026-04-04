"""
Tender Management Scraper
Handles: login, pagination, AI extraction, Google Sheets + CSV output
"""

import asyncio
import json
import csv
import os
import re
import hashlib
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import anthropic
from playwright.async_api import async_playwright, Page, BrowserContext

# ── Optional Google Sheets support ──────────────────────────────────────────
try:
    import gspread
    from google.oauth2.service_account import Credentials
    GSHEETS_AVAILABLE = True
except ImportError:
    GSHEETS_AVAILABLE = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("logs/scraper.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION  —  edit config/sites.json instead of this file
# ══════════════════════════════════════════════════════════════════════════════

DEFAULT_CONFIG = {
    "anthropic_api_key": os.getenv("ANTHROPIC_API_KEY", ""),
    "google_sheets": {
        "enabled": False,
        "credentials_file": "config/google_credentials.json",
        "spreadsheet_id": "",           # paste your sheet ID here
        "worksheet_name": "Tenders",
    },
    "csv_output": "output/tenders.csv",
    "seen_ids_file": "output/seen_ids.json",   # deduplication store
    "sites": [],                               # populated from sites.json
}

SHEET_HEADERS = [
    "ID", "Title", "Reference", "Issuer", "Category",
    "Deadline", "Budget", "Status", "Description", "URL", "Source Site", "Scraped At",
]


# ══════════════════════════════════════════════════════════════════════════════
# UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

def load_config() -> dict:
    cfg = DEFAULT_CONFIG.copy()
    sites_path = Path("config/sites.json")
    if sites_path.exists():
        with open(sites_path) as f:
            cfg["sites"] = json.load(f)
    else:
        log.warning("config/sites.json not found — using built-in demo site.")
        cfg["sites"] = [DEMO_SITE]
    return cfg


def load_seen_ids(path: str) -> set:
    p = Path(path)
    if p.exists():
        with open(p) as f:
            return set(json.load(f))
    return set()


def save_seen_ids(path: str, ids: set):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(list(ids), f)


def tender_id(tender: dict) -> str:
    """Stable fingerprint for deduplication."""
    key = (tender.get("reference") or tender.get("title") or "").strip().lower()
    return hashlib.md5(key.encode()).hexdigest()[:12]


def now_str() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")


# ══════════════════════════════════════════════════════════════════════════════
# AI EXTRACTION
# ══════════════════════════════════════════════════════════════════════════════

def extract_tenders_with_ai(html: str, page_url: str, api_key: str) -> list[dict]:
    """Send page HTML to Claude and get back structured tender list."""
    client = anthropic.Anthropic(api_key=api_key)

    # Trim HTML — keep first 15k chars to stay within token limits
    trimmed = html[:15_000]

    prompt = f"""You are a procurement data extraction specialist.

Extract ALL tender/procurement opportunities from the HTML below.
Page URL: {page_url}

Return a JSON array. Each object must have these exact keys (null if not found):
  title         – tender/contract title
  reference     – reference or tender number
  issuer        – authority or organisation issuing the tender
  category      – type of work, goods, or services
  deadline      – closing/submission deadline (keep original text)
  budget        – contract value or estimated budget (keep original text)
  status        – Open | Closing Soon | Closed | Awarded | Unknown
  description   – 1-2 sentence summary
  url           – direct link to the tender detail page (absolute URL if possible)

Rules:
- Return ONLY the raw JSON array — no markdown fences, no explanation.
- If zero tenders are found, return [].
- Do not invent data; use null for unknown fields.

HTML:
{trimmed}
"""

    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()
    # Strip accidental markdown fences
    raw = re.sub(r"^```[a-z]*\n?", "", raw).rstrip("` \n")

    try:
        tenders = json.loads(raw)
        if not isinstance(tenders, list):
            raise ValueError("Expected list")
        return tenders
    except Exception as e:
        log.error(f"AI parse error: {e}\nRaw response:\n{raw[:500]}")
        return []


# ══════════════════════════════════════════════════════════════════════════════
# PLAYWRIGHT SCRAPER
# ══════════════════════════════════════════════════════════════════════════════

async def login(page: Page, site: dict):
    """Handle form-based login if credentials are configured."""
    creds = site.get("credentials")
    if not creds:
        return

    login_url = site.get("login_url") or site["url"]
    log.info(f"  → Logging in at {login_url}")
    await page.goto(login_url, wait_until="networkidle")
    await page.wait_for_timeout(1000)

    # Fill username
    username_sel = creds.get("username_selector", 'input[name="username"], input[type="email"]')
    await page.fill(username_sel, creds["username"])

    # Fill password
    password_sel = creds.get("password_selector", 'input[name="password"], input[type="password"]')
    await page.fill(password_sel, creds["password"])

    # Submit
    submit_sel = creds.get("submit_selector", 'button[type="submit"]')
    await page.click(submit_sel)
    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(1500)
    log.info("  → Login submitted")


async def scrape_site(site: dict, api_key: str, context: BrowserContext) -> list[dict]:
    """Scrape one configured site across all pages."""
    all_tenders: list[dict] = []
    site_name = site.get("name", site["url"])
    log.info(f"Scraping: {site_name}")

    page = await context.new_page()

    # ── Login ────────────────────────────────────────────────────────────────
    await login(page, site)

    # ── Pagination ───────────────────────────────────────────────────────────
    pagination = site.get("pagination", {})
    strategy   = pagination.get("strategy", "none")   # none | next_button | url_param
    max_pages  = pagination.get("max_pages", 10)
    delay_ms   = site.get("delay_ms", 2000)

    current_url = site["url"]
    page_num    = 1

    while page_num <= max_pages:
        log.info(f"  Page {page_num}: {current_url}")
        await page.goto(current_url, wait_until="networkidle")
        await page.wait_for_timeout(delay_ms)

        # Optional: wait for a specific element to appear
        wait_sel = site.get("wait_for_selector")
        if wait_sel:
            try:
                await page.wait_for_selector(wait_sel, timeout=10_000)
            except Exception:
                log.warning(f"  wait_for_selector '{wait_sel}' timed out")

        html = await page.content()
        tenders = extract_tenders_with_ai(html, current_url, api_key)
        log.info(f"  → {len(tenders)} tenders extracted")

        for t in tenders:
            t["source_site"] = site_name
            t["scraped_at"]  = now_str()

        all_tenders.extend(tenders)

        # ── Advance to next page ─────────────────────────────────────────────
        if strategy == "none" or not tenders:
            break

        elif strategy == "next_button":
            next_sel = pagination.get("next_selector", 'a[rel="next"], .pagination-next, button.next')
            try:
                btn = page.locator(next_sel).first
                if await btn.count() == 0 or not await btn.is_visible():
                    log.info("  → No next button found, done.")
                    break
                await btn.click()
                await page.wait_for_load_state("networkidle")
                current_url = page.url
                page_num += 1
            except Exception as e:
                log.info(f"  → Pagination ended: {e}")
                break

        elif strategy == "url_param":
            param      = pagination.get("param", "page")
            start      = pagination.get("start", 1)
            increment  = pagination.get("increment", 1)
            next_val   = start + page_num * increment
            # Replace or append the param
            if f"{param}=" in current_url:
                current_url = re.sub(rf"{param}=\d+", f"{param}={next_val}", current_url)
            else:
                sep = "&" if "?" in current_url else "?"
                current_url = f"{current_url}{sep}{param}={next_val}"
            page_num += 1

        else:
            break

    await page.close()
    log.info(f"  Total from {site_name}: {len(all_tenders)}")
    return all_tenders


# ══════════════════════════════════════════════════════════════════════════════
# OUTPUT: CSV
# ══════════════════════════════════════════════════════════════════════════════

def append_to_csv(tenders: list[dict], path: str):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    file_exists = Path(path).exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SHEET_HEADERS, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        for t in tenders:
            row = {
                "ID":           t.get("id", ""),
                "Title":        t.get("title", ""),
                "Reference":    t.get("reference", ""),
                "Issuer":       t.get("issuer", ""),
                "Category":     t.get("category", ""),
                "Deadline":     t.get("deadline", ""),
                "Budget":       t.get("budget", ""),
                "Status":       t.get("status", ""),
                "Description":  t.get("description", ""),
                "URL":          t.get("url", ""),
                "Source Site":  t.get("source_site", ""),
                "Scraped At":   t.get("scraped_at", ""),
            }
            writer.writerow(row)
    log.info(f"CSV updated: {path}")


# ══════════════════════════════════════════════════════════════════════════════
# OUTPUT: GOOGLE SHEETS
# ══════════════════════════════════════════════════════════════════════════════

def append_to_sheets(tenders: list[dict], gs_config: dict):
    if not GSHEETS_AVAILABLE:
        log.warning("gspread not installed — skipping Google Sheets.")
        return
    if not gs_config.get("enabled"):
        return

    try:
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_file(
            gs_config["credentials_file"], scopes=scopes
        )
        gc    = gspread.authorize(creds)
        sheet = gc.open_by_key(gs_config["spreadsheet_id"])

        try:
            ws = sheet.worksheet(gs_config["worksheet_name"])
        except gspread.WorksheetNotFound:
            ws = sheet.add_worksheet(gs_config["worksheet_name"], rows=1000, cols=20)
            ws.append_row(SHEET_HEADERS)

        rows = []
        for t in tenders:
            rows.append([
                t.get("id", ""),        t.get("title", ""),
                t.get("reference", ""), t.get("issuer", ""),
                t.get("category", ""),  t.get("deadline", ""),
                t.get("budget", ""),    t.get("status", ""),
                t.get("description",""),t.get("url", ""),
                t.get("source_site",""),t.get("scraped_at",""),
            ])

        if rows:
            ws.append_rows(rows, value_input_option="USER_ENTERED")
            log.info(f"Google Sheets updated — {len(rows)} rows added.")

    except Exception as e:
        log.error(f"Google Sheets error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# DEMO SITE (used when no sites.json exists)
# ══════════════════════════════════════════════════════════════════════════════

DEMO_SITE = {
    "name": "UK Find a Tender (demo)",
    "url": "https://www.find-tender.service.gov.uk/Search/Results",
    "pagination": {
        "strategy": "url_param",
        "param": "page",
        "start": 1,
        "increment": 1,
        "max_pages": 3,
    },
    "delay_ms": 2000,
}


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    cfg      = load_config()
    api_key  = cfg["anthropic_api_key"]
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set. Export it as an environment variable.")

    seen_ids = load_seen_ids(cfg["seen_ids_file"])
    log.info(f"Starting run — {len(cfg['sites'])} site(s) configured, {len(seen_ids)} known tenders.")

    all_new: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )

        for site in cfg["sites"]:
            try:
                tenders = await scrape_site(site, api_key, context)
            except Exception as e:
                log.error(f"Failed scraping {site.get('name', site['url'])}: {e}")
                continue

            # Deduplicate
            new_tenders = []
            for t in tenders:
                tid = tender_id(t)
                if tid not in seen_ids:
                    t["id"] = tid
                    seen_ids.add(tid)
                    new_tenders.append(t)

            log.info(f"  {len(new_tenders)} new (deduplicated from {len(tenders)})")
            all_new.extend(new_tenders)

        await browser.close()

    # Persist
    save_seen_ids(cfg["seen_ids_file"], seen_ids)

    if all_new:
        append_to_csv(all_new, cfg["csv_output"])
        append_to_sheets(all_new, cfg["google_sheets"])
        log.info(f"Run complete — {len(all_new)} new tenders saved.")
    else:
        log.info("Run complete — no new tenders found.")

    return all_new


if __name__ == "__main__":
    asyncio.run(main())
