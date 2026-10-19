# Design brief

**What the user sees.** A single sortable table of upcoming IPOs, ranked by fit
for GEM's committed equity facilities, with summary tiles above it. Clicking a row
opens the full description, the deal terms, the score's arithmetic, and where
every field came from.

**Data sources.** Exchange and regulator endpoints, not data vendors: SET (mai),
SGX Catalodge (Catalist), Bursa (ACE), LSE (AIM) and TWSE (TIB), all growth
boards, plus MOPS for Taiwanese company profiles. Four refresh automatically
every day; Bursa's Cloudflare protection makes it a browser snapshot.

**C-suite contacts.** Names come from exchange profiles and offer documents. A
model reads the documents, but a field is kept only if its quote is found
verbatim in the source. Emails are tiered as verified, inferred or N/A, and are
never guessed, because a wrong address burns the contact.

**Search and filters.** Company/CEO search, filters by exchange, sector and
growth board, saved companies, and sorting by any column, including valuation
and listing date.

**Deployment.** A static page on GitHub Pages, refreshed by a daily GitHub
Actions job.

**Incomplete data.** Shown as N/A, never filled in. If an exchange fails, its
last good rows stay and are marked stale.
