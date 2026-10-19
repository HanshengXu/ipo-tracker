"""Stock Exchange of Thailand (SET) + Market for Alternative Investment (mai).

Endpoint:  https://www.set.or.th/api/set/ipo/upcoming?type={mai|set}&lang=en
Protection: Incapsula. Requires the warm-session pattern (see core/http.py).
Verified working: 2026-10-18.

This is the reference adapter. Every other exchange adapter should expose the
same `fetch() -> list[IPORecord]` signature and nothing else.
"""

from __future__ import annotations

from datetime import datetime, timezone

from core.http import get_json, warm_session
from core.schema import IPORecord

API = "https://www.set.or.th/api/set/ipo/upcoming"
PAGE = "https://www.set.or.th/en/listing/ipo/upcoming-ipo/{board}"

# SET publishes eight industry codes. Mapping them to our taxonomy is a guess,
# which is exactly why `sector_raw` keeps the original alongside it.
SECTOR_MAP = {
    "AGRO": "Agriculture & Food",
    "CONSUMP": "Consumer",
    "FINCIAL": "Financials",
    "INDUS": "Industrials",
    "PROPCON": "Real Estate & Construction",
    "RESOURC": "Energy & Resources",
    "SERVICE": "Services",
    "TECH": "Technology",
}

STATUS_MAP = {
    "submitted": "FILED",       # filing lodged with the Thai SEC
    "approved": "APPROVED",     # offer approved
    "effective": "EFFECTIVE",   # filing effective, cleared to offer
}


def _parse_dt(value):
    """SET returns '2026-10-09T00:00:00+07:00' or null. Return a date or None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except (TypeError, ValueError):
        return None


def _offering_window(row: dict):
    """Dig the subscription window out of a doubly-nested, often-empty structure.

    Shape: targetGroupOfferingPeriods[].periods[].{begin,end}
    Defensive at every level -- any of these can be absent or empty, and a
    KeyError here would take down the whole exchange's rows.
    """
    groups = row.get("targetGroupOfferingPeriods") or []
    for group in groups:
        for period in group.get("periods") or []:
            begin, end = _parse_dt(period.get("begin")), _parse_dt(period.get("end"))
            if begin or end:
                return begin, end
    return None, None


def _to_record(row: dict, *, board_param: str, source_url: str,
               as_of, fetched_at) -> IPORecord:
    industry_code = (row.get("industry") or "").strip()
    offer_open, offer_close = _offering_window(row)
    status_raw = row.get("status")

    return IPORecord(
        company_name=row.get("nameEn") or row.get("symbol") or "UNKNOWN",
        company_name_local=row.get("nameTh"),
        ticker=row.get("symbol"),
        exchange_mic="XBKK",
        # SET's own `market` field is authoritative; board_param is the fallback.
        board=row.get("market") or ("mai" if board_param == "mai" else "Main"),

        expected_listing_date=_parse_dt(row.get("firstTradeDate")),
        offer_open=offer_open,
        offer_close=offer_close,
        listing_status=STATUS_MAP.get((status_raw or "").strip().lower(), "UNKNOWN"),
        listing_status_raw=status_raw,

        sector=SECTOR_MAP.get(industry_code),
        sector_raw=row.get("industryName") or industry_code or None,
        business_description=(row.get("businessDescription") or "").strip() or None,

        currency="THB",
        offer_price_local=row.get("ipoPrice"),
        # NOTE: `issuedSize` is PROCEEDS IN BAHT, not a share count.
        # Verified: 32,000,000 shares x 5.65 THB = 180,800,000 = the issuedSize
        # reported for QUICK. Field names lie; arithmetic doesn't.
        offer_size_local=row.get("issuedSize"),
        valuation_local=row.get("marketCap"),

        company_url=(row.get("url") or "").strip() or None,
        filing_url=(row.get("filingUrl") or "").strip() or None,

        financial_advisors=list(row.get("financialAdvisors") or []),
        underwriters=list(row.get("underwriters") or []),

        source_url=source_url,
        source_type="EXCHANGE_API",
        source_as_of=as_of,
        fetched_at=fetched_at,
    )


def fetch(boards=("mai", "set")) -> list[IPORecord]:
    """Pull both Thai boards. Returns [] for a board rather than raising,
    so one bad board never kills the whole run."""
    records: list[IPORecord] = []
    fetched_at = datetime.now(timezone.utc)

    for board in boards:
        page = PAGE.format(board=board)
        try:
            session = warm_session(page)
            payload = get_json(session, API, referer=page,
                               params={"type": board, "lang": "en"})
        except Exception as exc:                      # noqa: BLE001
            print(f"[thailand:{board}] FAILED: {exc}")
            continue

        as_of = None
        if payload.get("asOfDate"):
            try:
                as_of = datetime.fromisoformat(payload["asOfDate"])
            except (TypeError, ValueError):
                pass

        rows = payload.get("data") or []
        source_url = f"{API}?type={board}&lang=en"
        for row in rows:
            try:
                records.append(_to_record(row, board_param=board,
                                          source_url=source_url,
                                          as_of=as_of, fetched_at=fetched_at))
            except Exception as exc:                  # noqa: BLE001
                # One malformed row must not discard the other nineteen.
                print(f"[thailand:{board}] skipped {row.get('symbol')}: {exc}")

        print(f"[thailand:{board}] {len(rows)} rows, asOf={payload.get('asOfDate')}")

    return records


if __name__ == "__main__":
    for rec in fetch():
        print(f"{rec.ticker:>8}  {rec.board:<5}  {rec.listing_status:<10}  "
              f"{rec.sector or '-':<28}  {rec.company_name[:40]}")
