"""Collect email addresses published on each company's own website -- run
LOCALLY, results committed to data/enriched/contacts/<domain>.json.

    python -m tools.contacts             # sites/documents not collected in REFRESH_DAYS
    python -m tools.contacts --refresh   # everything (after changing the collector)

Only addresses at the company's own domain are kept, along with the page they
appeared on -- and of those, only generic inboxes (ir@, info@) and addresses
belonging to a NAMED officer of that company. Ordinary staff addresses found on
the same pages are dropped: the pipeline has no use for them, and this repo is
public, so republishing them would be wrong. This step only COLLECTS. Deciding whether an address belongs to
the CEO (VERIFIED), implies a naming pattern (INFERRED), or is a generic inbox
is done by rules in core/enrich.py -- never by guessing.

Two sources, both published by the company itself:
  1. its website: homepage plus contact/IR pages;
  2. its offer document (SGX Catalist PDFs), whose summary has a standard
     "OUR CONTACT DETAILS" paragraph giving the website and inbox. This also
     supplies the website for companies the exchange lists without one.

An address counts as the company's if its domain is the website's domain, or
a sibling domain that spells the company's own name (Richest Supply publishes
info@richestsupply.com on enrichlighting.com). Free-mail and agency addresses
never count.

Politeness: at most a handful of pages per site, one second apart, and a
cache refreshed only when older than REFRESH_DAYS.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import requests
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.enrich import FILINGS, company_domain, keep_email  # noqa: E402
from core.http import DEFAULT_UA  # noqa: E402

DATA = ROOT / "docs" / "data" / "ipos.json"
OUT = ROOT / "data" / "enriched" / "contacts"
REFRESH_DAYS = 14
CANDIDATE_PATHS = ["", "/contact", "/contact-us", "/contactus", "/en/contact", "/investor-relations", "/ir"]
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
LINK = re.compile(r'href="([^"#]+)"', re.I)
# Matched against a link's PATH, decoded: matching the whole URL followed every
# link on tek4investors.co.uk, whose hostname contains "investor".
# ติดต่อ = "contact", นักลงทุน = "investor" (Thai sites link only in Thai).
CONTACT_LINK = re.compile(r"contact|investor|ติดต่อ|นักลงทุน", re.I)
# "Our internet address is www.x.com" / "Our email address is ir@x.com".
# Disclaimers ("The information on our website ...") don't match.
OFFER_CONTACT = re.compile(r"\b(?:our|its) (?:e-?mail|internet|website|web)(?: site)?(?: address)? (?:is|are)\b", re.I)
WEBSITE = re.compile(r"(?:internet address|website|web address)[^.]{0,20}? is (?:https?://)?((?:www\.)?[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})", re.I)
logging.getLogger("pypdf").setLevel(logging.ERROR)


def domain_of(url: str | None) -> str | None:
    if not url:
        return None
    host = urlparse(url if "//" in url else f"https://{url}").hostname or ""
    return host.lower().removeprefix("www.") or None


def _pages(base: str, session: requests.Session) -> list[tuple[str, str]]:
    """Fetch the homepage plus likely contact/IR pages, including any link on
    the homepage whose URL says contact/investor."""
    seen, out = set(), []
    queue = [urljoin(base, p) for p in CANDIDATE_PATHS]
    while queue and len(out) < 6:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            r = session.get(url, timeout=20, allow_redirects=True)
        except requests.RequestException:
            continue
        if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
            continue
        out.append((r.url, r.text))
        if len(out) == 1:   # homepage: follow its own contact/IR links first
            site = urlparse(r.url).hostname
            for href in LINK.findall(r.text):
                link = urljoin(r.url, unescape(href))
                if urlparse(link).hostname == site and CONTACT_LINK.search(unquote(urlparse(link).path)):
                    queue.insert(0, link)
        time.sleep(1.0)
    return out


def collect(company_url: str, session: requests.Session, officers: list[str], company_name: str) -> dict:
    domain = domain_of(company_url)
    base = company_url if company_url.startswith("http") else f"https://{company_url}"
    found: dict[str, str] = {}
    pages = _pages(base, session)
    for url, html in pages:
        # Decode both HTML entities (&#64;) and URL escapes (%40, %20 in
        # mailto: links) -- "%20info@..." was a real artefact of skipping this.
        text = unquote(unescape(html))
        for email in EMAIL.findall(text):
            email = email.lower().rstrip(".")
            # The company's own domain(s) only. Agency, CDN and placeholder
            # addresses (name@example.com) are not the company's.
            if company_domain(email.split("@")[1], domain, company_name) and keep_email(email, officers):
                found.setdefault(email, url)
    return {
        "domain": domain,
        "company_url": company_url,
        "pages_read": [u for u, _ in pages],
        "emails": [{"email": e, "source_url": u} for e, u in sorted(found.items())],
        "collected_at": datetime.now(timezone.utc).isoformat(),
    }


def from_offer_document(url: str, company_name: str) -> dict:
    """Website and inbox from the offer document's contact-details paragraph
    ("Our email address is ... Our internet address is ..."). Stops at the
    first page that has it, usually ~40 of ~500."""
    r = requests.get(url, headers={"User-Agent": DEFAULT_UA}, timeout=120)
    r.raise_for_status()
    if "pdf" not in r.headers.get("content-type", "").lower():
        raise RuntimeError(f"{url} is {r.headers.get('content-type')}, not a PDF")
    for i, page in enumerate(PdfReader(io.BytesIO(r.content)).pages[:150]):
        text = re.sub(r"\s+", " ", page.extract_text() or "")
        m = OFFER_CONTACT.search(text)
        if not m:
            continue
        para = text[max(0, m.start() - 400): m.end() + 400]
        site = WEBSITE.search(para)
        if not site:
            continue
        site_domain = site.group(1).lower().removeprefix("www.")
        # The stated website's domain or the company's own name, so a
        # sponsor's or registrar's address nearby can't pass as the company's
        # (Leong Guan's site is leongguan.com, its inbox @leong-guan.com).
        emails = sorted({e.lower().rstrip(".") for e in EMAIL.findall(para)
                         if company_domain(e.lower().rstrip(".").split("@")[1], site_domain, company_name)})
        quote = " ".join(sn for sn in re.split(r"(?<=\.) (?=[A-Z])", para)
                         if any(e in sn.lower() for e in emails) or site.group(0) in sn)
        return {"company_url": f"https://{site.group(1).lower()}", "page": i + 1, "quote": quote[:500],
                "emails": [{"email": e, "source_url": f"{url}#page={i + 1}"} for e in emails]}
    return {"company_url": None, "page": None, "quote": None, "emails": []}


def _offer_documents(records: list[dict], fresh_after: datetime) -> None:
    FILINGS.mkdir(parents=True, exist_ok=True)
    for rec in records:
        url = rec.get("filing_url") or ""
        if not url.lower().endswith(".pdf"):
            continue
        rid = f"{rec['exchange_mic']}|{rec.get('ticker') or rec['company_name']}"
        path = FILINGS / f"{hashlib.sha1(url.encode()).hexdigest()[:16]}.json"
        if path.exists() and datetime.fromisoformat(json.loads(path.read_text())["collected_at"]) > fresh_after:
            continue
        try:
            found = from_offer_document(url, rec["company_name"])
        except Exception as exc:                      # noqa: BLE001
            print(f"  {rec['company_name']}: offer document failed: {exc}")
            continue
        path.write_text(json.dumps({"record_id": rid, "filing_url": url, **found,
                                    "collected_at": datetime.now(timezone.utc).isoformat()}, indent=1))
        print(f"  {rec['company_name']:<40} offer doc p.{found['page']} site={found['company_url']} "
              f"emails={[e['email'] for e in found['emails']]}")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    records = json.loads(DATA.read_text())["records"]
    session = requests.Session()
    session.headers.update({"User-Agent": DEFAULT_UA, "Accept-Language": "en-US,en;q=0.9"})
    fresh_after = datetime.now(timezone.utc) - timedelta(days=0 if "--refresh" in sys.argv else REFRESH_DAYS)

    # Offer documents first: they supply the website for rows that lack one.
    _offer_documents(records, fresh_after)
    for rec in records:
        if not rec.get("company_url"):
            for path in FILINGS.glob("*.json"):
                doc = json.loads(path.read_text())
                if doc["record_id"] == f"{rec['exchange_mic']}|{rec.get('ticker') or rec['company_name']}":
                    rec["company_url"] = doc.get("company_url")

    for rec in records:
        url, domain = rec.get("company_url"), domain_of(rec.get("company_url"))
        # Every row with a website, listed or not: breadth of company
        # contacts is the priority. A filed inbox (Taiwan's MOPS) is enough.
        if not domain or (rec.get("company_email") and rec["exchange_mic"] == "XTAI"):
            continue
        path = OUT / f"{domain}.json"
        if path.exists():
            prev = json.loads(path.read_text())
            if datetime.fromisoformat(prev["collected_at"]) > fresh_after:
                continue
        try:
            officers = [n for n in (rec.get("ceo_name"), rec.get("cfo_name")) if n]
            result = collect(url, session, officers, rec["company_name"])
        except Exception as exc:                      # noqa: BLE001
            print(f"  {domain}: failed: {exc}")
            continue
        path.write_text(json.dumps(result, indent=1))
        print(f"  {domain:<28} pages={len(result['pages_read'])} emails={[e['email'] for e in result['emails']]}")


if __name__ == "__main__":
    main()
