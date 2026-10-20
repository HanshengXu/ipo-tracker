# Pre-IPO Pipeline

A live dashboard of upcoming IPOs on emerging-market **growth boards**, built as a
deal-sourcing tool for committed equity facilities.

**Live:** https://hanshengxu.github.io/ipo-tracker/ · refreshed every 6 hours by GitHub Actions

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
- **No server.** The dashboard is one static HTML file reading one JSON file. A six-hourly
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

### Contacts: breadth first, every address with its evidence

**Officers from regulatory filings.** Names come from the exchanges and from the
filings themselves, read by rules, inside the refresh job:

| Exchange | Filing | What it gives |
|---|---|---|
| Thailand | SEC Thailand filing, section 2.3 (governance: board and executives), `core/filings.py` | CEO (or managing director), CFO, and the **named head of investor relations with their email** |
| Taiwan | MOPS company profile, `adapters/taiwan.py` | President (CEO); spokesperson when the filed title is CFO (財務長); the CEO's English name when they are the legal representative |
| Singapore | Catalist offer document | CEO / CFO (model-read, quote-verified, `tools/extract.py`); company inbox and website |
| London | LSEG pre-IPO profile | directors with titles |

Thai and Taiwanese names are shown in the script they were filed in, with a
romanisation in parentheses; Thai and Chinese quotes get an English translation
underneath (`tools/translate.py`, cached in `data/enriched/translations.json`).
Both are labelled as Claude's and are display-only: no email is built from a
romanised name, because Thai and Chinese names have no single Latin spelling and
the address would be a guess stacked on a guess. Bursa
prospectuses sit behind Cloudflare and are not read yet.

**Company inbox, for as many companies as possible.** The *Company / IR email*
column shows the address the company published, tagged with where:

| Tag | Source |
|---|---|
| Exchange | filed with the exchange (Taiwan's MOPS company profile) |
| Filing | the company's regulatory filing (Thai SEC section 2.3) |
| Offer doc | the "our email address is…" sentence in an SGX offer document, linked to the page |
| Website | the company's own contact or IR page |
| Hunter | found by [Hunter.io](https://hunter.io) domain search, then re-checked: we fetch the page Hunter cites and keep the address only if it's there |

Under it, the **investor-relations contact** the filing names, with their address:
the person a company designates to answer investors.

An address counts only if its domain is the company's website domain, or a domain
that spells the company's own name (Richest Supply publishes `info@richestsupply.com`
on `enrichlighting.com`). Free-mail, agency and other staff addresses are never used,
and inboxes like `careers@` or `whistleblowing@` are never offered as the contact.

**Officer emails, tiered:**

- **Verified:** the person's own address is published by the company, on its site
  or in a filing. The evidence is that page. (Finnomena's and Supa Rich's CFOs are
  also their IR heads, so their filed addresses are their own.)
- **Inferred:** another named officer's published address reveals the company's
  naming pattern, which is then applied to a confirmed name.
- **Guessed:** not published. Hunter.io's usual pattern for the domain (learned
  from the addresses Hunter has seen there) applied to a confirmed name, then
  checked with Hunter's mailbox verifier. A guess the verifier rejects is dropped;
  one it confirms is labelled "mailbox exists"; one it can't test ("accepts any
  address") says so. Guesses are dashed red on the dashboard and never mixed with
  the tiers above.
- **N/A:** no published address and no pattern to guess from.

In deal sourcing **a wrong email is worse than a missing one**: it burns the
contact and damages sending reputation. That is why a guess is shown only with its
label, its pattern and its mailbox check, so the user decides whether to send.

Hunter runs inside the refresh job (`core/hunter.py`), on upcoming IPOs only,
companies with no email first, then companies with a named officer but no address.
The free plan allows 50 searches and 100 mailbox checks a month: each run uses at
most three searches and five checks, keeps a reserve, and caches results (90 days
for searches). Without the `HUNTER_API_KEY` secret the step is skipped.

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
