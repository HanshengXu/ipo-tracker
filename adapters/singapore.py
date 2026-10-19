"""Singapore Exchange -- Catalist board, via Catalodge.

Catalodge is where Catalist sponsors lodge preliminary offer documents before
registration, so it is the real pre-IPO pipeline rather than an archive of
completed offers (which is what api.sgx.com/ipoprospectus is).

Endpoints (SGX's headless-CMS GraphQL API, "persisted query" style):
  https://www.sgx.com/config/appconfig.json  -> CMS_VERSION
  https://api2.sgx.com/content-api?queryId={CMS_VERSION}:catalodge_list
  https://api2.sgx.com/content-api?queryId={CMS_VERSION}:page   (detail, by path)
Protection: none observed. Plain requests, no cookies.
Verified working: 2026-10-18.

Scope (agreed with Hang): IPO lodgements, registrations and withdrawals within
RECENT_DAYS. Follow-on share issuances and RTOs are in the same feed and
deliberately excluded -- they are not IPOs. Main board is out of scope: GEM's
customer is Catalist, and SGX has no main-board pipeline feed.

Listing cross-check: a Registered offer may already be trading. Each one is
matched against SGX's live securities list (api.sgx.com/securities/v1.1);
a confident, unique Catalist match upgrades it to LISTED and sets the ticker.
No confident match -> it stays EFFECTIVE. Recall is traded for zero false
positives: "The Place Hldg" (main board) must never be read as The Assembly
Place (which trades as "TAP" and so stays unconfirmed).
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from core.http import DEFAULT_UA, get_json
from core.schema import IPORecord

CONFIG = "https://www.sgx.com/config/appconfig.json"
API = "https://api2.sgx.com/content-api"
SECURITIES = "https://api.sgx.com/securities/v1.1"
SITE = "https://www.sgx.com"
REFERER = "https://www.sgx.com/"

PAGE_SIZE = 100
RECENT_DAYS = 365

# Words that carry no identity. SGX short names abbreviate them ("Hldgs"),
# so they are dropped from both sides before matching.
NAME_FILLER = {"THE", "LTD", "LIMITED", "HOLDINGS", "HOLDING", "HLDGS", "HLDG",
               "CO", "COMPANY", "CORP", "CORPORATION", "GROUP", "GRP", "INC", "PLC"}

# SGX stores dates as epoch seconds at MIDNIGHT SINGAPORE TIME. Read in UTC,
# 1790524800 is 2026-09-27 -- one day early. The URL slug says 2026-09-28.
SGT = ZoneInfo("Asia/Singapore")

STATUS_MAP = {
    "lodged": "FILED",          # preliminary offer document lodged with SGX RegCo
    "registered": "EFFECTIVE",  # offer document registered: cleared to offer
    "withdrawn": "WITHDRAWN",
}


def _sgt_date(epoch) -> date | None:
    if not epoch:
        return None
    try:
        return datetime.fromtimestamp(int(epoch), SGT).date()
    except (TypeError, ValueError, OverflowError):
        return None


def _term(field) -> str | None:
    """Taxonomy terms arrive triple-wrapped: {"data": {"data": {"id", "name"}}}."""
    try:
        return field["data"]["data"]["name"]
    except (KeyError, TypeError):
        return None


def _cms_version(session: requests.Session) -> str:
    """The persisted-query namespace is a deploy hash. It changes whenever SGX
    ships a new site build, so it is read at runtime -- same reasoning as never
    hardcoding cookies."""
    cfg = get_json(session, CONFIG, referer=REFERER)
    version = cfg.get("CMS_VERSION")
    if not version:
        raise RuntimeError("appconfig.json has no CMS_VERSION")
    return version


def _query(session: requests.Session, version: str, query_id: str, **variables) -> dict:
    """Run one persisted query. GraphQL reports failure as 200 OK with an
    `errors` key, so a status-code check alone would let errors through as
    empty data."""
    payload = get_json(
        session, API, referer=REFERER,
        params={"queryId": f"{version}:{query_id}",
                "variables": json.dumps({"lang": "EN", **variables})},
    )
    if payload.get("errors"):
        raise RuntimeError(f"{query_id}: {payload['errors'][0].get('message')}")
    return payload.get("data") or {}


def _list_all(session: requests.Session, version: str) -> list[dict]:
    """Page through every lodgement. ~460 rows = 5 calls; cheap enough that we
    don't rely on the API's sort order to stop early."""
    rows: list[dict] = []
    offset = 0
    while True:
        data = _query(session, version, "catalodge_list", offset=offset, limit=PAGE_SIZE)
        page = (data.get("list") or {}).get("results") or []
        total = (data.get("list") or {}).get("count") or 0
        rows.extend(r.get("data") or {} for r in page)
        offset += PAGE_SIZE
        if not page or offset >= total:
            return rows


def _in_scope(row: dict, cutoff: date) -> bool:
    if _term(row.get("offerType")) != "IPO":
        return False
    # The recency window applies to EVERY status, Lodged included: SGX never
    # closes out stale lodgements (TG Corporation has sat at "Lodged" since
    # 2012). The list payload has no withdrawal date, so lodgement/registration
    # date is the recency proxy.
    latest = max(filter(None, [_sgt_date(row.get("lodgementDate")),
                               _sgt_date(row.get("registrationDate"))]), default=None)
    return latest is not None and latest >= cutoff


def _first_pdf(widget) -> str | None:
    """Dig a PDF URL out of a download widget. Five levels of nesting, any of
    which can be null -- defensive at each step, like thailand._offering_window."""
    try:
        items = widget["data"]["downloadItems"] or []
        return items[0]["data"]["file"]["data"]["file"]["data"]["url"]
    except (KeyError, IndexError, TypeError):
        return None


def _detail(session: requests.Session, version: str, path: str) -> dict:
    # route.data is a wrapper; the catalodge fields sit one level further in.
    data = _query(session, version, "page", path=path)
    route_data = ((data.get("route") or {}).get("data")) or {}
    return route_data.get("data") or {}


def _name_tokens(name: str) -> list[str]:
    words = re.findall(r"[A-Z0-9]+", (name or "").upper())
    return [w for w in words if w not in NAME_FILLER]


def _short_name_matches(short: list[str], full: list[str]) -> bool:
    """SGX's display name "Infinity Dev" vs Catalodge's "Infinity Development
    Holdings Company Limited". Each short word must START the full word in the
    same position, aligned from the first word. Initialisms ("TAP") fail."""
    return (0 < len(short) <= len(full)
            and all(f.startswith(s) for s, f in zip(short, full)))


def _catalist_stocks(session: requests.Session) -> list[dict]:
    payload = get_json(session, SECURITIES, referer=REFERER,
                       params={"excludetypes": "bonds"})
    prices = (payload.get("data") or {}).get("prices") or []
    return [p for p in prices
            if p.get("type") == "stocks" and (p.get("m") or "").upper() == "CATALIST"]


def _listed_ticker(company: str, stocks: list[dict]) -> str | None:
    """Ticker if exactly one Catalist stock confidently matches, else None.
    Two matches is ambiguity, and ambiguity is not evidence."""
    full = _name_tokens(company)
    hits = [s for s in stocks if _short_name_matches(_name_tokens(s.get("n")), full)]
    return hits[0].get("nc") if len(hits) == 1 else None


def _to_record(row: dict, detail: dict, *, fetched_at: datetime,
               ticker: str | None = None) -> IPORecord:
    status_raw = _term(row.get("lodgeStatus"))
    path = (row.get("link") or {}).get("url") or ""
    # Prefer the final (registered) offer document; fall back to the preliminary.
    filing_url = (_first_pdf(detail.get("finalOfferDocument"))
                  or _first_pdf(detail.get("preliminaryOfferDocument")))
    sponsors = [((s.get("data") or {}).get("title") or "").strip()
                for s in row.get("sponsor") or []]

    return IPORecord(
        company_name=(row.get("company") or row.get("title") or "UNKNOWN").strip(),
        exchange_mic="XSES",
        board="Catalist",
        ticker=ticker,

        filed_date=_sgt_date(row.get("lodgementDate")),
        # Catalodge records lodgement and registration, not a listing date.
        # Leaving it None is honest; inferring "registration + N weeks" is not.
        expected_listing_date=None,
        # listing_status_raw keeps SGX's word ("Registered") even when the
        # cross-check upgrades our status to LISTED -- the ticker is the evidence.
        listing_status="LISTED" if ticker else STATUS_MAP.get((status_raw or "").lower(), "UNKNOWN"),
        listing_status_raw=status_raw,

        # Sector, description, price and size live in the Product Highlights
        # Sheet PDF -- that's enrichment work, not this adapter's job.
        currency="SGD",
        filing_url=filing_url,

        # A Catalist sponsor plays the role of financial adviser / nominated
        # adviser: they vouch for the issuer to SGX RegCo.
        financial_advisors=[s for s in sponsors if s],

        source_url=f"{SITE}{path}" if path else f"{SITE}/regulation/catalodge",
        source_type="EXCHANGE_API",
        source_as_of=None,          # the API exposes no freshness stamp
        fetched_at=fetched_at,
    )


def fetch(recent_days: int = RECENT_DAYS) -> list[IPORecord]:
    """Pull Catalist IPO lodgements. Returns [] rather than raising if the
    list call fails; a failed detail call costs one row its filing_url, not
    the row itself."""
    fetched_at = datetime.now(timezone.utc)
    cutoff = datetime.now(SGT).date() - timedelta(days=recent_days)

    session = requests.Session()
    session.headers.update({"User-Agent": DEFAULT_UA, "Origin": SITE})

    try:
        version = _cms_version(session)
        rows = _list_all(session, version)
    except Exception as exc:                          # noqa: BLE001
        print(f"[singapore] FAILED: {exc}")
        return []

    # The cross-check is an enhancement, not a dependency: if the securities
    # list is down, rows ship with their Catalodge status and nothing breaks.
    try:
        stocks = _catalist_stocks(session)
    except Exception as exc:                          # noqa: BLE001
        print(f"[singapore] listing cross-check unavailable: {exc}")
        stocks = []

    records: list[IPORecord] = []
    kept = [r for r in rows if _in_scope(r, cutoff)]
    for row in kept:
        is_registered = (_term(row.get("lodgeStatus")) or "").lower() == "registered"
        ticker = _listed_ticker(row.get("company") or "", stocks) if is_registered else None
        path = (row.get("link") or {}).get("url")
        detail: dict = {}
        if path:
            try:
                detail = _detail(session, version, path)
            except Exception as exc:                  # noqa: BLE001
                print(f"[singapore] detail failed for {row.get('company')}: {exc}")
        try:
            records.append(_to_record(row, detail, fetched_at=fetched_at, ticker=ticker))
        except Exception as exc:                      # noqa: BLE001
            print(f"[singapore] skipped {row.get('company')}: {exc}")

    print(f"[singapore] {len(rows)} lodgements, {len(kept)} IPOs in scope")
    return records


if __name__ == "__main__":
    for rec in fetch():
        print(f"{rec.listing_status:<10}  {(rec.listing_status_raw or '-'):<10}  "
              f"{rec.ticker or '-':<5}  {rec.filed_date}  "
              f"{rec.company_name[:45]:<45}  {', '.join(rec.financial_advisors)[:30]}")
        print(f"{'':>22}filing: {rec.filing_url or 'N/A'}")
