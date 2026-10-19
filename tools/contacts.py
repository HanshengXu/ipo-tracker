"""Collect email addresses published on each company's own website -- run
LOCALLY, results committed to data/enriched/contacts/<domain>.json.

    python -m tools.contacts

Only addresses at the company's own domain are kept, along with the page they
appeared on -- and of those, only generic inboxes (ir@, info@) and addresses
belonging to a NAMED officer of that company. Ordinary staff addresses found on
the same pages are dropped: the pipeline has no use for them, and this repo is
public, so republishing them would be wrong. This step only COLLECTS. Deciding whether an address belongs to
the CEO (VERIFIED), implies a naming pattern (INFERRED), or is a generic inbox
is done by rules in core/enrich.py -- never by guessing.

Politeness: at most a handful of pages per site, one second apart, and a
cache refreshed only when older than REFRESH_DAYS.
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.enrich import GENERIC, _name_parts, _pattern_for  # noqa: E402
from core.http import DEFAULT_UA  # noqa: E402

DATA = ROOT / "docs" / "data" / "ipos.json"
OUT = ROOT / "data" / "enriched" / "contacts"
UPCOMING = {"ITF", "FILED", "APPROVED", "EFFECTIVE", "PRICED", "UNKNOWN"}
REFRESH_DAYS = 14
CANDIDATE_PATHS = ["", "/contact", "/contact-us", "/contactus", "/en/contact", "/investor-relations", "/ir"]
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
LINK = re.compile(r'href="([^"#]+)"', re.I)


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
            for href in LINK.findall(r.text):
                if re.search(r"contact|investor", href, re.I):
                    queue.insert(0, urljoin(r.url, href))
        time.sleep(1.0)
    return out


def _keep(email: str, officers: list[str]) -> bool:
    local = email.split("@")[0]
    return local in GENERIC or any(_pattern_for(local, _name_parts(n)) for n in officers)


def collect(company_url: str, session: requests.Session, officers: list[str]) -> dict:
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
            # Own domain only (or a subdomain of it). Agency, CDN and
            # placeholder addresses (name@example.com) are not the company's.
            own = domain and (email.endswith("@" + domain) or email.endswith("." + domain))
            if own and _keep(email, officers):
                found.setdefault(email, url)
    return {
        "domain": domain,
        "company_url": company_url,
        "pages_read": [u for u, _ in pages],
        "emails": [{"email": e, "source_url": u} for e, u in sorted(found.items())],
        "collected_at": datetime.now(timezone.utc).isoformat(),
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    records = json.loads(DATA.read_text())["records"]
    session = requests.Session()
    session.headers.update({"User-Agent": DEFAULT_UA, "Accept-Language": "en-US,en;q=0.9"})
    fresh_after = datetime.now(timezone.utc) - timedelta(days=REFRESH_DAYS)

    for rec in records:
        url, domain = rec.get("company_url"), domain_of(rec.get("company_url"))
        if rec["listing_status"] not in UPCOMING or not domain:
            continue
        path = OUT / f"{domain}.json"
        if path.exists():
            prev = json.loads(path.read_text())
            if datetime.fromisoformat(prev["collected_at"]) > fresh_after:
                continue
        try:
            officers = [n for n in (rec.get("ceo_name"), rec.get("cfo_name")) if n]
            result = collect(url, session, officers)
        except Exception as exc:                      # noqa: BLE001
            print(f"  {domain}: failed: {exc}")
            continue
        path.write_text(json.dumps(result, indent=1))
        print(f"  {domain:<28} pages={len(result['pages_read'])} emails={[e['email'] for e in result['emails']]}")


if __name__ == "__main__":
    main()
