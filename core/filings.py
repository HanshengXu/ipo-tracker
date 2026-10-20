"""Officers and contacts from regulatory filings -- runs in the daily cron.

Thailand (SEC Thailand, market.sec.or.th). Every equity filing has a
standard section 2.3, "corporate governance structure and key information
about the board, sub-committees, executives...", published as a .docx. It
contains, in a fixed format:
  - tables of directors and executives with their positions, from which the
    CEO (ประธานเจ้าหน้าที่บริหาร; managing director กรรมการผู้จัดการ if there is
    no CEO) and the CFO (ประธานเจ้าหน้าที่ฝ่ายการเงิน, ผู้อำนวยการฝ่ายบัญชีและการเงิน,
    and variants) are read by rule;
  - "head of investor relations and contact information"
    (หัวหน้างานนักลงทุนสัมพันธ์ และข้อมูลเพื่อการติดต่อ): a named person and,
    usually, their email -- the company's designated contact for investors.
Names are kept in Thai script as filed. They are not romanised: Thai names
have no single spelling in Latin letters, so an email guessed from a
romanisation would be a guess built on a guess.

The SEC site is slow (~20s a page) and filings change rarely, so each
filing's parse is cached at data/enriched/filings/thsec/<TransID>.json for
REFRESH_DAYS. Parsing is pure rules: no model, no API key.

Taiwan's equivalent (MOPS spokesperson = CFO) is read in adapters/taiwan.py,
which already fetches the MOPS profile.
"""

from __future__ import annotations

import html
import io
import json
import re
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

from core.enrich import company_domain, domain_of
from core.http import DEFAULT_UA

ROOT = Path(__file__).resolve().parent.parent
THSEC = ROOT / "data" / "enriched" / "filings" / "thsec"
REFRESH_DAYS = 7
FILE_LINK = re.compile(r"https://market\.sec\.or\.th/public/ipos/IPOSGetFile\.aspx\?TransID=\d+&amp;TransFileSeq=\d+")
GOVERNANCE = "โครงสร้างการกำกับดูแล"           # section 2.3's title
IR_HEAD = "หัวหน้างานนักลงทุนสัมพันธ์"
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

# Titles. "Deputy" (รอง) and "assistant" (ผู้ช่วย) versions are not the officer.
# Advisers (ที่ปรึกษา) neither. A CEO "of a line" (ประธานเจ้าหน้าที่บริหาร สายงาน...)
# is a division head -- except the accounting-and-finance line, which is the CFO.
NOT_TOP = re.compile(r"(?:รอง|ผู้ช่วย|ที่ปรึกษา)\s*$")
CEO_TITLE = re.compile(r"ประธานเจ้าหน้าที่บริหาร(?!\s*(?:สายงาน|ฝ่าย))|Chief Executive Officer", re.I)
MD_TITLE = re.compile(r"กรรมการผู้จัดการ|Managing Director", re.I)
CFO_TITLE = re.compile(r"ประธานเจ้าหน้าที่(?:บริหาร\s*)?(?:ฝ่าย|สายงาน)?(?:บัญชี(?:และ)?)?(?:การ)?เงิน"
                       r"|ผู้อำนวยการ(?:ฝ่าย|สายงาน)?(?:บัญชีและ)?การเงิน(?:และบัญชี)?"
                       r"|ผู้จัดการ(?:ฝ่าย|สายงาน)?บัญชีและการเงิน|Chief Financial Officer|\bCFO\b", re.I)
# Thai honorifics and ranks, in the order they can stack ("ผศ.ดร.", "ว่าที่ร้อยตรี").
HONORIFIC = re.compile(r"^(?:\d+\.\s*)?(?:(?:ผศ|รศ|ศ|ดร|พล\.?[ตอร]\.?)\.?\s*)*"
                       r"(?:ว่าที่\s*ร้อย(?:ตรี|โท|เอก)\s*(?:นางสาว|นาย|นาง)?|นางสาว|นาย|นาง)\s*")
FOOTNOTE = re.compile(r"\s*(?:/\d+|\d+/?|\(\d+\))\s*$")
# Thai has no spaces between words, so a name in running text runs into the
# next word ("...โตบารมีกุลเป็นหัวหน้า..."). Cut at the verbs that follow it.
NAME_END = re.compile(r"(?:เป็น|ดำรง|ตำแหน่ง|ทำหน้าที่|และ|โดย|ซึ่ง|ผู้).*$")


def _xml_text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape("".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", fragment)))).strip()


def _docx(content: bytes) -> tuple[list[list[str]], str]:
    """(table rows as cell lists, body text as paragraphs joined by newlines)."""
    xml = zipfile.ZipFile(io.BytesIO(content)).read("word/document.xml").decode()
    rows = [[_xml_text(tc) for tc in re.findall(r"<w:tc>.*?</w:tc>", tr, re.S)]
            for tr in re.findall(r"<w:tr[ >].*?</w:tr>", xml, re.S)]
    paras = [_xml_text(p) for p in re.findall(r"<w:p[ >].*?</w:p>", xml, re.S)]
    return rows, "\n".join(p for p in paras if p)


def _person(cells: list[str]) -> str | None:
    """A row's name: the cells before the title, minus the row number.
    Tables sometimes split a name across cells ("1.", "นายประพันธ์", "อังอติชาติ").
    Only rows that start with an honorific are people -- header rows
    ("คณะกรรมการบริษัท", ...) are titles, not names."""
    joined = " ".join(c for c in cells if c and not re.fullmatch(r"\d+\.?", c))
    if not HONORIFIC.match(joined):
        return None
    name = FOOTNOTE.sub("", HONORIFIC.sub("", joined, count=1)).strip()
    return name or None


def _officer(rows: list[list[str]], title: re.Pattern) -> tuple[str, str] | None:
    """First person whose title cell holds `title` as the top role."""
    for cells in rows:
        if len(cells) < 2:
            continue
        position = cells[-1]
        m = title.search(position)
        if not m or NOT_TOP.search(position[:m.start()]):
            continue
        name = _person(cells[:-1])
        if name:
            return name, f"{name} — {position}"
    return None


def _ir_contact(text: str, domain: str | None, company_name: str) -> dict | None:
    i = text.find(IR_HEAD)
    if i < 0:
        return None
    block = text[i: i + 700]
    person = re.search(r"(?:นางสาว|นาย|นาง)\s*[ก-๙]+(?:\s+[ก-๙]+)?", block[len(IR_HEAD):])
    emails = [e.lower().rstrip(".") for e in EMAIL.findall(block)]
    emails = [e for e in emails if company_domain(e.split("@")[1], domain, company_name)]
    if not person and not emails:
        return None
    name = None
    if person:
        first, _, last = HONORIFIC.sub("", person.group(0)).strip().partition(" ")
        name = f"{first} {NAME_END.sub('', last)}".strip()
    quote_end = max([block.lower().find(e) + len(e) for e in emails] + [len(IR_HEAD) + person.end() if person else 0])
    return {"name": name, "email": emails[0] if emails else None,
            "quote": block[: quote_end + 1].replace("\n", " | ").strip()}


def _governance_url(session: requests.Session, filing_url: str) -> str | None:
    """The newest version of section 2.3 on the filing's page."""
    page = session.get(filing_url, timeout=90).text
    for row in re.split(r'<tr class="rg(?:Alt)?Row"', page)[1:]:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if cells and GOVERNANCE in cells[0]:
            links = FILE_LINK.findall(row)
            if links:
                return html.unescape(links[-1])          # last column = latest filing version
    return None


def thai_sec(rec, session: requests.Session) -> dict:
    gov_url = _governance_url(session, rec.filing_url)
    if not gov_url:
        return {"governance_url": None}
    r = session.get(gov_url, timeout=120)
    r.raise_for_status()
    rows, text = _docx(r.content)
    ceo = _officer(rows, CEO_TITLE) or _officer(rows, MD_TITLE)
    cfo = _officer(rows, CFO_TITLE)
    return {"governance_url": gov_url,
            "ceo": ceo and {"name": ceo[0], "quote": ceo[1]},
            "cfo": cfo and {"name": cfo[0], "quote": cfo[1]},
            "ir": _ir_contact(text, domain_of(rec.company_url), rec.company_name)}


def refresh(records) -> str:
    """Parse due Thai filings into the cache. One failure never stops the rest."""
    THSEC.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update({"User-Agent": DEFAULT_UA})
    stale_before = datetime.now(timezone.utc) - timedelta(days=REFRESH_DAYS)
    parsed = failed = 0
    for rec in records:
        if rec.exchange_mic != "XBKK" or "market.sec.or.th" not in (rec.filing_url or ""):
            continue
        trans = (parse_qs(urlparse(rec.filing_url).query).get("TransID") or [None])[0]
        if not trans:
            continue
        path = THSEC / f"{trans}.json"
        if path.exists() and datetime.fromisoformat(json.loads(path.read_text())["parsed_at"]) > stale_before:
            continue
        try:
            result = thai_sec(rec, session)
        except (requests.RequestException, zipfile.BadZipFile, KeyError) as exc:
            print(f"[filings] {rec.ticker}: {type(exc).__name__}")
            failed += 1
            continue
        path.write_text(json.dumps({"record_id": f"{rec.exchange_mic}|{rec.ticker or rec.company_name}",
                                    "filing_url": rec.filing_url, **result,
                                    "parsed_at": datetime.now(timezone.utc).isoformat()},
                                   indent=1, ensure_ascii=False))
        parsed += 1
        print(f"[filings] {rec.ticker}: CEO={(result.get('ceo') or {}).get('name')} "
              f"CFO={(result.get('cfo') or {}).get('name')} IR={(result.get('ir') or {}).get('email')}")
    return f"Thai SEC: {parsed} parsed, {failed} failed"
