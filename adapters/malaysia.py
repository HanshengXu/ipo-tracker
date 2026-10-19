"""Bursa Malaysia -- Main, ACE and LEAP markets, from a captured snapshot.

Source page: https://www.bursamalaysia.com/listing/listing_resources/ipo/ipo_summary?year=YYYY&page=N
It is a server-rendered HTML table, not an API -- and it sits behind a
Cloudflare JavaScript challenge. Plain `requests` gets 403 "Just a moment";
the warm-session trick that beats Incapsula on SET does not work, because the
challenge needs a real JS engine.

Tier: SEMI-AUTOMATED (decided with Hang, 2026-10-18). A real browser captures
the table HTML into data/sources/bursa_ipo_summary.json (tools/capture_bursa.js),
which is committed. This adapter parses that snapshot. The cron reprocesses it
but cannot refresh it -- so every record's timestamps are the CAPTURE time,
never the run time. Pretending otherwise would overstate freshness.

Why this is acceptable: Bursa adds ~5 IPOs a month, so a refresh every few
days loses little, while a cron that 403s the night before Lucas clicks the
link would cost a lot.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from statistics import median
from html.parser import HTMLParser
from pathlib import Path

from core.schema import IPORecord

SNAPSHOT = Path(__file__).resolve().parent.parent / "data" / "sources" / "bursa_ipo_summary.json"
SITE = "https://www.bursamalaysia.com"
RECENT_DAYS = 365

BOARD_MAP = {"ACE Market": "ACE", "Main Market": "Main", "LEAP Market": "LEAP"}


class _TableRows(HTMLParser):
    """Collect each <tr> of <td> cells as (text, first href). The header rows
    use <th>, so they drop out naturally. Stdlib only: one table doesn't
    justify adding BeautifulSoup to the dependency list."""

    def __init__(self):
        super().__init__()
        self.rows: list[list[tuple[str, str | None]]] = []
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag == "td" and self._row is not None:
            self._cell = [[], None]
        elif tag == "a" and self._cell is not None and self._cell[1] is None:
            self._cell[1] = dict(attrs).get("href")

    def handle_endtag(self, tag):
        if tag == "td" and self._cell is not None:
            self._row.append((" ".join("".join(self._cell[0]).split()), self._cell[1]))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell[0].append(data)


def _date(text: str) -> date | None:
    """'28 Sep 2026' -> date. '-' and blanks -> None."""
    try:
        return datetime.strptime(text.replace("*", "").strip(), "%d %b %Y").date()
    except ValueError:
        return None


def _num(text: str) -> float | None:
    """Prices arrive as '0.19', 'RM0.16' and 'RM 0.25' on the same page."""
    cleaned = text.upper().replace("RM", "").replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _status(offer_open, offer_close, listing, as_of: date) -> tuple[str, str]:
    """Status as of the CAPTURE date, derived from the row's own dates.
    Bursa publishes no status field, so the raw value records the rule used."""
    if listing and listing <= as_of:
        return "LISTED", "listing date passed at capture"
    if offer_close and offer_close < as_of:
        return "PRICED", "offer closed, listing pending (tentative date)"
    if offer_open and offer_open <= as_of:
        return "EFFECTIVE", "offer period open at capture"
    return "EFFECTIVE", "prospectus issued, offer not yet open"


def _to_record(cells, *, page_url: str, captured_at: datetime) -> IPORecord | None:
    if len(cells) < 10:
        return None
    (name, href), *_ = cells
    texts = [c[0] for c in cells]
    offer_open, offer_close = _date(texts[1]), _date(texts[2])
    price = _num(texts[3])
    shares = [_num(t) for t in texts[4:7]]      # public issue, offer for sale, private placement
    listing = _date(texts[9])
    status, status_raw = _status(offer_open, offer_close, listing, captured_at.date())

    # Total offering value = price x every share offered. Includes the offer
    # for sale, whose proceeds go to SELLING SHAREHOLDERS, not the company --
    # same basis as SET's issuedSize, so the two exchanges compare like for like.
    offered = sum(s for s in shares if s)
    offer_size = price * offered if price and offered else None

    return IPORecord(
        company_name=name,
        exchange_mic="XKLS",
        board=BOARD_MAP.get(texts[8], texts[8] or "UNKNOWN"),

        offer_open=offer_open,
        offer_close=offer_close,
        expected_listing_date=listing,
        listing_status=status,
        listing_status_raw=status_raw,

        currency="MYR",
        offer_price_local=price,
        offer_size_local=offer_size,

        # The company link is Bursa's prospectus announcement, which carries
        # the prospectus PDFs -- the management-name source for enrichment.
        filing_url=f"{SITE}{href}" if href and href.startswith("/") else href,

        source_url=page_url,
        source_type="EXCHANGE_PAGE",
        source_as_of=captured_at,
        fetched_at=captured_at,
    )


PRICE_OUTLIER_FACTOR = 10


def _flag_price_outliers(records: list[IPORecord]) -> None:
    """Flag issue prices more than 10x their board's median. Example that
    motivated it: PENTECH HOLDINGS, ACE Market, published at RM20.00 when ACE
    IPOs price at RM0.10-0.50 -- probably a typo for RM0.20, but unverified,
    so the value stays and a warning goes alongside it.

    LEAP is excluded: it is a sophisticated-investor board of private
    placements, so prices don't cluster and a median test misfires (it
    flagged NOESIS EXED at RM1.26 on a plausible RM2.5m raise)."""
    by_board: dict[str, list[float]] = {}
    for r in records:
        if r.offer_price_local and r.board != "LEAP":
            by_board.setdefault(r.board, []).append(r.offer_price_local)
    for r in records:
        prices = by_board.get(r.board) or []
        if r.offer_price_local and len(prices) >= 5:
            mid = median(prices)
            if r.offer_price_local > PRICE_OUTLIER_FACTOR * mid:
                r.data_warnings.append(
                    f"Issue price RM{r.offer_price_local:.2f} is over {PRICE_OUTLIER_FACTOR}x the "
                    f"{r.board} median (RM{mid:.2f}); possible source error, unverified. "
                    f"Offer size derived from it is unreliable.")


def fetch(snapshot: Path = SNAPSHOT, recent_days: int = RECENT_DAYS) -> list[IPORecord]:
    """Parse the committed snapshot. Missing or corrupt snapshot -> []."""
    try:
        data = json.loads(snapshot.read_text())
        captured_at = datetime.fromisoformat(data["captured_at"].replace("Z", "+00:00"))
    except Exception as exc:                          # noqa: BLE001
        print(f"[malaysia] FAILED to read snapshot: {exc}")
        return []

    cutoff = captured_at.date() - timedelta(days=recent_days)
    records: list[IPORecord] = []
    seen: set[str] = set()
    for page in data.get("pages") or []:
        parser = _TableRows()
        parser.feed(page.get("table_html") or "")
        for cells in parser.rows:
            try:
                rec = _to_record(cells, page_url=page["url"], captured_at=captured_at)
            except Exception as exc:                  # noqa: BLE001
                print(f"[malaysia] skipped {cells[:1]}: {exc}")
                continue
            if rec is None or rec.company_name in seen:
                continue
            if rec.expected_listing_date and rec.expected_listing_date < cutoff:
                continue
            seen.add(rec.company_name)
            records.append(rec)

    _flag_price_outliers(records)
    age = (datetime.now(captured_at.tzinfo) - captured_at).days
    print(f"[malaysia] {len(records)} rows from snapshot captured {captured_at:%Y-%m-%d} ({age}d old)")
    return records


if __name__ == "__main__":
    for rec in fetch():
        size = f"{rec.offer_size_local / 1e6:,.1f}m" if rec.offer_size_local else "N/A"
        print(f"{rec.board:<5} {rec.listing_status:<9} {rec.expected_listing_date}  "
              f"RM{rec.offer_price_local or 0:<5} {size:>9}  {rec.company_name[:45]}")
        for w in rec.data_warnings:
            print(f"      WARNING: {w}")
