# Pre-IPO Pipeline

A live dashboard of upcoming IPOs on emerging-market **growth boards**, built as a
deal-sourcing tool for committed equity facilities.

**Live:** https://hanshengxu.github.io/ipo-tracker/ · refreshed daily by GitHub Actions

Every row traces to an exchange or regulator source. Nothing is invented: a field
that couldn't be sourced shows **N/A**, and the expanded row says where each
field that *is* shown came from.

---

## Using it

- **Default view:** upcoming IPOs, sorted by **GEM fit** (highest first).
  Tick *Include recent listings* to add IPOs that listed in the last 12 months.
- **Search:** company, CEO, sector, sponsor or adviser.
- **Filters:** exchange, sector, growth vs main board, has email, saved only.
- **Sort:** click any column header (numbers and dates sort properly; N/A always last).
- **Click a row** for the full description, the deal terms, the GEM-fit arithmetic,
  contacts, and the source and supporting quote for every model-extracted field.
- **★** saves a company in your browser. **Copy** puts an email on the clipboard.
  **Export CSV** downloads exactly the rows on screen, with the provenance columns included.

## Coverage

| Exchange | Board(s) | Source | Tier |
|---|---|---|---|
| Thailand (SET) | mai, SET | SET upcoming-IPO API | automated |
| Singapore (SGX) | Catalist | SGX Catalodge (sponsors' lodged offer documents) + SGX securities list | automated |
| Malaysia (Bursa) | ACE, LEAP, Main | Bursa IPO summary | **semi-automated**: Cloudflare blocks scripts, so a browser snapshot is committed |
| London (LSE) | AIM | LSE upcoming issues + LSEG pre-IPO profiles | automated |
| Taiwan (TWSE) | TIB, Main | TWSE listing applications + MOPS company profiles | automated |

**Left out, and why:**
- **JSE:** a scan of all 1,314 SENS announcements in the 30 days to 19 Oct 2026
  found no equity IPOs, only structured products and notes. SENS is also
  Cloudflare-protected.
- **WSE, DFM, Tadawul, B3, Xetra:** not attempted. Five exchanges done properly
  beat eleven done badly.
- **SGX main board:** SGX publishes no pre-IPO feed for the main board, only an
  archive of completed offers. Catalist is the board that matters here.

## How it works

```
adapters/  one file per exchange; each returns list[IPORecord]
   │
run.py  ── ingest ─► enrich ─► fx ─► score ─► docs/data/ipos.json ─► docs/index.html
              │          │        │       │
              │          │        │       └ core/score.py   rule-based GEM fit
              │          │        └ core/fx.py               ECB rates, dated per record
              │          └ core/enrich.py                    merges cached extractions; email tiers
              └ adapters/*.py                                exchange APIs / pages
```

- **One contract.** `core/schema.py` defines `IPORecord`. Every adapter maps its
  exchange's format onto it, and nothing downstream ever reads raw exchange data.
  Adding an exchange means adding one file.
- **Failures stay local.** A failing exchange or malformed row never takes down the
  run. If an exchange returns nothing, its previous rows are kept and labelled
  *stale* on the dashboard rather than silently vanishing.
- **No server.** The dashboard is one static HTML file reading one JSON file. A daily
  GitHub Actions job (`.github/workflows/refresh.yml`) re-runs the pipeline and commits
  the JSON, and GitHub Pages redeploys. No framework and no build step: for ~130 rows,
  React would add a toolchain and failure modes without adding value.

### The model reads; rules judge

- **The model** (Claude) is used for one job only: reading documents. It extracts
  sector, description, officers and offer terms from filings and offer documents.
  Every value must come with a **verbatim quote**, and code checks that the quote is
  really in the source, that a name appears in its own quote, and that a number is
  actually stated in its quote. Anything that fails is discarded.
  Extraction runs locally and the results are committed, so the cron needs no API key.
- **Rules** make every judgment that has to be defended: the GEM-fit score and the
  email tiers. Given the same input, a rule always gives the same answer and can be
  argued line by line.

### GEM fit (0–100)

| Driver | Points | Why |
|---|---|---|
| Growth board (AIM, Catalist, mai, ACE, LEAP, TIB) | 35 | micro-caps that come back for capital |
| Deal size (<$10m 30 · <$50m 22 · <$150m 12) | 30 | small raise, small cushion |
| Emerging-market exchange (MSCI) | 20 | GEM's geography |
| Capital-hungry sector (AI, software, hardware, biotech, healthcare, fintech, energy, mining) | 15 | pre-profit / capex-heavy |

An unknown input scores **half** its points and is labelled "unknown" in the
breakdown. Zero would punish markets that disclose little; full marks would
reward it.

### Contact emails: breadth first, never guessed

**Company inbox, for as many companies as possible.** Each row's *Company email*
column shows the address the company itself published, tagged with where:

| Tag | Source |
|---|---|
| EXCHANGE | filed with the exchange (Taiwan's MOPS company profile) |
| OFFER DOC | the "our email address is…" sentence in an SGX offer document, linked to the page |
| WEBSITE | the company's own contact or IR page |
| HUNTER | found by [Hunter.io](https://hunter.io) domain search, then re-checked: we fetch the page Hunter cites and keep the address only if it's there |

Hunter runs inside the daily refresh (`core/hunter.py`), on upcoming IPOs only,
companies with no email first. The free plan allows 50 searches a month, so
each run does at most five, keeps five in reserve, and caches results for 90
days. Hunter's pattern-guessed addresses (no source page) are discarded.

An address counts only if its domain is the company's website domain, or a domain
that spells the company's own name (Richest Supply publishes `info@richestsupply.com`
on `enrichlighting.com`). Free-mail, agency and staff addresses are never used, and
inboxes like `careers@` or `whistleblowing@` are never offered as the contact.
Coverage today: **36 of 43 upcoming IPOs**. The other seven are real gaps: three Thai
sites publish no inbox, the two Bursa prospectuses sit behind Cloudflare, and two
Taiwanese applicants have no public MOPS profile yet.

**Named officers, tiered:**

- **VERIFIED:** the person's own address appears on the company's site or in a
  filing. The evidence is that URL.
- **INFERRED:** another named officer's published address reveals the company's
  naming pattern, which is then applied to a confirmed name. The evidence is the
  pattern and the address it was learned from.
- **N/A:** everything else. A generic inbox (`ir@`, `info@`) is shown as the
  *company* contact, never as the CEO's address.

In deal sourcing **a wrong email is worse than a missing one**: it burns the
contact and damages sending reputation. So most personal emails show N/A today.
Small issuers rarely publish them, and a CEO named only in Chinese characters
can't be matched against `amos.liao@`.

### Other details

- **Two timestamps.** `source_as_of` is when the exchange last updated its data;
  `fetched_at` is when we pulled it. Showing only one would overstate freshness.
- **FX at the date of the number.** A May offer size is converted at May's ECB
  rate, not today's. The ECB doesn't publish TWD, so Taiwan uses the Federal
  Reserve's H.10 rate for the same date. The dashboard shows USD; hovering over a
  figure shows the amount as published, the rate and its source.
- **Field names lie; arithmetic doesn't.** SET's `issuedSize` is offer proceeds,
  not a share count. TWSE's "Amount of Capital" is paid-in capital, not valuation.
  Bursa lists one ACE IPO at RM20.00, 80× the board median. That value is
  **flagged, not corrected**.

## Running it

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python run.py                                  # writes docs/data/ipos.json
python -m http.server -d docs 8000             # open http://localhost:8000
```

**Local-only enrichment** (needs `ANTHROPIC_API_KEY` in `.env`, which is git-ignored):

```bash
pip install -r requirements-tools.txt
python -m tools.extract --dry-run && python -m tools.extract   # model extraction, cached
python -m tools.contacts                                         # emails from company sites, cached
python run.py
```

**Refreshing Malaysia:** open Bursa's IPO summary page in Chrome, paste
`tools/capture_bursa.js` into the DevTools console, move the downloaded file to
`data/sources/bursa_ipo_summary.json`, and commit it.

## Beyond new listings

The pipeline (ingest, normalize, enrich, score, surface) doesn't care where the
companies come from. Swapping the adapters for one that reads an existing list of
companies points the same provenance, contact and scoring machinery at a leads
sheet instead of new IPOs.
