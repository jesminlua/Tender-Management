"""
extractor.py — AI-powered tender extraction using Claude

Features:
  - Cleans HTML before sending (strips scripts, styles, nav, footers)
  - Chunks large pages into overlapping windows to avoid token limits
  - Retries on parse failure with a stricter prompt
  - Assigns stable fingerprints for deduplication
  - Resolves relative URLs to absolute
"""

import re
import json
import hashlib
import logging
import asyncio
from urllib.parse import urljoin, urlparse
import anthropic
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

# Chars of cleaned text sent to Claude per chunk
CHUNK_SIZE    = 12_000
CHUNK_OVERLAP = 1_000   # overlap between chunks to avoid splitting tenders

EXTRACTION_PROMPT = """You are a procurement data extraction specialist.

Extract ALL tender/contract/procurement opportunities from the text below.
Source URL: {url}

Return a JSON array. Each object must have EXACTLY these keys (null if unknown):
  title        – full tender or contract title
  reference    – tender/contract/lot reference number or ID
  issuer       – organisation or authority issuing this tender
  category     – type of goods, works, or services (e.g. IT, Construction, Consulting)
  deadline     – submission/closing deadline, keep original text
  budget       – estimated value or contract budget, keep original text
  status       – exactly one of: Open | Closing Soon | Closed | Awarded | Unknown
  description  – 1-2 sentence plain-English summary
  url          – direct URL to the tender detail page (absolute if possible, else as-is)
  location     – geographic location or region if mentioned, else null
  contact      – contact name/email/phone if shown, else null

Rules:
- Return ONLY the raw JSON array, no markdown, no explanation.
- Do NOT invent data. Use null for any unknown field.
- A "Closing Soon" deadline is within 14 days of today.
- If zero tenders found, return [].

Page text:
{text}
"""

RETRY_PROMPT = """The previous extraction returned invalid JSON.
Extract tender data from the text below and return ONLY a valid JSON array.
No markdown. No explanation. Just the array, starting with [ and ending with ].

Text:
{text}
"""


def clean_html(html: str, base_url: str) -> tuple[str, list[str]]:
    """
    Strip boilerplate from HTML, return (clean_text, list_of_absolute_links).
    """
    soup = BeautifulSoup(html, "html.parser")

    # Remove noise elements
    for tag in soup.find_all(["script", "style", "nav", "footer", "header",
                               "aside", "noscript", "iframe", "svg"]):
        tag.decompose()

    # Collect all hrefs for link extraction
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href and not href.startswith(("#", "javascript:", "mailto:")):
            links.append(urljoin(base_url, href))

    text = soup.get_text(separator="\n", strip=True)
    # Collapse whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text, links


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split text into overlapping chunks."""
    if len(text) <= chunk_size:
        return [text]

    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        start = end - overlap
    return chunks


def fingerprint(tender: dict) -> str:
    """Stable 16-char hash for deduplication."""
    key = " ".join(filter(None, [
        (tender.get("reference") or "").strip().lower(),
        (tender.get("title")     or "").strip().lower()[:60],
    ]))
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def resolve_urls(tenders: list[dict], base_url: str) -> list[dict]:
    """Convert relative tender URLs to absolute."""
    for t in tenders:
        if t.get("url") and not t["url"].startswith("http"):
            t["url"] = urljoin(base_url, t["url"])
    return tenders


def call_claude(prompt: str, api_key: str, max_tokens: int = 4096) -> str:
    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


def parse_json_response(raw: str) -> list[dict]:
    """Extract JSON array from Claude response, handling markdown fences."""
    raw = re.sub(r"^```[a-z]*\n?", "", raw).rstrip("` \n")
    # Find the outermost array
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if not match:
        raise ValueError("No JSON array found in response")
    return json.loads(match.group(0))


def extract_from_html(html: str, page_url: str, api_key: str) -> list[dict]:
    """
    Main entry point. Given raw HTML and its URL, return a list of tender dicts
    with fingerprints assigned, ready to upsert into Supabase.
    """
    clean_text, _ = clean_html(html, page_url)
    chunks = chunk_text(clean_text)

    all_tenders: list[dict] = []
    seen_fps: set[str] = set()

    for i, chunk in enumerate(chunks, 1):
        if len(chunks) > 1:
            log.info(f"    Chunk {i}/{len(chunks)} ({len(chunk)} chars)")

        prompt = EXTRACTION_PROMPT.format(url=page_url, text=chunk)
        tenders = []

        for attempt in range(1, 3):
            try:
                raw = call_claude(prompt if attempt == 1 else RETRY_PROMPT.format(text=chunk), api_key)
                tenders = parse_json_response(raw)
                break
            except Exception as e:
                log.warning(f"    Extraction attempt {attempt} failed: {e}")
                if attempt == 2:
                    log.error(f"    Giving up on this chunk.")

        # Deduplicate within this page
        tenders = resolve_urls(tenders, page_url)
        for t in tenders:
            fp = fingerprint(t)
            if fp not in seen_fps:
                seen_fps.add(fp)
                t["fingerprint"] = fp
                all_tenders.append(t)

    log.info(f"  Extracted {len(all_tenders)} tenders from {len(chunks)} chunk(s).")
    return all_tenders
