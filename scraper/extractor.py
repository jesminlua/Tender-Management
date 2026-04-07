import re
import json
import hashlib
import logging
import tempfile
import os
from urllib.parse import urljoin
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

CHUNK_SIZE = 12000
CHUNK_OVERLAP = 1000

LISTING_PROMPT = """You are a procurement data extraction specialist.

Extract ALL tender/contract/procurement opportunities from the text below.
Source URL: {url}

Return a JSON array. Each object must have EXACTLY these keys (null if unknown):
  title        - full tender or contract title
  reference    - tender reference number or ID
  issuer       - organisation issuing this tender
  category     - type of goods, works, or services
  deadline     - submission deadline, keep original text
  budget       - estimated value or budget, keep original text
  status       - exactly one of: Open | Closing Soon | Closed | Awarded | Unknown
  description  - 1-2 sentence summary
  url          - direct URL to the tender detail page (absolute if possible)
  location     - geographic location if mentioned, else null
  contact      - contact name/email/phone if shown, else null

Return ONLY the raw JSON array, no markdown, no explanation.
If zero tenders found, return [].

Page text:
{text}
"""

DETAIL_PROMPT = """You are a procurement data extraction specialist.

Extract additional detail from this tender detail page.
Source URL: {url}

Return a JSON object with these keys (null if not found):
  full_description  - complete description of the tender scope
  requirements      - eligibility or technical requirements
  submission_method - how to submit (email, portal, post, etc.)
  contact_name      - contact person name
  contact_email     - contact email address
  contact_phone     - contact phone number
  documents         - list of document names/types available for download
  additional_info   - any other important information

Return ONLY the raw JSON object, no markdown, no explanation.

Page text:
{text}
"""

FILE_PROMPT = """You are a procurement data extraction specialist.

Extract key information from this tender document.
Source URL: {url}

Return a JSON object with these keys (null if not found):
  full_description  - complete scope of work or services required
  requirements      - technical, financial or eligibility requirements
  submission_method - how and where to submit the tender
  contact_name      - contact person name
  contact_email     - contact email
  contact_phone     - contact phone
  deadline          - submission deadline if different from listing
  budget            - contract value if mentioned
  additional_info   - any other critical information

Return ONLY the raw JSON object, no markdown, no explanation.

Document text:
{text}
"""


def clean_html(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["script", "style", "nav", "footer", "header", "aside", "noscript", "iframe", "svg"]):
        tag.decompose()
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href and not href.startswith(("#", "javascript:", "mailto:")):
            links.append(urljoin(base_url, href))
    text = soup.get_text(separator="\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text, links


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
    for btn in soup.find_all(["button", "a"], string=re.compile(r"download|tender document|bidding document", re.I)):
        href = btn.get("href") or btn.get("data-url") or btn.get("onclick", "")
        if href and href.startswith("http"):
            download_links.append({"url": href, "text": btn.get_text(strip=True)})
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
        end = start + chunk_size
        chunks.append(text[start:end])
        start = end - overlap
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


def extract_from_html(html, page_url, api_key):
    clean_text, _ = clean_html(html, page_url)
    chunks = chunk_text(clean_text)
    all_tenders = []
    seen_fps = set()

    for i, chunk in enumerate(chunks, 1):
        if len(chunks) > 1:
            log.info(f"    Chunk {i}/{len(chunks)} ({len(chunk)} chars)")
        prompt = LISTING_PROMPT.format(url=page_url, text=chunk)
        tenders = []
        for attempt in range(1, 3):
            try:
                raw = call_claude(prompt, api_key)
                tenders = parse_json_response(raw)
                if not isinstance(tenders, list):
                    tenders = []
                break
            except Exception as e:
                log.warning(f"    Extraction attempt {attempt} failed: {e}")

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
    clean_text, _ = clean_html(html, page_url)
    clean_text = clean_text[:15000]
    prompt = DETAIL_PROMPT.format(url=page_url, text=clean_text)
    try:
        raw = call_claude(prompt, api_key)
        detail = parse_json_response(raw)
        if isinstance(detail, dict):
            return detail
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
        prompt = FILE_PROMPT.format(url=file_url, text=text)
        raw = call_claude(prompt, api_key)
        result = parse_json_response(raw)
        return result if isinstance(result, dict) else {}
    except Exception as e:
        log.warning(f"  PDF extraction failed: {e}")
        return {}


def extract_from_docx(file_path, file_url, api_key):
    try:
        import docx
        doc = docx.Document(file_path)
        text = "\n".join([p.text for p in doc.paragraphs if p.text.strip()])
        text = text[:15000]
        if not text.strip():
            return {}
        prompt = FILE_PROMPT.format(url=file_url, text=text)
        raw = call_claude(prompt, api_key)
        result = parse_json_response(raw)
        return result if isinstance(result, dict) else {}
    except Exception as e:
        log.warning(f"  DOCX extraction failed: {e}")
        return {}


def merge_detail_into_tender(tender, detail):
    if not detail:
        return tender
    if detail.get("full_description") and not tender.get("description"):
        tender["description"] = detail["full_description"]
    elif detail.get("full_description"):
        tender["description"] = tender["description"] + "\n\n" + detail["full_description"]
    for field in ["contact_name", "contact_email", "contact_phone", "submission_method", "requirements", "additional_info"]:
        if detail.get(field) and not tender.get(field):
            tender[field] = detail[field]
    if detail.get("contact_name") or detail.get("contact_email") or detail.get("contact_phone"):
        parts = filter(None, [detail.get("contact_name"), detail.get("contact_email"), detail.get("contact_phone")])
        tender["contact"] = " | ".join(parts)
    return tender
