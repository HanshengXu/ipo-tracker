"""Run every adapter and write data/ipos.json -- the file the dashboard reads.

Deliberately trivial: adapters all expose fetch() -> list[IPORecord], so this
is a loop. Enrichment, FX and scoring slot in between collect and write as
they are built.

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
from core.schema import SCHEMA_VERSION

OUT = Path(__file__).resolve().parent / "data" / "ipos.json"

# (name, module, tier). Tier is what the README and dashboard tell the user
# about freshness: "automated" refreshes on every cron run; "semi-automated"
# is a committed browser snapshot the cron can only reprocess.
SOURCES = [
    ("Thailand (SET, mai)", thailand, "automated"),
    ("Singapore (SGX Catalist)", singapore, "automated"),
    ("Malaysia (Bursa)", malaysia, "semi-automated"),
    ("London (LSE AIM)", london, "automated"),
    ("Taiwan (TWSE, TIB)", taiwan, "automated"),
]


def main() -> None:
    started = datetime.now(timezone.utc)
    records, sources = [], []

    for name, module, tier in SOURCES:
        try:
            rows = module.fetch()
        except Exception as exc:                      # noqa: BLE001
            # Adapters already isolate their own failures; this is the last
            # line of defence against a bug in one taking down the rest.
            print(f"[run] {name} crashed: {exc}")
            rows = []
        records.extend(rows)
        sources.append({"name": name, "tier": tier, "rows": len(rows)})

    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": started.isoformat(),
        "sources": sources,
        "records": [r.to_dict() for r in records],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False))

    print(f"[run] wrote {len(records)} records to {OUT.relative_to(Path.cwd())}")
    for s in sources:
        print(f"      {s['rows']:>4}  {s['tier']:<15} {s['name']}")


if __name__ == "__main__":
    main()
