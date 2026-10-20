"""English for every Thai or Chinese string the dashboard shows -- names,
company names, titles, and the filing quotes used as evidence.

    python -m tools.translate             # translate strings not yet cached
    python -m tools.translate --dry-run   # list what would be sent

Writes data/enriched/translations.json: {original: {"en", "kind", "model",
"translated_at"}}. run.py publishes the entries for strings in the current
data, and the dashboard shows the English in parentheses beside the original.

What this is and isn't:
  - Names are ROMANISED, not translated, and labelled so on the page. Thai and
    Chinese names have no single Latin spelling, so the person's own spelling
    may differ; romanised names are never used to build email addresses.
  - Everything else is a machine translation, labelled as one. The original
    stays on screen, and it is what the evidence links point to.
  - Display only. No field the pipeline reasons with (scores, email tiers,
    name matching) reads a translation.

Runs locally like tools/extract.py. The key is read from ANTHROPIC_API_KEY in
the environment or .env, and never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import anthropic
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DATA = ROOT / "docs" / "data" / "ipos.json"
CACHE = ROOT / "data" / "enriched" / "translations.json"
MODEL = "claude-opus-5-5"
BATCH = 60
# Thai, CJK ideographs and CJK punctuation.
FOREIGN = re.compile(r"[฀-๿　-〿㐀-鿿＀-￯]")
LATIN = re.compile(r"[A-Za-z]")

SYSTEM = """You translate short strings from Thai and Chinese (Taiwan) company filings into English
for an investment dashboard. You get a JSON list of items, each with an id, a kind and the text.

kind "name": a person's name. Romanise it the way the person would most likely write it in
English: Thai names in the conventional spelling Thai people use for themselves (e.g. ไพศาล → Paisan),
given name first, surname last, no honorific; Taiwanese names in Taiwanese Wade-Giles style as used
on Taiwanese documents, surname LAST, hyphenated given name (陳泓愷 → Hung-Kai Chen).

kind "company": a company's legal name. Give the English legal name, keeping the legal form
(บริษัท ... จำกัด (มหาชน) → "... Public Company Limited"; 股份有限公司 → "Co., Ltd.").

kind "text": anything else (job titles, business descriptions, quoted filing passages). Translate
faithfully and literally. Keep names inside the text romanised per the rules above, keep email
addresses, numbers and English words exactly as they are, and keep separators such as " — ", " / "
and " | ". Do not summarise, explain or add anything.

Return one entry per id, with the same id. Never omit an item."""


class Item(BaseModel):
    id: int
    english: str


class Batch(BaseModel):
    items: list[Item]


def _strings(records: list[dict]) -> dict[str, str]:
    """{original: kind} for every Thai/Chinese string the dashboard displays.
    Strings that already carry Latin letters next to the foreign script (a
    name like 'Hung-Kai Chen (陳泓愷)') are skipped: the English is there."""
    out: dict[str, str] = {}

    def add(s, kind):
        if isinstance(s, str) and FOREIGN.search(s) and not (kind == "name" and LATIN.search(s)):
            out.setdefault(s.strip(), kind)

    for r in records:
        for f in ("ceo_name", "cfo_name", "ir_contact_name"):
            add(r.get(f), "name")
        # company_name_local is not sent: the page already shows it beside
        # the English legal name the exchange publishes.
        for f in ("underwriters", "financial_advisors"):
            for v in r.get(f) or []:
                add(v, "company")
        for src in (r.get("field_sources") or {}).values():
            add(src.get("quote"), "text")
        for w in r.get("data_warnings") or []:
            add(w, "text")
    return out


def _key_from_env() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY") and (ROOT / ".env").exists():
        for line in (ROOT / ".env").read_text().splitlines():
            m = re.match(r"\s*ANTHROPIC_API_KEY\s*=\s*(\S+)", line)
            if m:
                os.environ["ANTHROPIC_API_KEY"] = m.group(1).strip("\"'")


def _ok(kind: str, english: str) -> bool:
    """A translation must be English: no Thai/Chinese left in it, and for a
    name nothing but letters, spaces, hyphens, apostrophes and dots."""
    english = english.strip()
    if not english or FOREIGN.search(english):
        return False
    # Commas: Taiwan files some companies with several presidents in one field.
    return kind != "name" or bool(re.fullmatch(r"[A-Za-z][A-Za-z .,'\-]*", english))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    wanted = _strings(json.loads(DATA.read_text())["records"])
    todo = [(s, k) for s, k in wanted.items() if s not in cache]
    print(f"{len(wanted)} strings shown, {len(todo)} not yet translated")
    if args.dry_run or not todo:
        for s, k in todo[:20]:
            print(f"  {k:<8} {s[:70]}")
        return

    _key_from_env()
    client = anthropic.Anthropic()
    rejected = 0
    for start in range(0, len(todo), BATCH):
        chunk = todo[start:start + BATCH]
        payload = [{"id": i, "kind": k, "text": s} for i, (s, k) in enumerate(chunk)]
        try:
            resp = client.messages.parse(
                model=MODEL, max_tokens=16000, system=SYSTEM,
                output_config={"effort": "medium"},
                messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                output_format=Batch,
            )
        except anthropic.AuthenticationError:
            sys.exit("ANTHROPIC_API_KEY was rejected (revoked or expired?): nothing translated")
        if resp.stop_reason in ("refusal", "max_tokens") or resp.parsed_output is None:
            print(f"  batch {start // BATCH}: no usable output (stop_reason={resp.stop_reason})")
            continue
        now = datetime.now(timezone.utc).isoformat()
        for item in resp.parsed_output.items:
            if not 0 <= item.id < len(chunk):
                continue
            original, kind = chunk[item.id]
            if not _ok(kind, item.english):
                rejected += 1
                print(f"  rejected {kind}: {original[:40]} -> {item.english[:40]}")
                continue
            cache[original] = {"en": item.english.strip(), "kind": kind, "model": resp.model,
                               "translated_at": now}
        CACHE.write_text(json.dumps(cache, indent=1, ensure_ascii=False, sort_keys=True))
        print(f"  batch {start // BATCH}: {len(resp.parsed_output.items)} returned "
              f"(in {resp.usage.input_tokens:,} / out {resp.usage.output_tokens:,} tokens)")
    print(f"{len(cache)} cached, {rejected} rejected")


if __name__ == "__main__":
    main()
