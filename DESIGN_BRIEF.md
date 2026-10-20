# Design brief

**What the user sees.** A single sortable table of upcoming IPOs, ranked by fit
for GEM's committed equity facilities, with summary tiles above it. Clicking a row
opens the full description, the deal terms, the score's arithmetic, and where
every field came from.

**Data sources.** Exchange and regulator endpoints, not data vendors: SET (mai),
SGX Catalodge (Catalist), Bursa (ACE), LSE (AIM) and TWSE (TIB), all growth
boards, plus Thai SEC filings and Taiwan's MOPS profiles. Four refresh
automatically; Bursa's Cloudflare protection makes it a browser snapshot.

**C-suite contacts.** Names come from exchange profiles and regulatory filings,
read by rules or by a model whose quotes must appear verbatim in the source.
Every company gets its published inbox and named IR contact; personal emails are
verified, inferred, or a labelled, mailbox-checked guess.

**Search and filters.** Company/CEO search, filters by exchange, sector and
growth board, saved companies, and sorting by any column, including valuation
and listing date.

**Deployment.** A static page on GitHub Pages, refreshed every six hours by
GitHub Actions.

**Incomplete data.** Shown as N/A, never filled in. If an exchange fails, its
last good rows stay and are marked stale.
