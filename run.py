"""Run every adapter and write docs/data/ipos.json -- the file the dashboard reads.

  ingest (adapters) -> enrich (cached extractions + contact tiers)
                    -> fx (USD at each record's own date) -> score -> write

Order matters: enrichment can supply an offer size, FX converts it, the
score reads the USD figure. Each step after ingest is isolated: if the ECB
download fails, records ship without USD figures rather than not at all.

The `sources` block records, per exchange, how many rows came back and how
the data is obtained. A 0 there is visible in the output instead of silently
shrinking the table -- the dashboard can say "SGX: 0 rows" rather than
letting a dead adapter look like a quiet market.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from adapters import london, malaysia, singapore, taiwan, thailand
from core import enrich, fx, hunter, score
from core.schema import SCHEMA_VERSION

# Published inside docs/ because GitHub Pages serves only that folder --
# a page at docs/index.html cannot fetch ../data/.
OUT = Path(__file__).resolve().parent / "docs" / "data" / "ipos.json"

# (name, module, tier, exchange MIC). Tier is what the README and dashboard
# tell the user about freshness: "automated" refreshes on every cron run;
# "semi-automated" is a committed browser snapshot the cron can only reprocess.
SOURCES = [
    ("Thailand (SET, mai)", thailand, "automated", "XBKK"),
    ("Singapore (SGX Catalist)", singapore, "automated", "XSES"),
    ("Malaysia (Bursa)", malaysia, "semi-automated", "XKLS"),
    ("London (LSE AIM)", london, "automated", "XLON"),
    ("Taiwan (TWSE, TIB)", taiwan, "automated", "XTAI"),
]


def _previous() -> dict:
    try:
        return json.loads(OUT.read_text())
    except (OSError, ValueError):
        return {}


def main() -> None:
    started = datetime.now(timezone.utc)
    previous = _previous()
    records, sources, carried = [], [], []

    for name, module, tier, mic in SOURCES:
        try:
            rows = module.fetch()
        except Exception as exc:                      # noqa: BLE001
            # Adapters already isolate their own failures; this is the last
            # line of defence against a bug in one taking down the rest.
            print(f"[run] {name} crashed: {exc}")
            rows = []
        source = {"name": name, "tier": tier, "rows": len(rows), "status": "fresh",
                  "as_of": started.isoformat()}
        if not rows:
            # Zero rows almost always means the fetch failed, not that the
            # market went quiet overnight. Publishing the empty result would
            # silently delete a whole exchange from the dashboard -- learned
            # when a network drop turned 131 rows into 70. Carry the previous
            # run's rows forward and say so, visibly.
            old = [r for r in previous.get("records", []) if r.get("exchange_mic") == mic]
            old_src = next((s for s in previous.get("sources", []) if s.get("name") == name), {})
            if old:
                carried.extend(old)
                source.update(rows=len(old), status="stale",
                              as_of=old_src.get("as_of") or previous.get("generated_at"))
                print(f"[run] {name}: 0 rows fetched -- kept {len(old)} rows from {source['as_of']}")
        records.extend(rows)
        sources.append(source)

    steps = {}
    try:
        steps["enrich"] = enrich.apply(records)
    except Exception as exc:                          # noqa: BLE001
        steps["enrich"] = f"failed: {exc}"
    try:
        # Needs the websites enrichment just filled in (SGX's come from offer
        # documents) and the GEM score to spend its small budget on the best
        # rows first -- so it scores once here, then enrichment re-applies
        # to pick up anything Hunter found.
        score.score_all(records)
        steps["hunter"] = hunter.refresh(records)
        if "kept" in steps["hunter"] and not steps["hunter"].startswith("0 searched"):
            enrich.apply(records)
    except Exception as exc:                          # noqa: BLE001
        steps["hunter"] = f"failed: {type(exc).__name__}"   # never str(exc): it could carry a request
    try:
        fx.convert(records, fx.Rates.load())
        steps["fx"] = "ECB reference rates, dated per record"
    except Exception as exc:                          # noqa: BLE001
        steps["fx"] = f"failed: {exc}"
    score.score_all(records)                          # pure rules over local data
    for step, result in steps.items():
        print(f"[run] {step}: {result}")

    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": started.isoformat(),
        "sources": sources,
        "steps": steps,
        # Carried-forward rows were enriched, converted and scored on the run
        # that produced them, so they are appended as-is.
        "records": [r.to_dict() for r in records] + carried,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False))

    print(f"[run] wrote {len(payload['records'])} records to {OUT.relative_to(Path.cwd())}")
    for s in sources:
        print(f"      {s['rows']:>4}  {s['status']:<6} {s['tier']:<15} {s['name']}")


if __name__ == "__main__":
    main()
