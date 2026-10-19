// Refresh the Bursa Malaysia IPO snapshot (adapters/malaysia.py reads it).
//
// Why this exists: bursamalaysia.com is behind a Cloudflare JS challenge, so
// the cron can't fetch it. A real browser can.
//
// How to run:
//   1. Open https://www.bursamalaysia.com/listing/listing_resources/ipo/ipo_summary in Chrome.
//   2. DevTools -> Console -> paste this whole file -> Enter.
//   3. A file named bursa_ipo_summary.json downloads. Move it to
//      data/sources/bursa_ipo_summary.json, run `python -m adapters.malaysia`
//      to check it parses, then commit.

(async () => {
  const base = 'https://www.bursamalaysia.com/listing/listing_resources/ipo/ipo_summary';
  const year = new Date().getFullYear();
  const pages = [];
  for (const y of [year, year - 1]) {
    let prevFirst = null;
    for (let page = 1; page <= 10; page++) {
      const url = `${base}?year=${y}&page=${page}`;
      const html = await fetch(url, { credentials: 'include' }).then(r => r.text());
      const table = new DOMParser().parseFromString(html, 'text/html').querySelector('table');
      const rows = table ? [...table.rows].slice(2) : [];
      const first = rows[0]?.cells[0]?.innerText.trim() || null;
      // The site serves the last page again past the end, so stop on a repeat.
      if (!rows.length || first === prevFirst) break;
      prevFirst = first;
      pages.push({ url, table_html: table.outerHTML });
      await new Promise(r => setTimeout(r, 800));   // politeness between pages
    }
  }
  const snapshot = {
    captured_at: new Date().toISOString(),
    captured_via: 'chrome console (real browser; Cloudflare blocks plain requests)',
    pages,
  };
  const blob = new Blob([JSON.stringify(snapshot)], { type: 'application/json' });
  const a = Object.assign(document.createElement('a'),
    { href: URL.createObjectURL(blob), download: 'bursa_ipo_summary.json' });
  a.click();
  console.log(`Captured ${pages.length} pages.`);
})();
