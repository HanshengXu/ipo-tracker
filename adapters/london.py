"""London Stock Exchange -- upcoming issues (in practice: AIM).

Endpoints:
  https://api.londonstockexchange.com/api/v1/pages?path=live-markets/new-issues
      -> component "upcoming-issues": name, market, offer size, first trading date, link
  https://api.lsegissuerservices.com/preipoissuers/profile/{slug}
      -> the pre-IPO profile: description, website, DIRECTORS WITH TITLES, advisers
Protection: none observed. Plain requests. Verified working: 2026-10-18.

This is the best contact source in the project: the CEO and CFO names come
from the issuer's own LSE profile, not from inference. Where a profile has no
CEO/CFO title (Axies Ventures lists two "Executive Directors"), the field
stays None -- picking one would be a guess.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import requests

from core.http import DEFAULT_UA, get_json
from core.schema import IPORecord

PAGES_API = "https://api.londonstockexchange.com/api/v1/pages"
PROFILE_API = "https://api.lsegissuerservices.com/preipoissuers/profile/{slug}"
PROFILE_PAGE = "https://profile.lsegissuerservices.com/upcoming-issues/{slug}"
REFERER = "https://www.londonstockexchange.com/"

CEO_ROLE = re.compile(r"\b(chief executive|ceo)\b", re.I)
CFO_ROLE = re.compile(r"\b(chief financial|cfo|finance director)\b", re.I)


def _exact_date(text: str | None):
    """Only a full calendar date becomes a date. "Late October 2026" -> None."""
    for fmt in ("%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime((text or "").strip(), fmt).date()
        except ValueError:
            continue
    return None


def _clean(text) -> str | None:
    text = (text or "").strip()
    return None if text in ("", "-", "TBC", "To be defined") else text


def _url(site: str | None) -> str | None:
    """Profiles give both 'https://voltage-x.com' and 'www.liberty-minerals.com'."""
    site = _clean(site)
    if not site:
        return None
    return site if site.startswith(("http://", "https://")) else f"https://{site}"


def _one_role(directors: list[dict], pattern: re.Pattern, label: str, warnings: list[str]):
    """Name of the single director whose title matches. Two matches (co-CEOs,
    or a CEO plus an 'incoming CEO') is ambiguity: record it, don't choose."""
    hits = [" ".join((d.get("name") or "").split()) for d in directors
            if pattern.search(d.get("role") or "")]
    if len(hits) > 1:
        warnings.append(f"Multiple {label} titles on profile: {', '.join(hits)}")
        return None
    return hits[0] if hits else None


def _upcoming(session: requests.Session) -> list[dict]:
    page = get_json(session, PAGES_API, referer=REFERER,
                    params={"path": "live-markets/new-issues", "parameters": ""})
    for comp in page.get("components") or []:
        if comp.get("type") == "upcoming-issues":
            for block in comp.get("content") or []:
                return (block.get("value") or {}).get("Items") or []
    raise RuntimeError("no 'upcoming-issues' component on the new-issues page")


def _to_record(item: dict, profile: dict, *, fetched_at: datetime) -> IPORecord:
    slug = (item.get("link") or "").rstrip("/").rsplit("/", 1)[-1]
    offer = profile.get("offerDetails") or {}
    directors = profile.get("directors") or []
    advisers = profile.get("advisors") or []
    warnings: list[str] = []

    date_raw = _clean(offer.get("expectedFirstDayTrading") or item.get("firsttradingdate"))
    size_raw = _clean(offer.get("totalExpectedSizeOffer") or item.get("primaryoffersize"))

    return IPORecord(
        company_name=(profile.get("name") or item.get("name") or "UNKNOWN").strip(),
        exchange_mic="XLON",
        board=(offer.get("market") or item.get("market") or "UNKNOWN").strip(),

        expected_listing_date=_exact_date(date_raw),
        expected_listing_date_raw=date_raw,
        # Being on LSE's upcoming-issues list means the pre-admission
        # announcement has been made (for AIM, the 10-day Schedule One notice).
        listing_status="FILED",
        listing_status_raw="LSE upcoming issue",

        business_description=_clean(profile.get("description")),
        offer_size_raw=size_raw,

        company_url=_url(profile.get("website")),
        ceo_name=_one_role(directors, CEO_ROLE, "CEO", warnings),
        cfo_name=_one_role(directors, CFO_ROLE, "CFO", warnings),

        # Nominated Adviser vouches for the company to the exchange (AIM's
        # gatekeeper); brokers sell the shares.
        financial_advisors=[a["name"].strip() for a in advisers
                            if a.get("name") and "broker" not in (a.get("role") or "").lower()],
        underwriters=[a["name"].strip() for a in advisers
                      if a.get("name") and "broker" in (a.get("role") or "").lower()],

        source_url=PROFILE_PAGE.format(slug=slug) if slug else REFERER + "live-markets/new-issues",
        source_type="EXCHANGE_API",
        source_as_of=None,          # neither endpoint exposes a freshness stamp
        fetched_at=fetched_at,
        data_warnings=warnings,
    )


def fetch() -> list[IPORecord]:
    fetched_at = datetime.now(timezone.utc)
    session = requests.Session()
    session.headers.update({"User-Agent": DEFAULT_UA})

    try:
        items = _upcoming(session)
    except Exception as exc:                          # noqa: BLE001
        print(f"[london] FAILED: {exc}")
        return []

    records: list[IPORecord] = []
    for item in items:
        if (item.get("type") or "Equity") != "Equity":
            continue
        slug = (item.get("link") or "").rstrip("/").rsplit("/", 1)[-1]
        profile: dict = {}
        if slug:
            try:
                profile = get_json(session, PROFILE_API.format(slug=slug),
                                   referer="https://profile.lsegissuerservices.com/")
            except Exception as exc:                  # noqa: BLE001
                # Row survives with list-level fields only.
                print(f"[london] profile failed for {item.get('name')}: {exc}")
        try:
            records.append(_to_record(item, profile, fetched_at=fetched_at))
        except Exception as exc:                      # noqa: BLE001
            print(f"[london] skipped {item.get('name')}: {exc}")

    print(f"[london] {len(items)} upcoming issues, {len(records)} equity records")
    return records


if __name__ == "__main__":
    for rec in fetch():
        print(f"{rec.board:<5} {(rec.expected_listing_date_raw or '-'):<20} "
              f"{(rec.offer_size_raw or 'N/A'):<10} {rec.company_name[:32]:<32} "
              f"CEO={rec.ceo_name or 'N/A'} | CFO={rec.cfo_name or 'N/A'} | {rec.company_url}")
        for w in rec.data_warnings:
            print(f"      WARNING: {w}")
