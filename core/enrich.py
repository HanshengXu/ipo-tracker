"""Merge cached enrichment onto records, and tier contact emails by rule.

Runs inside the cron with no API key and no network: it only reads
data/enriched/ (written locally by tools/extract.py and tools/contacts.py).

Precedence: a value the exchange published beats a model-extracted one. The
model fills gaps; it never overwrites the primary source.

Email tiers (see README for why a wrong email is worse than none):
  VERIFIED     the person's own address appears on the company's site
               (or in a filing); evidence = that page.
  INFERRED     another named officer's address on the same domain reveals the
               naming pattern (e.g. first.last); evidence = the pattern and
               the address it was learned from.
  UNAVAILABLE  everything else. Never a guess.
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXTRACTED = ROOT / "data" / "enriched"
CONTACTS = EXTRACTED / "contacts"
FILINGS = CONTACTS / "offer_documents"
HUNTER = EXTRACTED / "hunter"

MODEL_FIELDS = ("sector", "business_description", "ceo_name", "cfo_name",
                "offer_size_local", "valuation_local")
# Inboxes that can be the company contact, best first. The rest of GENERIC
# are recognised (so they're never mistaken for a person) but never offered:
# nobody should open a deal conversation at whistleblowing@ or careers@.
GENERIC_PREFERENCE = ("ir", "investor", "investors", "investor.relations", "info",
                      "contact", "enquiry", "enquiries", "hello", "admin", "connect",
                      "sales", "marketing", "media", "press", "support")
GENERIC = set(GENERIC_PREFERENCE) | {"hr", "careers", "jobs", "complain", "feedback",
                                     "whistleblowing", "noreply", "no-reply"}
HONORIFICS = {"mr", "mrs", "ms", "dr", "dato", "datuk", "sri", "tan", "khun", "prof"}


def record_id(rec) -> str:
    """Same key tools/extract.py writes, so cache entries find their record."""
    return f"{rec.exchange_mic}|{rec.ticker or rec.company_name}"


def domain_of(url: str | None) -> str | None:
    if not url:
        return None
    host = re.sub(r"^[a-z]+://", "", url.lower()).split("/")[0]
    return host.removeprefix("www.") or None


def _name_parts(name: str) -> list[str]:
    """'Dato’ Sri Chua Chwee Lee' -> ['chua', 'chwee', 'lee']; parenthesised
    local names and honorifics dropped; accents folded for matching."""
    name = re.sub(r"\(.*?\)", " ", name)
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return [p for p in re.findall(r"[a-z]+", name.lower()) if p not in HONORIFICS]


# Address patterns, as functions of (first, last). Ordered most to least
# specific, so a match on 'first.last' isn't mistaken for plain 'first'.
PATTERNS = {
    "first.last": lambda f, l: f"{f}.{l}",
    "first_last": lambda f, l: f"{f}_{l}",
    "firstlast": lambda f, l: f"{f}{l}",
    "f.last": lambda f, l: f"{f[0]}.{l}",
    "flast": lambda f, l: f"{f[0]}{l}",
    "first.la": lambda f, l: f"{f}.{l[:2]}",
    "first.l": lambda f, l: f"{f}.{l[0]}",
    "first": lambda f, l: f,
}


def _pattern_for(local: str, parts: list[str]) -> str | None:
    if len(parts) < 2:
        return None
    first, last = parts[0], parts[-1]
    for name, render in PATTERNS.items():
        if render(first, last) == local:
            return name
    return None


# ---- which addresses belong to the company (shared by tools/contacts.py and core/hunter.py)
LEGAL = re.compile(r"\b(public company limited|holdings?|group|berhad|bhd|limited|ltd|plc|pcl|inc|corp(oration)?|co)\b\.?", re.I)


def _name_key(company_name: str) -> str:
    """'RICHEST SUPPLY PUBLIC COMPANY LIMITED' -> 'richestsupply'."""
    return re.sub(r"[^a-z0-9]", "", LEGAL.sub(" ", company_name.lower()))


def company_domain(email_domain: str, site_domain: str | None, company_name: str) -> bool:
    """The site's own domain (or a subdomain), or a sibling domain whose first
    label spells the company's name ('richestsupply' for Richest Supply,
    'tek4' for TEK 4 Group), or at least half of it. Labels under four
    characters never match: too many companies share them."""
    if site_domain and (email_domain == site_domain or email_domain.endswith("." + site_domain)):
        return True
    label, key = email_domain.split(".")[0].replace("-", ""), _name_key(company_name)
    return len(label) >= 4 and bool(key) and (key == label or key.startswith(label) and len(label) >= len(key) / 2)


def keep_email(email: str, officers: list[str]) -> bool:
    """Only generic inboxes and named officers' own addresses are kept: other
    staff addresses have no use here, and this repo is public."""
    local = email.split("@")[0]
    return local in GENERIC or any(_pattern_for(local, _name_parts(n)) for n in officers)


def _tier_contacts(rec, contacts: dict | None, filing: dict | None = None, hunter: dict | None = None) -> None:
    # Company-published sources first, so on a tie in inbox preference the
    # offer document or the company's own site is cited over a Hunter find.
    emails = ([{**e, "method": "offer-document"} for e in (filing or {}).get("emails", [])]
              + [{**e, "method": "website"} for e in (contacts or {}).get("emails", [])]
              + [{**e, "method": "hunter"} for e in (hunter or {}).get("emails", [])])
    if not emails:
        return

    generic = [e for e in emails if e["email"].split("@")[0] in GENERIC_PREFERENCE]
    # An inbox the exchange itself published (Taiwan's MOPS) is kept.
    if generic and not rec.company_email:
        rank = {g: i for i, g in enumerate(GENERIC_PREFERENCE)}
        best = min(generic, key=lambda e: rank.get(e["email"].split("@")[0], 99))
        rec.company_email = best["email"]
        rec.company_email_evidence = best["source_url"]
        rec.company_email_method = best["method"]

    people = {"ceo": rec.ceo_name, "cfo": rec.cfo_name}
    personal = [e for e in emails if e["email"].split("@")[0] not in GENERIC]

    # Pass 1 -- VERIFIED: the officer's own address is published.
    learned = None                                    # (pattern, example email, page)
    for role, name in people.items():
        if not name:
            continue
        parts = _name_parts(name)
        for e in personal:
            local = e["email"].split("@")[0]
            # A first-name-only address (jane@) could be any Jane: not
            # evidence enough to call it the CEO's. Needs two name parts.
            if _pattern_for(local, parts) not in (None, "first"):
                setattr(rec, f"{role}_email", e["email"])
                setattr(rec, f"{role}_email_tier", "VERIFIED")
                setattr(rec, f"{role}_email_evidence", e["source_url"])
                learned = (_pattern_for(local, parts), e["email"], e["source_url"])
                break

    # Pass 2 -- INFERRED: apply a pattern learned from one named officer to
    # the other. Only from a confirmed name + a confirmed address on the
    # same domain. Known limit: first/last are taken by position, so a
    # surname-first name (Chua Chwee Lee) learned against a given-name-first
    # colleague would render in the wrong order -- hence INFERRED, not VERIFIED.
    if not learned:
        return
    pattern, example, page = learned
    domain = example.split("@")[1]                    # the domain the pattern was seen on
    for role, name in people.items():
        if not name or getattr(rec, f"{role}_email_tier") == "VERIFIED":
            continue
        parts = _name_parts(name)
        if len(parts) < 2:
            continue
        setattr(rec, f"{role}_email", f"{PATTERNS[pattern](parts[0], parts[-1])}@{domain}")
        setattr(rec, f"{role}_email_tier", "INFERRED")
        setattr(rec, f"{role}_email_evidence", f"pattern {pattern} from {example} ({page})")


def _load_extracted() -> dict[str, dict]:
    out = {}
    for path in EXTRACTED.glob("*.json"):
        entry = json.loads(path.read_text())
        rid = entry.get("record_id")
        # Several cache files can exist for one record (source changed);
        # the newest extraction wins.
        if rid and (rid not in out or entry["extracted_at"] > out[rid]["extracted_at"]):
            out[rid] = entry
    return out


def apply(records) -> dict:
    """Merge enrichment in place. Returns counts for the run log."""
    extracted = _load_extracted()
    contacts = {p.stem: json.loads(p.read_text()) for p in CONTACTS.glob("*.json")}
    hunter = {p.stem: json.loads(p.read_text()) for p in HUNTER.glob("*.json")}
    filings = {}
    for p in FILINGS.glob("*.json"):
        doc = json.loads(p.read_text())
        filings[doc["record_id"]] = doc
    filled = 0
    for rec in records:
        filing = filings.get(record_id(rec))
        if filing and filing.get("company_url") and not rec.company_url:
            # The website as stated in the company's own offer document.
            rec.company_url = filing["company_url"]
            rec.field_sources["company_url"] = {"method": "offer-document",
                                                "source_url": f"{filing['filing_url']}#page={filing['page']}",
                                                "quote": filing["quote"]}
        entry = extracted.get(record_id(rec))
        if entry:
            for fld in MODEL_FIELDS:
                got = entry["fields"].get(fld)
                if got is None or getattr(rec, fld) not in (None, ""):
                    continue                          # exchange value wins
                setattr(rec, fld, got["value"])
                rec.field_sources[fld] = {"method": "model-extracted", "model": entry["model"],
                                          "source_url": got["source_url"], "quote": got["quote"],
                                          "extracted_at": entry["extracted_at"]}
                filled += 1
        domain = domain_of(rec.company_url) or ""
        _tier_contacts(rec, contacts.get(domain), filing, hunter.get(domain))
    return {"fields_filled": filled, "cache_entries": len(extracted), "contact_sites": len(contacts),
            "offer_documents": len(filings), "hunter_domains": len(hunter)}
