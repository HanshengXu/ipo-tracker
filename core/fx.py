"""Local currency -> USD, at the rate on the date the number refers to.

Source: ECB euro foreign exchange reference rates (free, no key):
  https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.xml
ECB quotes everything against EUR, so USD per unit = (USD/EUR) / (CCY/EUR).

Why historical, not today's rate: a Bursa offer size priced in May is a May
number. Converting it at October's rate quietly changes the deal. Each record
is converted at the rate on (or the last ECB business day before) its own
reference date, and that date is stored in fx_date so the choice is visible.

Coverage gap: ECB does not publish TWD. Taiwan rows keep valuation_usd = None
rather than borrowing a rate from a second, unvetted source.
"""

from __future__ import annotations

import bisect
import xml.etree.ElementTree as ET
from datetime import date, datetime

import requests

ECB_HIST = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.xml"


class Rates:
    def __init__(self, table: dict[str, list[tuple[date, float]]]):
        self._table = table          # ccy -> sorted [(date, units per EUR)]

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
        return cls(table)

    def _on(self, ccy: str, when: date) -> tuple[date, float] | None:
        series = self._table.get(ccy)
        if not series:
            return None
        i = bisect.bisect_right(series, (when, float("inf"))) - 1
        return series[i] if i >= 0 else None

    def usd_per_unit(self, ccy: str, when: date) -> tuple[float, date] | None:
        """USD value of one unit of `ccy` on `when` (or the prior ECB day)."""
        if ccy == "USD":
            return 1.0, when
        usd, local = self._on("USD", when), self._on(ccy, when)
        if not usd or not local:
            return None
        return usd[1] / local[1], min(usd[0], local[0])


def _reference_date(rec) -> date:
    """The date the record's money figures refer to: when the price was set
    if known, else when it was filed, else when we saw it."""
    for d in (rec.offer_close, rec.offer_open, rec.filed_date, rec.expected_listing_date):
        if d:
            return d
    seen = rec.source_as_of or rec.fetched_at
    return seen.date() if isinstance(seen, datetime) else date.today()


def convert(records, rates: Rates) -> None:
    """Fill valuation_usd / offer_size_usd / fx_rate / fx_date in place."""
    for rec in records:
        if not rec.currency or (rec.valuation_local is None and rec.offer_size_local is None):
            continue
        hit = rates.usd_per_unit(rec.currency, _reference_date(rec))
        if not hit:
            continue                       # e.g. TWD: no ECB series -> stays None
        rec.fx_rate, rec.fx_date = hit
        if rec.valuation_local is not None:
            rec.valuation_usd = rec.valuation_local * rec.fx_rate
        if rec.offer_size_local is not None:
            rec.offer_size_usd = rec.offer_size_local * rec.fx_rate
