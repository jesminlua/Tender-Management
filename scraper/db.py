"""
db.py — Supabase database layer
Handles all reads/writes: sites, tenders, runs, cookies
"""

import os
import json
import logging
from datetime import datetime
from typing import Optional
from supabase import create_client, Client

log = logging.getLogger(__name__)


def get_client() -> Client:
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_KEY"]   # service role key — never expose publicly
    return create_client(url, key)


# ══════════════════════════════════════════════════════════════════════════════
# SITES
# ══════════════════════════════════════════════════════════════════════════════

def fetch_active_sites(db: Client) -> list[dict]:
    """Return all enabled site configs from the database."""
    res = db.table("sites").select("*").eq("enabled", True).execute()
    return res.data or []


# ══════════════════════════════════════════════════════════════════════════════
# TENDERS
# ══════════════════════════════════════════════════════════════════════════════

def fetch_seen_fingerprints(db: Client) -> set[str]:
    """Fetch all known tender fingerprints for deduplication."""
    res = db.table("tenders").select("fingerprint").execute()
    return {row["fingerprint"] for row in (res.data or [])}


def upsert_tenders(db: Client, tenders: list[dict]) -> int:
    """
    Insert new tenders. Skip duplicates via fingerprint unique constraint.
    Returns count of actually inserted rows.
    """
    if not tenders:
        return 0
    res = (
        db.table("tenders")
        .upsert(tenders, on_conflict="fingerprint", ignore_duplicates=True)
        .execute()
    )
    inserted = len(res.data) if res.data else 0
    log.info(f"  DB: upserted {len(tenders)} rows, {inserted} new.")
    return inserted


# ══════════════════════════════════════════════════════════════════════════════
# SCRAPE RUNS (audit log)
# ══════════════════════════════════════════════════════════════════════════════

def start_run(db: Client, site_id: str) -> str:
    """Insert a run record, return its ID."""
    res = db.table("scrape_runs").insert({
        "site_id":    site_id,
        "status":     "running",
        "started_at": datetime.utcnow().isoformat(),
    }).execute()
    return res.data[0]["id"]


def finish_run(db: Client, run_id: str, status: str, pages: int, found: int, error: str = None):
    db.table("scrape_runs").update({
        "status":       status,          # "success" | "partial" | "error"
        "finished_at":  datetime.utcnow().isoformat(),
        "pages_scraped": pages,
        "tenders_found": found,
        "error_message": error,
    }).eq("id", run_id).execute()


# ══════════════════════════════════════════════════════════════════════════════
# COOKIE STORE (persistent sessions for login-protected sites)
# ══════════════════════════════════════════════════════════════════════════════

def load_cookies(db: Client, site_id: str) -> Optional[list]:
    res = db.table("site_cookies").select("cookies").eq("site_id", site_id).execute()
    if res.data:
        return json.loads(res.data[0]["cookies"])
    return None


def save_cookies(db: Client, site_id: str, cookies: list):
    db.table("site_cookies").upsert({
        "site_id":    site_id,
        "cookies":    json.dumps(cookies),
        "updated_at": datetime.utcnow().isoformat(),
    }, on_conflict="site_id").execute()
