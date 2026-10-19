"""Taiwan Stock Exchange -- main board and TIB (Taiwan Innovation Board).

Endpoint: https://www.twse.com.tw/rwd/en/company/applylisting?response=json
Protection: none observed. Plain requests. Verified working: 2026-10-18.

Each row records how far an application has got through TWSE's own ladder:
  applied -> Listing Review Committee -> TWSE Board approval
          -> agreement filed with the FSC -> listing date
Status is derived from the furthest rung with a date, so listing_status_raw
names that rung -- no inference beyond the exchange's own dates.

Stale rows: applications from 2021 still have no listing date and no status
change. They were almost certainly withdrawn or rejected, but TWSE doesn't
say so. Same fix as SGX: a recency window on the application date.

Company profile: MOPS, the TWSE-run disclosure system, has a structured
profile for every public company -- and most applicants already trade on the
emerging-stock board, so they have one:
  POST https://mops.twse.com.tw/mops/api/t05st03  {"companyId": "<code>"}
It supplies the English legal name, website, IR email, main business (in
Chinese) and the 總經理. That title is MOPS's "President": the chief executive
of a Taiwanese company, mapped to ceo_name with the original title recorded
in field_sources. A failed profile call costs the row its enrichment, never
the row.
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from core.http import DEFAULT_UA, get_json
from core.schema import IPORecord

API = "https://www.twse.com.tw/rwd/en/company/applylisting"
PAGE = "https://www.twse.com.tw/en/listed/listed/apply-listing.html"
MOPS_API = "https://mops.twse.com.tw/mops/api/t05st03"
MOPS_PAGE = "https://mops.twse.com.tw/mops/#/web/t05st03"
RECENT_DAYS = 365
TPE = ZoneInfo("Asia/Taipei")

# Column positions, checked against the payload's own `fields` list at
# runtime -- if TWSE reorders columns, we fail loudly instead of mislabelling.
EXPECTED_FIELDS = {
    1: "Code", 2: "Company", 3: "Application Date",
    5: "Date of the Listing Review Committee",
    6: "Date the application approved by the TWSE Board",
    7: "Date the Agreement for Listing submitted to the FSC",
    8: "Listing Date", 9: "Underwriter", 10: "Underwriting price", 11: "Note",
}


def _date(text) -> date | None:
    try:
        return datetime.strptime((text or "").strip(), "%Y/%m/%d").date()
    except ValueError:
        return None


def _price(value) -> float | None:
    try:
        return float(str(value).replace(",", "")) if value not in ("", None) else None
    except ValueError:
        return None


def _check_fields(fields: list[str]) -> None:
    for idx, prefix in EXPECTED_FIELDS.items():
        if idx >= len(fields) or not fields[idx].strip().startswith(prefix):
            raise RuntimeError(f"TWSE column {idx} is {fields[idx:idx+1]}, expected {prefix!r}")


def _stage(row: list, today: date) -> tuple[str, str]:
    review, board_ok, fsc, listing = (_date(row[i]) for i in (5, 6, 7, 8))
    if listing and listing <= today:
        return "LISTED", f"listed {listing}"
    if listing:
        return "APPROVED", f"listing scheduled {listing}"
    if fsc:
        return "APPROVED", f"listing agreement filed with FSC {fsc}"
    if board_ok:
        return "APPROVED", f"approved by TWSE Board {board_ok}"
    if review:
        return "FILED", f"Listing Review Committee met {review}"
    return "FILED", "application submitted"


def _mops_profile(session: requests.Session, code: str) -> dict:
    """{field: value} for the visible fields of a MOPS company profile."""
    r = session.post(MOPS_API, data=json.dumps({"companyId": code}), timeout=30,
                     headers={"Content-Type": "application/json",
                              "Origin": "https://mops.twse.com.tw",
                              "Referer": "https://mops.twse.com.tw/mops/"})
    r.raise_for_status()
    if "json" not in r.headers.get("content-type", ""):
        raise RuntimeError(f"MOPS returned {r.headers.get('content-type')}")
    result = (r.json() or {}).get("result") or {}
    return {k: v.get("value") for k, v in result.items()
            if isinstance(v, dict) and not v.get("isHidden") and v.get("value") not in ("", "-", None)}


def _apply_mops(rec: IPORecord, p: dict) -> None:
    src = f"{MOPS_PAGE} (company {rec.ticker})"
    rec.company_name = (p.get("companyEnglishName") or rec.company_name).strip()
    rec.company_name_local = p.get("companyName")
    rec.sector_raw = p.get("industryCategory")
    rec.business_description_local = p.get("mainBusiness")
    site = p.get("internetAddress") or p.get("mops_url_2")
    if site:
        rec.company_url = site if site.startswith("http") else f"https://{site}"
    if p.get("president"):
        rec.ceo_name = p["president"]
        rec.field_sources["ceo_name"] = {"method": "exchange-filing", "source_url": src,
                                         "quote": f"總經理 (President): {p['president']}"}
    if p.get("email"):
        # A company IR inbox from the official filing -- a company contact,
        # never the CEO's personal address.
        rec.company_email = p["email"].strip().lower()
        rec.company_email_evidence = src


def _to_record(row: list, *, today: date, fetched_at: datetime) -> IPORecord:
    status, status_raw = _stage(row, today)
    underwriter = (row[9] or "").strip()
    return IPORecord(
        company_name=(row[2] or "").strip() or row[1],
        ticker=(row[1] or "").strip() or None,
        exchange_mic="XTAI",
        board="TIB" if (row[11] or "").strip() == "TIB" else "Main",

        filed_date=_date(row[3]),
        expected_listing_date=_date(row[8]),
        listing_status=status,
        listing_status_raw=status_raw,

        currency="TWD",
        offer_price_local=_price(row[10]),
        # Column 4 is PAID-IN CAPITAL at application (thousand TWD), not a
        # valuation or a raise. Multiplying it by the IPO price would undercount
        # the new shares the IPO itself creates -- so valuation stays None.

        underwriters=[underwriter] if underwriter else [],

        source_url=PAGE,
        source_type="EXCHANGE_API",
        source_as_of=None,          # payload carries no freshness stamp
        fetched_at=fetched_at,
    )


def fetch(recent_days: int = RECENT_DAYS) -> list[IPORecord]:
    fetched_at = datetime.now(timezone.utc)
    today = datetime.now(TPE).date()
    cutoff = today - timedelta(days=recent_days)

    session = requests.Session()
    session.headers.update({"User-Agent": DEFAULT_UA})
    try:
        payload = get_json(session, API, referer=PAGE, params={"response": "json"})
        if (payload.get("stat") or "").lower() != "ok":
            raise RuntimeError(f"stat={payload.get('stat')!r}")
        _check_fields(payload.get("fields") or [])
    except Exception as exc:                          # noqa: BLE001
        print(f"[taiwan] FAILED: {exc}")
        return []

    rows = payload.get("data") or []
    records: list[IPORecord] = []
    for row in rows:
        try:
            applied = _date(row[3])
            if not applied or applied < cutoff:
                continue
            records.append(_to_record(row, today=today, fetched_at=fetched_at))
        except Exception as exc:                      # noqa: BLE001
            print(f"[taiwan] skipped {row[:3]}: {exc}")

    profiled = 0
    for rec in records:
        try:
            _apply_mops(rec, _mops_profile(session, rec.ticker))
            profiled += 1
        except Exception as exc:                      # noqa: BLE001
            print(f"[taiwan] MOPS profile failed for {rec.ticker}: {exc}")
        time.sleep(0.4)                               # politeness between profile calls

    print(f"[taiwan] {len(rows)} applications since 2000, {len(records)} filed in last {recent_days}d, "
          f"{profiled} MOPS profiles")
    return records


if __name__ == "__main__":
    for rec in fetch():
        print(f"{rec.ticker:<5} {rec.board:<4} {rec.listing_status:<9} {rec.filed_date}  "
              f"{(rec.listing_status_raw or ''):<44} {rec.company_name[:24]:<24} "
              f"{rec.offer_price_local or ''}")
