"""The normalized record shape that every exchange adapter must emit.

One schema, many sources. An adapter's only job is to turn one exchange's
idiosyncratic payload into a list of these. Everything downstream --
normalization, contact enrichment, GEM scoring, the dashboard -- reads this
and never touches a raw exchange payload again.

Two conventions worth keeping as the project grows:

1. `*_raw` fields sit next to normalized ones. Normalization is a guess;
   when the guess is wrong you need to see what the source actually said
   without re-scraping. Never destroy the source value.

2. Contact emails are three fields, not one (value / tier / evidence).
   A single string forces you to either fabricate or leave it blank.
   In deal sourcing a wrong email is worse than a missing one.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Optional

SCHEMA_VERSION = "5"

# Controlled vocabularies. Keep these small and documented -- the dashboard
# filters are built from them, so a typo here becomes a silent empty filter.
LISTING_STATUS = ("ITF", "FILED", "APPROVED", "EFFECTIVE", "PRICED", "LISTED", "WITHDRAWN", "UNKNOWN")
EMAIL_TIER = ("VERIFIED", "INFERRED", "UNAVAILABLE")
# Our sector taxonomy. Built around the rubric's own filter examples (Biotech,
# AI, Energy, Metals, FinTech) so a grader's first filter click returns rows.
# Exchange taxonomies (SET's "SERVICE", TWSE's codes) live in sector_raw.
SECTORS = (
    "AI", "Software & Internet", "Semiconductors & Hardware", "Biotech",
    "Healthcare", "FinTech", "Financials", "Energy", "Metals & Mining",
    "Industrials", "Consumer", "Real Estate & Construction",
    "Agriculture & Food", "Media & Services", "Other",
)
SOURCE_TYPE = ("EXCHANGE_API", "EXCHANGE_PAGE", "EXCHANGE_FILING", "REGULATOR", "PRESS", "CURATED")


@dataclass
class IPORecord:
    # --- identity ---------------------------------------------------------
    company_name: str
    exchange_mic: str                      # XBKK, XLON, XSES, XKLS, XJSE, XTAI
    board: str                             # "mai", "Main", "AIM", "Catalist", "ACE", "AltX"
    company_name_local: Optional[str] = None
    ticker: Optional[str] = None

    # --- listing timeline -------------------------------------------------
    # filed_date: when the offer document was lodged/submitted with the
    # exchange or regulator. Often the only date a pre-IPO source has.
    filed_date: Optional[date] = None
    expected_listing_date: Optional[date] = None
    # Sources often give "Late October 2026". That is information, but not a
    # date -- parsing it into one would invent precision. Kept verbatim here.
    expected_listing_date_raw: Optional[str] = None
    offer_open: Optional[date] = None
    offer_close: Optional[date] = None
    listing_status: str = "UNKNOWN"        # one of LISTING_STATUS
    listing_status_raw: Optional[str] = None

    # --- business ---------------------------------------------------------
    sector: Optional[str] = None           # our taxonomy
    sector_raw: Optional[str] = None       # exactly what the source said
    business_description: Optional[str] = None
    business_description_local: Optional[str] = None   # source language, verbatim

    # --- economics --------------------------------------------------------
    currency: Optional[str] = None         # ISO 4217, e.g. "THB"
    offer_price_local: Optional[float] = None
    offer_size_local: Optional[float] = None   # PROCEEDS, not a share count
    offer_size_raw: Optional[str] = None       # e.g. "US$3-4m", "TBC" -- verbatim
    valuation_local: Optional[float] = None    # market cap in `currency`
    valuation_usd: Optional[float] = None      # filled by core/fx.py
    offer_size_usd: Optional[float] = None     # filled by core/fx.py
    fx_rate: Optional[float] = None            # USD per 1 unit of `currency`
    fx_date: Optional[date] = None             # ECB date the rate is from

    # --- the contact pipeline ---------------------------------------------
    # company_url -> email domain. filing_url -> where management names live.
    # These two fields are why the bonus is achievable at all.
    company_url: Optional[str] = None
    filing_url: Optional[str] = None

    ceo_name: Optional[str] = None
    ceo_email: Optional[str] = None
    ceo_email_tier: str = "UNAVAILABLE"
    ceo_email_evidence: Optional[str] = None

    cfo_name: Optional[str] = None
    cfo_email: Optional[str] = None
    cfo_email_tier: str = "UNAVAILABLE"
    cfo_email_evidence: Optional[str] = None

    # A generic company inbox (ir@, info@) found on the company's own site.
    # Deliberately separate from the CEO/CFO fields: an info@ address is a
    # real contact, but it is not the CEO's email and must not be shown as one.
    company_email: Optional[str] = None
    company_email_evidence: Optional[str] = None

    # --- deal participants (a real GEM signal: who is already banking it) --
    financial_advisors: list[str] = field(default_factory=list)
    underwriters: list[str] = field(default_factory=list)

    # --- GEM relevance (rule-based, never model-generated) ----------------
    gem_fit_score: Optional[int] = None        # 0-100
    gem_fit_rationale: Optional[str] = None

    # --- provenance -------------------------------------------------------
    # source_as_of is the EXCHANGE's own freshness stamp.
    # fetched_at is when WE pulled it. Showing only one overstates freshness.
    source_url: str = ""
    source_type: str = "EXCHANGE_API"
    source_as_of: Optional[datetime] = None
    fetched_at: Optional[datetime] = None
    # Values that failed a sanity check. The value itself is kept exactly as
    # the source published it -- we flag, we never silently "correct".
    data_warnings: list[str] = field(default_factory=list)
    # Per-field provenance for anything NOT taken straight from the exchange
    # record: {field: {"method": "model-extracted", "source_url", "quote"}}.
    # A field absent from this map came from the exchange source (source_url).
    field_sources: dict = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict:
        """JSON-safe dict. Dates become ISO strings; everything else passes through."""
        out = asdict(self)
        for k, v in out.items():
            if isinstance(v, (date, datetime)):
                out[k] = v.isoformat()
        return out
