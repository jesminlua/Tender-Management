import re
import json
import hashlib
import logging
from urllib.parse import urljoin
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

CHUNK_SIZE = 12000
CHUNK_OVERLAP = 1000

BASE_LISTING_PROMPT = """You are a procurement data extraction specialist for Malaysian tender websites.

Site: {site_name_or_url}

UNIVERSAL RULES — apply to ALL sites:
1. Extract ALL procurement opportunities — open, closing soon, and unknown status. Include price schedules, corrigenda, and refund notices only if they are attached to a specific tender reference number. Include tenders, quotations (sebut harga), RFPs, EOIs
2. IGNORE completely: closed, awarded results, cancellations, successful bidder lists (Syarikat Berjaya),
   staff announcements, promotions, news articles, event notices, committee meeting schedules,
   annual reports, strategic plan books, T-shirt procurement for internal events
3. IGNORE any item whose section header contains: Result, Keputusan, Cancellation, 
   Pembatalan, Successful Bidder, Berjaya, Jadual Penyertaan
4. USE the presence of a tender reference number (e.g. IIUM/DEV/3/2026, PTPTN/2026/SH11,
   KDN/T/001/2026, UTM/BID/2026) as the PRIMARY signal that an item is a real tender
5. If sections are labelled, ONLY extract from: Quotation Proposal, Tender Proposal,
   Sebut Harga, Tawaran Perolehan, Active Tenders, Open Tenders
6. If the page mixes tender and non-tender content, use judgment — a tender has a 
   reference number, a deadline, and a category of goods/works/services

{extract_hint}

For each tender found, return a JSON object with these keys (null if unknown):
  title        — full tender title
  reference    — reference/document number
  issuer       — the organisation issuing the tender
  category     — type of goods/works/services
  deadline     — closing date (keep original text)
  briefing_date — tender briefing or site visit date and time if shown
  budget       — estimated value if shown
  status       — exactly one of: Open | Closing Soon | Closed | Awarded | Unknown
  description  — 1-2 sentence summary
  url          — absolute URL to the tender detail page
  location     — geographic location if mentioned
  contact      — contact name/email/phone if shown

Return ONLY a raw JSON array. Return [] if no active tenders found.

Page text:
{text}
"""

DETAIL_PROMPT = """You are a procurement data extraction specialist.

Extract additional detail from this tender detail page.
Source URL: {url}

Return a JSON object with these keys (null if not found):
  full_description  — complete description of scope, extract as much information as you can and summarise it
  requirements      — eligibility or technical requirements  
  submission_method — how to submit
  contact_name      — contact person
  contact_email     — contact email
  contact_phone     — contact phone
  briefing_date     - tender briefing, site visit, or mandatory briefing date and time
  additional_info   — any other important information

Return ONLY the raw JSON object, no markdown.

Page text:
{text}
"""

FILE_PROMPT = """You are a procurement data extraction specialist.

Extract key information from this tender document.
Source URL: {url}

Return a JSON object with these keys (null if not found):
  full_description, requirements, submission_method,
  contact_name, contact_email, contact_phone, deadline, budget, additional_info

Return ONLY the raw JSON object, no markdown.

Document text:
{text}
"""


def clean_html(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["script", "style", "nav", "footer", "header",
                               "aside", "noscript", "iframe", "svg"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text


def find_download_links(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    download_links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        lower = href.lower()
        if any(ext in lower for ext in [".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip"]):
            abs_url = urljoin(base_url, href)
            link_text = a.get_text(strip=True) or "document"
            download_links.append({"url": abs_url, "text": link_text})
    seen = set()
    unique = []
    for d in download_links:
        if d["url"] not in seen:
            seen.add(d["url"])
            unique.append(d)
    return unique


def chunk_text(text, chunk_size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    if len(text) <= chunk_size:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        chunks.append(text[start:start + chunk_size])
        start += chunk_size - overlap
    return chunks


def fingerprint(tender):
    key = " ".join(filter(None, [
        (tender.get("reference") or "").strip().lower(),
        (tender.get("title") or "").strip().lower()[:60],
    ]))
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def resolve_urls(tenders, base_url):
    for t in tenders:
        if t.get("url") and not t["url"].startswith("http"):
            t["url"] = urljoin(base_url, t["url"])
    return tenders


def call_claude(prompt, api_key, max_tokens=4096):
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


def parse_json_response(raw):
    raw = re.sub(r"^```[a-z]*\n?", "", raw).rstrip("` \n")
    match = re.search(r"[\[{].*[\]}]", raw, re.DOTALL)
    if not match:
        raise ValueError("No JSON found in response")
    return json.loads(match.group(0))


def extract_from_html(html, page_url, api_key, extract_hint=""):
    clean_text = clean_html(html, page_url)
    chunks = chunk_text(clean_text)
    all_tenders = []
    seen_fps = set()

    hint_section = f"\nSITE-SPECIFIC HINT: {extract_hint}\n" if extract_hint else ""

    for i, chunk in enumerate(chunks, 1):
        if len(chunks) > 1:
            log.info(f"    Chunk {i}/{len(chunks)} ({len(chunk)} chars)")

        prompt = BASE_LISTING_PROMPT.format(
            site_name_or_url=page_url,
            extract_hint=hint_section,
            text=chunk
        )

        tenders = []
        for attempt in range(1, 3):
            try:
                raw = call_claude(prompt, api_key)
                tenders = parse_json_response(raw)
                if not isinstance(tenders, list):
                    tenders = []
                break
            except Exception as e:
                log.warning(f"    Attempt {attempt} failed: {e}")

        tenders = resolve_urls(tenders, page_url)
        for t in tenders:
            fp = fingerprint(t)
            if fp not in seen_fps:
                seen_fps.add(fp)
                t["fingerprint"] = fp
                all_tenders.append(t)

    log.info(f"  Extracted {len(all_tenders)} tenders from {len(chunks)} chunk(s).")
    return all_tenders


def extract_detail_page(html, page_url, api_key):
    clean_text = clean_html(html, page_url)[:15000]
    prompt = DETAIL_PROMPT.format(url=page_url, text=clean_text)
    try:
        raw = call_claude(prompt, api_key)
        detail = parse_json_response(raw)
        return detail if isinstance(detail, dict) else {}
    except Exception as e:
        log.warning(f"  Detail extraction failed: {e}")
    return {}


def extract_from_pdf(file_path, file_url, api_key):
    try:
        import pdfplumber
        text = ""
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
        text = text[:15000]
        if not text.strip():
            return {}
        raw = call_claude(FILE_PROMPT.format(url=file_url, text=text), api_key)
        result = parse_json_response(raw)
        return result if isinstance(result, dict) else {}
    except Exception as e:
        log.warning(f"  PDF extraction failed: {e}")
        return {}


def extract_from_docx(file_path, file_url, api_key):
    try:
        import docx
        doc = docx.Document(file_path)
        text = "\n".join([p.text for p in doc.paragraphs if p.text.strip()])[:15000]
        if not text.strip():
            return {}
        raw = call_claude(FILE_PROMPT.format(url=file_url, text=text), api_key)
        result = parse_json_response(raw)
        return result if isinstance(result, dict) else {}
    except Exception as e:
        log.warning(f"  DOCX extraction failed: {e}")
        return {}


def merge_detail_into_tender(tender, detail):
    if not detail:
        return tender
    if detail.get("full_description"):
        if not tender.get("description"):
            tender["description"] = detail["full_description"]
        else:
            tender["description"] += "\n\n" + detail["full_description"]
    for field in ["contact_name", "contact_email", "contact_phone",
                  "submission_method", "requirements","briefing_date", "additional_info"]:
        if detail.get(field) and not tender.get(field):
            tender[field] = detail[field]
    parts = list(filter(None, [
        detail.get("contact_name"), detail.get("contact_email"), detail.get("contact_phone")
    ]))
    if parts:
        tender["contact"] = " | ".join(parts)
    return tender
