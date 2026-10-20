"""Local currency -> USD, at the rate on the date the number refers to.

Source: ECB euro foreign exchange reference rates (free, no key):
  https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.xml
ECB quotes everything against EUR, so USD per unit = (USD/EUR) / (CCY/EUR).

Why historical, not today's rate: a Bursa offer size priced in May is a May
number. Converting it at October's rate quietly changes the deal. Each record
is converted at the rate on (or the last ECB business day before) its own
reference date, and that date is stored in fx_date so the choice is visible.

Coverage gap: ECB does not publish TWD. For currencies ECB lacks, the
Federal Reserve's H.10 noon buying rates fill in (official, free, served as
CSV by FRED with no key). Every record says which source its rate came from.
If FRED is unreachable, those rows keep USD = None rather than a guess.
"""

from __future__ import annotations

import bisect
import xml.etree.ElementTree as ET
from datetime import date, datetime

import requests

ECB_HIST = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.xml"
# FRED series id per currency, quoted as units of the currency per USD.
FRED_SERIES = {"TWD": "DEXTAUS"}
# Two years back covers the 365-day window; the full history (1983-) is slow.
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={}&cosd={}"


class Rates:
    def __init__(self, table: dict[str, list[tuple[date, float]]],
                 fed: dict[str, list[tuple[date, float]]] | None = None):
        self._table = table          # ccy -> sorted [(date, units per EUR)]
        self._fed = fed or {}        # ccy -> sorted [(date, units per USD)]

    @classmethod
    def load(cls, timeout: int = 60) -> "Rates":
        root = ET.fromstring(requests.get(ECB_HIST, timeout=timeout).content)
        table: dict[str, list[tuple[date, float]]] = {"EUR": []}
        # <Cube time="2026-10-16"><Cube currency="USD" rate="1.1253"/>...</Cube>
        # Match on attributes, not namespaces or quote style.
        for day in root.iter():
            if not day.get("time"):
                continue
            d = date.fromisoformat(day.get("time"))
            table["EUR"].append((d, 1.0))
            for cell in day:
                if cell.get("currency") and cell.get("rate"):
                    table.setdefault(cell.get("currency"), []).append((d, float(cell.get("rate"))))
        for series in table.values():
            series.sort()
        if not table.get("USD"):
            raise RuntimeError("ECB history parsed with no USD series")
        return cls(table, _load_fed(timeout))

    def _on(self, ccy: str, when: date, table=None) -> tuple[date, float] | None:
        series = (self._table if table is None else table).get(ccy)
        if not series:
            return None
        i = bisect.bisect_right(series, (when, float("inf"))) - 1
        return series[i] if i >= 0 else None

    def usd_per_unit(self, ccy: str, when: date) -> tuple[float, date, str] | None:
        """USD value of one unit of `ccy` on `when` (or the prior business
        day), with the source's name."""
        if ccy == "USD":
            return 1.0, when, "none needed"
        usd, local = self._on("USD", when), self._on(ccy, when)
        if usd and local:
            return usd[1] / local[1], min(usd[0], local[0]), "ECB"
        fed = self._on(ccy, when, self._fed)
        if fed:
            return 1 / fed[1], fed[0], "Federal Reserve H.10"
        return None


def _load_fed(timeout: int) -> dict[str, list[tuple[date, float]]]:
    """H.10 series for currencies ECB lacks. Failure here only costs those
    currencies their USD figures; it never fails the ECB conversion."""
    out = {}
    for ccy, series_id in FRED_SERIES.items():
        try:
            # An honest user agent: FRED stalls requests claiming to be a
            # browser ("Mozilla/5.0" timed out; this answers in 0.1s).
            since = date.today().replace(year=date.today().year - 2)
            r = requests.get(FRED_CSV.format(series_id, since), timeout=timeout,
                             headers={"User-Agent": "ipo-tracker/1.0 (+https://github.com/HanshengXu/ipo-tracker)"})
            r.raise_for_status()
            rows = []
            for line in r.text.splitlines()[1:]:
                d, _, v = line.partition(",")
                if v and v != ".":                    # "." = no fixing that day
                    rows.append((date.fromisoformat(d), float(v)))
            if rows:
                out[ccy] = sorted(rows)
        except (requests.RequestException, ValueError) as exc:
            print(f"[fx] {series_id} unavailable: {exc}")
    return out


def _reference_date(rec) -> date:
    """The date the record's money figures refer to: when the price was set
    if known, else when it was filed, else when we saw it."""
    for d in (rec.offer_close, rec.offer_open, rec.filed_date, rec.expected_listing_date):
        if d:
            return d
    seen = rec.source_as_of or rec.fetched_at
    return seen.date() if isinstance(seen, datetime) else date.today()


def convert(records, rates: Rates) -> None:
    """Fill the *_usd fields, fx_rate, fx_date and fx_source in place."""
    for rec in records:
        amounts = (rec.valuation_local, rec.offer_size_local, rec.offer_price_local)
        if not rec.currency or all(a is None for a in amounts):
            continue
        hit = rates.usd_per_unit(rec.currency, _reference_date(rec))
        if not hit:
            continue                       # no rate from either source -> stays None
        rec.fx_rate, rec.fx_date, rec.fx_source = hit
        if rec.valuation_local is not None:
            rec.valuation_usd = rec.valuation_local * rec.fx_rate
        if rec.offer_size_local is not None:
            rec.offer_size_usd = rec.offer_size_local * rec.fx_rate
        if rec.offer_price_local is not None:
            rec.offer_price_usd = rec.offer_price_local * rec.fx_rate
