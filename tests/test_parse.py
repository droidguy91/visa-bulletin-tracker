"""Parser tests.

The fixtures under tests/fixtures/ are faithful reconstructions of the real
bulletin markup -- same headings, same row labels, same column order, same
values -- built to exercise the structural decisions the parser makes:
family tables appearing first, an inline <strong> inside a cell, C and U
values, and the 2015-era heading wording.

They are not a substitute for checking against the live site. That is what
`vbt verify` does in CI, against the golden values in tests/golden.json.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from vbt.model import Cutoff, ParseError
from vbt.parse import parse_bulletin, parse_index
from vbt.urls import bulletin_url, fiscal_year, months_to_backfill

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = json.loads((Path(__file__).parent / "golden.json").read_text("utf-8"))


def _load(month: str):
    html = (FIXTURES / f"{month}.html").read_text("utf-8")
    year, mon = int(month[:4]), int(month[5:7])
    return parse_bulletin(html, month, bulletin_url(year, mon))


# -- cutoff value model --------------------------------------------------


def test_parses_date_value():
    c = Cutoff.parse("15OCT22")
    assert c.status == "DATE"
    assert c.value == date(2022, 10, 15)


def test_current_and_unavailable_are_not_dates():
    assert Cutoff.parse("C").is_current
    assert Cutoff.parse("C").value is None
    assert Cutoff.parse("U").is_unavailable
    assert Cutoff.parse("U").value is None


def test_two_digit_year_pivot():
    assert Cutoff.parse("15JUL94").value == date(1994, 7, 15)
    assert Cutoff.parse("08MAR04").value == date(2004, 3, 8)


def test_whitespace_and_case_tolerated():
    assert Cutoff.parse("  15 oct 22 \n").value == date(2022, 10, 15)


@pytest.mark.parametrize("bad", ["", "15XXX22", "2022-10-15", "TBD", "32JAN22"])
def test_garbage_raises_rather_than_guessing(bad):
    with pytest.raises(ParseError):
        Cutoff.parse(bad)


def test_currency_against_priority_date():
    pd = date(2023, 9, 18)
    assert Cutoff.parse("01DEC23").is_current_for(pd) is True
    assert Cutoff.parse("15OCT22").is_current_for(pd) is False
    assert Cutoff.parse("C").is_current_for(pd) is True
    assert Cutoff.parse("U").is_current_for(pd) is False
    # The cutoff is exclusive: a PD equal to the cutoff is not current.
    assert Cutoff.parse("18SEP23").is_current_for(pd) is False


def test_days_from_is_none_without_a_date():
    assert Cutoff.parse("C").days_from(date(2023, 9, 18)) is None
    assert Cutoff.parse("01DEC23").days_from(date(2023, 9, 18)) == 74


# -- page parsing --------------------------------------------------------


def test_modern_bulletin_eb1_india():
    rec = _load("2026-09")
    assert rec.final_action.raw == "15OCT22"
    assert rec.dates_for_filing.raw == "01DEC23"
    assert rec.fiscal_year == 2026


def test_legacy_2015_bulletin_eb1_india_is_current():
    rec = _load("2015-10")
    assert rec.final_action.is_current
    assert rec.dates_for_filing.is_current
    # October 2015 belongs to fiscal year 2016 -- the URL trap.
    assert rec.fiscal_year == 2016
    assert "/2016/" in rec.url


def test_family_tables_are_not_mistaken_for_employment_tables():
    # F1 India in the 2026-09 fixture is 01SEP16; if the parser grabbed a
    # family table this would come back as that value.
    rec = _load("2026-09")
    assert rec.final_action.raw != "01SEP16"


def test_chart_notice_captured():
    rec = _load("2026-09")
    assert rec.chart_notice is not None
    assert "visabulletininfo" in rec.chart_notice.lower()


def test_narrative_notes_captured():
    rec = _load("2026-09")
    assert any("retrogress" in n.lower() for n in rec.notes)


def test_content_hash_is_stable():
    assert _load("2026-09").content_sha256 == _load("2026-09").content_sha256


def test_empty_document_raises():
    with pytest.raises(ParseError):
        parse_bulletin("", "2026-09", "http://example.invalid")


def test_page_without_employment_tables_raises():
    with pytest.raises(ParseError):
        parse_bulletin("<html><body><p>Coming soon</p></body></html>",
                       "2026-10", "http://example.invalid")


def test_golden_values_match_fixtures():
    for month, expected in GOLDEN.items():
        if not (FIXTURES / f"{month}.html").exists():
            continue
        rec = _load(month)
        assert rec.final_action.raw == expected["final_action"], month
        assert rec.dates_for_filing.raw == expected["dates_for_filing"], month


# -- index page ----------------------------------------------------------


def test_parse_index_extracts_month_keys():
    html = """
    <a href="/content/travel/en/legal/visa-law0/visa-bulletin/2026/visa-bulletin-for-september-2026.html">September 2026</a>
    <a href="/content/travel/en/legal/visa-law0/visa-bulletin/2026/visa-bulletin-for-november-2025.html">November 2025</a>
    <a href="/content/travel/en/legal/visa-law0/visa-bulletin/2016/visa-bulletin-for-october-2015.html">October 2015</a>
    """
    assert parse_index(html) == ["2026-09", "2025-11", "2015-10"]


def test_parse_index_ignores_noise():
    assert parse_index("<a href='/something/else.html'>x</a>") == []


# -- url construction ----------------------------------------------------


def test_fiscal_year_boundary():
    assert fiscal_year(2025, 9) == 2025
    assert fiscal_year(2025, 10) == 2026
    assert fiscal_year(2025, 12) == 2026
    assert fiscal_year(2026, 1) == 2026


def test_bulletin_url_uses_fiscal_year_folder():
    assert bulletin_url(2025, 11).endswith(
        "/2026/visa-bulletin-for-november-2025.html")
    assert bulletin_url(2026, 9).endswith(
        "/2026/visa-bulletin-for-september-2026.html")


def test_backfill_range_starts_at_two_chart_era():
    months = months_to_backfill(date(2026, 9, 17))
    assert months[0] == (2015, 10)
    # The bulletin for month N+1 is published during month N.
    assert months[-1] == (2026, 10)
    assert len(months) == len(set(months))
