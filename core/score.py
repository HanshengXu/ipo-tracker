"""GEM fit score: 0-100, rule-based, never model-generated.

GEM provides committed equity facilities to small-cap and pre-IPO issuers in
emerging and frontier markets. The score asks one question: how much does
this issuer look like a company that will need follow-on equity capital soon,
in a market GEM works in?

Four drivers, each worth a fixed number of points, every point explained in
gem_fit_rationale so Lucas can disagree with a specific line:

  Growth board               35   micro-caps on AIM/Catalist/mai/ACE/LEAP/TIB
                                  are the ones that come back for capital
  Small deal size            30   a small raise is a small cushion
  Emerging-market exchange   20   GEM's geography (MSCI classification)
  Capital-hungry sector      15   pre-profit, capex-heavy or R&D-heavy themes

Missing data scores HALF the driver's points, labelled "unknown". Zero would
punish thin disclosure (Taiwan publishes no deal sizes); full marks would
reward it. Half is the neutral choice, and the label keeps it visible.

Why rules and not a model: a model's score can't be defended line by line in
an interview, and it can change between runs on the same input. These can.
"""

from __future__ import annotations

GROWTH_BOARDS = {"mai", "Catalist", "ACE", "LEAP", "AIM", "TIB"}

# MSCI market classification of the exchange's home market.
EMERGING = {"XBKK": "Thailand", "XKLS": "Malaysia", "XTAI": "Taiwan"}
DEVELOPED = {"XSES": "Singapore", "XLON": "United Kingdom"}

CAPITAL_HUNGRY = {"AI", "Software & Internet", "Semiconductors & Hardware",
                  "Biotech", "Healthcare", "FinTech", "Energy", "Metals & Mining"}

# (upper bound in USD, points, label). Bands are deliberately coarse: the
# ranking matters, not whether $11m beats $9m by a point.
SIZE_BANDS = [
    (10e6, 30, "under $10m"),
    (50e6, 22, "$10-50m"),
    (150e6, 12, "$50-150m"),
    (float("inf"), 0, "over $150m"),
]


def _size_points(rec) -> tuple[int, str]:
    size = rec.offer_size_usd if rec.offer_size_usd is not None else rec.valuation_usd
    basis = "raise" if rec.offer_size_usd is not None else "market cap"
    if size is None or any("unreliable" in w for w in rec.data_warnings):
        return 15, "deal size unknown (+15 of 30, neutral)"
    for bound, pts, label in SIZE_BANDS:
        if size < bound:
            return pts, f"{basis} {label} (~${size / 1e6:,.1f}m) (+{pts})"
    return 0, "deal size not scored"


def score(rec) -> None:
    """Set gem_fit_score and gem_fit_rationale in place."""
    parts: list[tuple[int, str]] = []

    if rec.board in GROWTH_BOARDS:
        parts.append((35, f"growth board: {rec.board} (+35)"))
    else:
        parts.append((0, f"main board: {rec.board} (+0)"))

    parts.append(_size_points(rec))

    if rec.exchange_mic in EMERGING:
        parts.append((20, f"emerging market: {EMERGING[rec.exchange_mic]} (+20)"))
    elif rec.exchange_mic in DEVELOPED:
        parts.append((0, f"developed market: {DEVELOPED[rec.exchange_mic]} (+0)"))
    else:
        parts.append((10, "market classification unknown (+10 of 20, neutral)"))

    if rec.sector is None:
        parts.append((7, "sector unknown (+7 of 15, neutral)"))
    elif rec.sector in CAPITAL_HUNGRY:
        parts.append((15, f"capital-hungry sector: {rec.sector} (+15)"))
    else:
        parts.append((0, f"sector: {rec.sector} (+0)"))

    rec.gem_fit_score = sum(p for p, _ in parts)
    rec.gem_fit_rationale = "; ".join(label for _, label in parts)


def score_all(records) -> None:
    for rec in records:
        score(rec)
