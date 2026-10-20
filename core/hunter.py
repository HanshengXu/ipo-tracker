"""Hunter.io domain search: addresses Hunter has seen published on the web,
re-checked by us before they are used. Runs inside the daily cron.

    GET https://api.hunter.io/v2/domain-search?domain=<company domain>

What Hunter returns is a lead, not evidence. Each address comes with the
pages Hunter saw it on; an address is kept only if
  1. it belongs to the company (its domain, or a sibling domain spelling the
     company's name -- core/enrich.company_domain),
  2. it is a generic inbox or a NAMED officer's own address
     (core/enrich.keep_email; other staff addresses are never stored), and
  3. we fetch one of Hunter's source pages ourselves and find the address on
     it. That page becomes the evidence. Hunter's own "still_on_page" flag
     and confidence score are recorded but never trusted on their own.
Addresses Hunter derived from a pattern (no source page) are dropped: that
is a guess, and this project doesn't publish guesses.

Budget: the free plan has 50 searches a month, and a search that returns at
least one address costs one. Only upcoming IPOs are searched (pre-IPO
companies are the point; ~50 domains already exceed a month of credits).
Each run spends at most PER_RUN, never touches the last RESERVE, searches
each domain at most once per REFRESH_DAYS, companies with no email first. With no
HUNTER_API_KEY in the environment the step is skipped and nothing changes.

Results are cached at data/enriched/hunter/<domain>.json and committed, so a
domain is never paid for twice inside the refresh window.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from urllib.parse import unquote

import requests

from core.enrich import company_domain, domain_of, keep_email
from core.http import DEFAULT_UA

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "enriched" / "hunter"
API = "https://api.hunter.io/v2"
UPCOMING = {"ITF", "FILED", "APPROVED", "EFFECTIVE", "PRICED", "UNKNOWN"}
PER_RUN = 5            # searches per daily run
RESERVE = 5            # searches left untouched for manual use
REFRESH_DAYS = 90
MAX_SOURCES = 3        # source pages we fetch per address before giving up


def _key() -> str | None:
    """From the environment (GitHub secret in CI), else the local .env file.
    The value is never printed."""
    if os.environ.get("HUNTER_API_KEY"):
        return os.environ["HUNTER_API_KEY"].strip()
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            name, _, value = line.partition("=")
            if name.strip().removeprefix("export ").strip() == "HUNTER_API_KEY":
                return value.strip().strip("\"'") or None
    return None


def _remaining(session: requests.Session) -> int:
    """Searches left this month. The account endpoint itself is free."""
    r = session.get(f"{API}/account", timeout=30)
    r.raise_for_status()
    return int(r.json()["data"]["requests"]["searches"]["remaining"])


def _on_page(email: str, url: str) -> bool:
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": DEFAULT_UA})
    except requests.RequestException:
        return False
    return r.ok and email in unquote(unescape(r.text)).lower()


def _search(session: requests.Session, domain: str, company_name: str, officers: list[str]) -> dict:
    r = session.get(f"{API}/domain-search", params={"domain": domain, "limit": 10}, timeout=30)
    r.raise_for_status()
    data = r.json()["data"]
    kept, dropped = [], 0
    for e in data.get("emails") or []:
        email = (e.get("value") or "").lower()
        if not email or not company_domain(email.split("@")[1], domain, company_name) or not keep_email(email, officers):
            dropped += 1                                  # not the company's, or a staff address: not stored
            continue
        sources = [s["uri"] for s in e.get("sources") or [] if s.get("uri")]
        page = next((u for u in sources[:MAX_SOURCES] if _on_page(email, u)), None)
        if not page:
            dropped += 1                                  # pattern-derived or no longer published
            continue
        kept.append({"email": email, "source_url": page, "hunter_confidence": e.get("confidence"),
                     "hunter_type": e.get("type")})
    return {"domain": domain, "pattern": data.get("pattern"), "emails": kept,
            "returned": len(data.get("emails") or []), "dropped": dropped,
            "searched_at": datetime.now(timezone.utc).isoformat()}


def _candidates(records) -> list:
    """One upcoming record per domain. Breadth first: companies with no
    email of any kind come first, then those missing a personal address;
    best GEM fit first within each."""
    seen, out = set(), []
    upcoming = [r for r in records if r.listing_status in UPCOMING]
    for rec in sorted(upcoming, key=lambda r: (bool(r.company_email),
                                               bool(r.ceo_email or r.cfo_email),
                                               -(r.gem_fit_score or 0))):
        d = domain_of(rec.company_url)
        if d and d not in seen:
            seen.add(d)
            out.append((d, rec))
    return out


def refresh(records) -> str:
    """Search due domains within budget. Returns a line for the run log."""
    key = _key()
    if not key:
        return "skipped: no HUNTER_API_KEY"
    CACHE.mkdir(parents=True, exist_ok=True)
    stale_before = datetime.now(timezone.utc) - timedelta(days=REFRESH_DAYS)
    due = []
    for domain, rec in _candidates(records):
        path = CACHE / f"{domain}.json"
        if path.exists() and datetime.fromisoformat(json.loads(path.read_text())["searched_at"]) > stale_before:
            continue
        due.append((domain, rec))
    if not due:
        return "nothing due"

    session = requests.Session()
    session.headers.update({"X-API-KEY": key})             # header, so the key never sits in a logged URL
    budget = min(PER_RUN, _remaining(session) - RESERVE)
    searched, found = 0, 0
    for domain, rec in due:
        if searched >= budget:
            break
        try:
            result = _search(session, domain, rec.company_name, [n for n in (rec.ceo_name, rec.cfo_name) if n])
        except (requests.RequestException, KeyError, ValueError) as exc:
            print(f"[hunter] {domain}: {type(exc).__name__}")   # never the URL or headers
            continue
        (CACHE / f"{domain}.json").write_text(json.dumps(result, indent=1))
        searched += 1
        found += len(result["emails"])
        print(f"[hunter] {domain}: {result['returned']} returned, kept {[e['email'] for e in result['emails']]}")
        time.sleep(0.5)
    return f"{searched} searched, {found} addresses kept, {len(due) - searched} domains still due"
