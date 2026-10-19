"""Model extraction from source documents -- run LOCALLY, results committed.

    python -m tools.extract            # extract for upcoming rows not yet cached
    python -m tools.extract --dry-run  # show what would be sent, and token counts

The split that matters (see README): the model READS documents; rules JUDGE.
The model is asked for facts that are written down somewhere -- a sector, a
CEO's name, an offer size -- and must return, for each, a verbatim quote from
the source. Code then checks the quote really is in the source. A field whose
quote can't be found is discarded, however plausible it looks. That check is
the guard against the specific way language models fail: fluent, confident
fabrication.

Why local and cached: the cron never needs an API key, and a document is only
ever paid for once. Cache: data/enriched/<key>.json, keyed by record + source.

Key: read from ANTHROPIC_API_KEY in the environment or .env (git-ignored).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

import anthropic
import requests
from pydantic import BaseModel, Field
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.schema import SECTORS  # noqa: E402

logging.getLogger("pypdf").setLevel(logging.ERROR)   # font-encoding chatter

DATA = ROOT / "docs" / "data" / "ipos.json"
CACHE = ROOT / "data" / "enriched"
MODEL = "claude-opus-5-5"
UPCOMING = {"ITF", "FILED", "APPROVED", "EFFECTIVE", "PRICED", "UNKNOWN"}
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

Sector = Literal[SECTORS]  # type: ignore[valid-type]


class Extraction(BaseModel):
    """Every value is paired with the exact source sentence that states it.
    null means 'not stated in the source' -- which is a correct answer."""
    sector: Optional[Sector] = Field(None, description="Best-fit sector from the allowed list")
    sector_quote: Optional[str] = Field(None, description="Verbatim source text describing what the company does")
    business_description: Optional[str] = Field(None, description="1-2 plain sentences on what the company does, from the source only")
    description_quote: Optional[str] = None
    ceo_name: Optional[str] = Field(None, description="Person whose stated title is CEO / Chief Executive / Managing Director as chief executive")
    ceo_quote: Optional[str] = None
    cfo_name: Optional[str] = Field(None, description="Person whose stated title is CFO / Chief Financial Officer / Finance Director")
    cfo_quote: Optional[str] = None
    offer_size_local: Optional[float] = Field(None, description="Gross proceeds of the offering in the offer currency, full units (not millions)")
    offer_size_quote: Optional[str] = None
    valuation_local: Optional[float] = Field(None, description="Market capitalisation at the offer price, as STATED in the source, full units")
    valuation_quote: Optional[str] = None


SYSTEM = f"""You extract facts from IPO documents for an investment analyst.

Rules:
- Use ONLY the source text provided. If a fact is not stated, return null.
  null is a correct answer; a guess is a wrong one.
- For every non-null value, copy the supporting quote EXACTLY from the source:
  same words, same spelling, one sentence or table row, under 300 characters.
  Do not paraphrase, translate, or join fragments from different places.
- Names: only a person whose title is explicitly stated. If two people share
  the title, or the title is ambiguous (e.g. "Executive Director"), return null.
- Numbers: only figures the source states. Never compute one (no shares x price).
- Sector must be one of: {", ".join(SECTORS)}. Pick by what the company mainly
  does. "AI" only if AI is the core product, not a feature.
"""


# --------------------------------------------------------------------------
# Source gathering. Only sources code can fetch without a browser are used
# here; Thai SEC filings and Bursa announcements need a browser capture.
# --------------------------------------------------------------------------

# Offer documents run to 400+ pages (~630k tokens, ~$2.50 each). The facts we
# need sit on a few: cover/summary up front, the management section, the
# offering terms. Long PDFs send the front pages plus any page matching these
# terms. Documented page selection, not silent truncation: if the filter
# misses the page that mattered, the field comes back null, never wrong.
LONG_PDF_PAGES = 60
FRONT_PAGES = 15
RELEVANT = re.compile(r"chief executive|chief financial|managing director|finance director"
                      r"|market capitali[sz]ation|gross proceeds|offer price|invitation price",
                      re.I)


def _pdf_text(url: str) -> str:
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=120)
    r.raise_for_status()
    if "pdf" not in r.headers.get("content-type", "").lower():
        raise RuntimeError(f"{url} is {r.headers.get('content-type')}, not a PDF")
    pages = [p.extract_text() or "" for p in PdfReader(io.BytesIO(r.content)).pages]
    if len(pages) <= LONG_PDF_PAGES:
        return "\n".join(pages)
    keep = [i for i, t in enumerate(pages) if i < FRONT_PAGES or RELEVANT.search(t)]
    print(f"    {url.rsplit('/', 1)[-1]}: {len(pages)} pages, sending {len(keep)}")
    return "\n".join(f"[page {i + 1}]\n{pages[i]}" for i in keep)


def sources_for(rec: dict) -> list[dict]:
    """Primary sources only. A field the model filled on an earlier run (it has
    an entry in field_sources) must never come back as a 'source': the model
    would then be verifying quotes against its own writing -- a real bug in
    the first version of this file, caught when cached rows re-extracted."""
    out = []
    model_filled = set(rec.get("field_sources") or {})
    if rec.get("business_description") and "business_description" not in model_filled:
        out.append({"label": "exchange listing description", "url": rec["source_url"],
                    "text": rec["business_description"]})
    if rec.get("business_description_local"):
        # Original-language text (e.g. Chinese from Taiwan's MOPS). The model
        # writes the English summary; its quote must match the original.
        text = "\n".join(filter(None, [f"Industry: {rec.get('sector_raw')}" if rec.get("sector_raw") else None,
                                       f"Main business: {rec['business_description_local']}"]))
        out.append({"label": "company filing (original language)", "url": rec["source_url"], "text": text})
    url = rec.get("filing_url") or ""
    if url.lower().endswith(".pdf"):
        out.append({"label": "offer document", "url": url, "text": _pdf_text(url)})
    return out


# --------------------------------------------------------------------------
# Verification: the part that makes the model's output trustworthy.
# --------------------------------------------------------------------------

def _norm(s: str) -> str:
    """Compare text the way a reader would: ignore case, whitespace runs, and
    curly-vs-straight quotes, which PDF extraction scrambles."""
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", s).strip().lower()


def _states(quote: str, value: float) -> bool:
    """True if a number in the quote equals `value` at some scale: '18.34
    million' and '18,340,000' both state 18340000. Tolerance 0.5% for
    rounding in prose ('S$88.1 million')."""
    for raw in re.findall(r"\d[\d,]*(?:\.\d+)?", quote):
        n = float(raw.replace(",", ""))
        for scale in (1, 1e3, 1e6, 1e9):
            if n and abs(n * scale - value) <= 0.005 * value:
                return True
    return False


def verify(ext: Extraction, sources: list[dict]) -> tuple[dict, list[str]]:
    """Keep a field only if its quote is found verbatim in a source (and, for
    names, the name is inside that quote). Returns (kept, rejection notes)."""
    corpus = [(_norm(s["text"]), s["url"]) for s in sources]
    kept, rejected = {}, []
    pairs = [("sector", "sector_quote"), ("business_description", "description_quote"),
             ("ceo_name", "ceo_quote"), ("cfo_name", "cfo_quote"),
             ("offer_size_local", "offer_size_quote"), ("valuation_local", "valuation_quote")]
    for field, qfield in pairs:
        value, quote = getattr(ext, field), getattr(ext, qfield)
        if value is None:
            continue
        if not quote:
            rejected.append(f"{field}: no quote given")
            continue
        q = _norm(quote)
        hit = next((url for text, url in corpus if q in text), None)
        if not hit:
            rejected.append(f"{field}: quote not found in source: {quote[:80]!r}")
            continue
        if field in ("ceo_name", "cfo_name"):
            surname = _norm(str(value)).split()[-1]
            if surname not in q:
                rejected.append(f"{field}: {value!r} not in its own quote")
                continue
        if field in ("offer_size_local", "valuation_local") and not _states(quote, float(value)):
            # A real sentence attached to the wrong figure would pass the
            # quote check alone. The quote must state THIS number.
            rejected.append(f"{field}: {value:,.0f} is not stated in its quote")
            continue
        kept[field] = {"value": value, "quote": quote, "source_url": hit}
    return kept, rejected


def _key(rec: dict, sources: list[dict]) -> str:
    h = hashlib.sha1()
    h.update(f"{rec['exchange_mic']}|{rec['company_name']}".encode())
    for s in sources:
        h.update(s["url"].encode())
        h.update(hashlib.sha1(s["text"].encode()).digest())
    return h.hexdigest()[:16]


def record_id(rec: dict) -> str:
    """Stable id shared with core/enrich.py for merging cache onto records."""
    return f"{rec['exchange_mic']}|{rec.get('ticker') or rec['company_name']}"


def _prompt(rec: dict, sources: list[dict]) -> str:
    docs = "\n\n".join(f'<source label="{s["label"]}" url="{s["url"]}">\n{s["text"]}\n</source>'
                       for s in sources)
    return (f"Company: {rec['company_name']} ({rec['exchange_mic']}, board {rec['board']}).\n\n"
            f"{docs}\n\nExtract the fields. Remember: verbatim quotes, null when not stated.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY") and (ROOT / ".env").exists():
        for line in (ROOT / ".env").read_text().splitlines():
            m = re.match(r"\s*ANTHROPIC_API_KEY\s*=\s*(\S+)", line)
            if m:
                os.environ["ANTHROPIC_API_KEY"] = m.group(1).strip("\"'")
    client = anthropic.Anthropic()

    CACHE.mkdir(parents=True, exist_ok=True)
    records = json.loads(DATA.read_text())["records"]
    todo = [r for r in records if r["listing_status"] in UPCOMING]
    done = spent_in = spent_out = 0

    for rec in todo:
        try:
            sources = sources_for(rec)
        except Exception as exc:                      # noqa: BLE001
            print(f"  skip {rec['company_name']}: source fetch failed: {exc}")
            continue
        if not sources:
            continue
        key = _key(rec, sources)
        out = CACHE / f"{key}.json"
        if out.exists():
            continue
        prompt = _prompt(rec, sources)
        if args.dry_run:
            n = client.messages.count_tokens(model=MODEL, system=SYSTEM,
                                             messages=[{"role": "user", "content": prompt}]).input_tokens
            print(f"  would extract {rec['company_name'][:40]:<40} {n:>8,} input tokens")
            continue

        resp = client.messages.parse(
            model=MODEL, max_tokens=4000, system=SYSTEM,
            output_config={"effort": "medium"},
            messages=[{"role": "user", "content": prompt}],
            output_format=Extraction,
        )
        if resp.stop_reason == "refusal" or resp.parsed_output is None:
            print(f"  no output for {rec['company_name']} (stop_reason={resp.stop_reason})")
            continue
        kept, rejected = verify(resp.parsed_output, sources)
        emails = sorted({(e.lower(), s["url"]) for s in sources for e in EMAIL.findall(s["text"])})
        out.write_text(json.dumps({
            "record_id": record_id(rec),
            "company_name": rec["company_name"],
            "model": resp.model,
            "extracted_at": datetime.now(timezone.utc).isoformat(),
            "sources": [{"label": s["label"], "url": s["url"]} for s in sources],
            "fields": kept,
            "rejected": rejected,
            "emails_found": [{"email": e, "source_url": u} for e, u in emails],
        }, indent=1, ensure_ascii=False))
        spent_in += resp.usage.input_tokens
        spent_out += resp.usage.output_tokens
        done += 1
        print(f"  {rec['company_name'][:40]:<40} kept={sorted(kept)} rejected={len(rejected)}")
        if args.limit and done >= args.limit:
            break

    if not args.dry_run:
        cost = spent_in / 1e6 * 4 + spent_out / 1e6 * 20
        print(f"extracted {done} records | tokens in {spent_in:,} out {spent_out:,} | ~${cost:.2f}")


if __name__ == "__main__":
    main()
