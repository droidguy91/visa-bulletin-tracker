"""Parse a Visa Bulletin page into a BulletinRecord.

Design notes
------------
The bulletin has been published in the same broad shape since October 2015,
but the wording around the tables drifts. The October 2015 heading reads
"A. APPLICATION FINAL ACTION DATES FOR EMPLOYMENT-BASED PREFERENCE CASES";
the September 2026 one reads "A. FINAL ACTION DATES FOR EMPLOYMENT-BASED
PREFERENCE CASES". So we never match headings exactly.

Instead we locate tables structurally -- an employment table is one whose
first header cell says "Employment" -- and then classify each as Final Action
or Dates for Filing from the nearest preceding heading text, with table order
as a fallback. Anything ambiguous raises ParseError rather than guessing.
"""

from __future__ import annotations

import hashlib
import re
from typing import Iterable

from bs4 import BeautifulSoup, Tag

from .model import BulletinRecord, Cutoff, ParseError
from .urls import fiscal_year

FINAL_ACTION = "final_action"
DATES_FOR_FILING = "dates_for_filing"

_NOTE_KEYWORDS = (
    "retrogress",
    "spillover",
    "fall across",
    "annual limit",
    "employment first",
    "employment-based first",
    "eb-1",
    "unavailable",
    "visa availability",
    "oversubscribed",
)


def _norm(text: str) -> str:
    """Collapse whitespace (including non-breaking) and casefold."""
    return " ".join(text.replace("\xa0", " ").split()).strip().lower()


def _squash(text: str) -> str:
    """Normalised form with every non-alphanumeric character removed."""
    return re.sub(r"[^a-z0-9]", "", _norm(text))


def _cells(row: Tag) -> list[Tag]:
    return row.find_all(["td", "th"], recursive=False)


def _rows(table: Tag) -> list[Tag]:
    return table.find_all("tr")


def _is_employment_table(table: Tag) -> bool:
    rows = _rows(table)
    if not rows:
        return False
    first = _cells(rows[0])
    if not first:
        return False
    return "employment" in _norm(first[0].get_text())


def _classify(table: Tag) -> str | None:
    """Final Action or Dates for Filing, from the nearest preceding text."""
    scanned = 0
    for node in table.find_all_previous(string=True):
        text = _norm(str(node)).upper()
        if not text:
            continue
        scanned += 1
        if scanned > 400:
            break
        if "FINAL ACTION" in text:
            return FINAL_ACTION
        if "DATES FOR FILING" in text:
            return DATES_FOR_FILING
    return None


def _india_column(header_row: Tag) -> int:
    cells = _cells(header_row)
    for idx, cell in enumerate(cells):
        if "india" in _norm(cell.get_text()):
            return idx
    raise ParseError(
        "no INDIA column in employment table; headers were: "
        + ", ".join(repr(_norm(c.get_text())) for c in cells)
    )


def _eb1_cell(table: Tag, col: int) -> str:
    for row in _rows(table)[1:]:
        cells = _cells(row)
        if not cells:
            continue
        if _squash(cells[0].get_text()) == "1st":
            if col >= len(cells):
                raise ParseError(
                    f"EB-1 row has {len(cells)} cells, need column {col}"
                )
            return cells[col].get_text()
    raise ParseError("no '1st' row found in employment table")


def _employment_tables(soup: BeautifulSoup) -> dict[str, Tag]:
    tables = [t for t in soup.find_all("table") if _is_employment_table(t)]
    if len(tables) < 2:
        raise ParseError(
            f"expected at least 2 employment tables, found {len(tables)}"
        )

    classified: dict[str, Tag] = {}
    kinds = [_classify(t) for t in tables]

    # Trust the headings only if they disagree with each other in the way we
    # expect; otherwise fall back to document order, which has been stable
    # since 2015 (Final Action first, Dates for Filing second).
    if kinds[0] == FINAL_ACTION and DATES_FOR_FILING in kinds[1:]:
        classified[FINAL_ACTION] = tables[0]
        classified[DATES_FOR_FILING] = tables[kinds.index(DATES_FOR_FILING, 1)]
    else:
        classified[FINAL_ACTION] = tables[0]
        classified[DATES_FOR_FILING] = tables[1]
    return classified


def _paragraph_texts(soup: BeautifulSoup) -> Iterable[str]:
    for tag in soup.find_all(["p", "li"]):
        text = " ".join(tag.get_text().replace("\xa0", " ").split())
        if text:
            yield text


def _chart_notice(paragraphs: list[str]) -> str | None:
    for text in paragraphs:
        lowered = text.lower()
        if "visabulletininfo" in lowered or "in lieu of the chart" in lowered:
            return text
    return None


def _notes(paragraphs: list[str]) -> list[str]:
    """FR-1.8: keep the narrative that explains a month's movement."""
    out: list[str] = []
    for text in paragraphs:
        lowered = text.lower()
        if len(text) < 40:
            continue
        if any(k in lowered for k in _NOTE_KEYWORDS):
            out.append(text)
    # Deduplicate while preserving order, and keep the record a sane size.
    seen: set[str] = set()
    deduped = []
    for t in out:
        if t not in seen:
            seen.add(t)
            deduped.append(t)
    return deduped[:25]


def parse_bulletin(html: str, month: str, url: str) -> BulletinRecord:
    """Parse one bulletin page. Raises ParseError on anything unexpected."""
    if not html or not html.strip():
        raise ParseError("empty document")

    soup = BeautifulSoup(html, "lxml")
    tables = _employment_tables(soup)

    fa_table = tables[FINAL_ACTION]
    dff_table = tables[DATES_FOR_FILING]

    fa = Cutoff.parse(_eb1_cell(fa_table, _india_column(_rows(fa_table)[0])))
    dff = Cutoff.parse(_eb1_cell(dff_table, _india_column(_rows(dff_table)[0])))

    paragraphs = list(_paragraph_texts(soup))
    year, mon = int(month[:4]), int(month[5:7])

    return BulletinRecord(
        month=month,
        url=url,
        fiscal_year=fiscal_year(year, mon),
        final_action=fa,
        dates_for_filing=dff,
        chart_notice=_chart_notice(paragraphs),
        notes=_notes(paragraphs),
        content_sha256=hashlib.sha256(html.encode("utf-8", "replace")).hexdigest(),
    )


_LINK_RE = re.compile(
    r"visa-bulletin/(\d{4})/visa-bulletin-for-([a-z]+)-(\d{4})\.html",
    re.IGNORECASE,
)

_MONTH_INDEX = {
    name: i
    for i, name in enumerate(
        ["january", "february", "march", "april", "may", "june", "july",
         "august", "september", "october", "november", "december"],
        start=1,
    )
}


def parse_index(html: str) -> list[str]:
    """Month keys (YYYY-MM) for every bulletin linked from the index page.

    Returned newest first. Used to detect publication without downloading
    any bulletin body (FR-2.5).
    """
    found: set[str] = set()
    for _fy, month_name, year in _LINK_RE.findall(html or ""):
        idx = _MONTH_INDEX.get(month_name.lower())
        if idx:
            found.add(f"{int(year):04d}-{idx:02d}")
    return sorted(found, reverse=True)
