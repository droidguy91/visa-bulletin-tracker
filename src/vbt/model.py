"""Typed model for Visa Bulletin cutoff values and bulletin records.

A cutoff cell in the bulletin is one of three things, and the whole point of
this module is that they stay three distinct things all the way to the JSON:

  * a date   -- "15OCT22"
  * current  -- "C", no backlog, anyone may proceed
  * unavailable -- "U", no visa numbers at all this month

Collapsing "C" or "U" into a sentinel date is how trackers end up showing
confidently wrong numbers, so we never do it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Literal

CutoffStatus = Literal["DATE", "C", "U"]

_MONTHS = {
    m: i
    for i, m in enumerate(
        ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL",
         "AUG", "SEP", "OCT", "NOV", "DEC"],
        start=1,
    )
}

_DATE_RE = re.compile(r"^(\d{1,2})([A-Z]{3})(\d{2})$")

# Two-digit years in the bulletin span the 1990s to the 2030s. Cutoffs are
# always priority dates that have already been filed, so nothing lands near
# the ambiguous end of the window.
_CENTURY_PIVOT = 60


class ParseError(Exception):
    """Bulletin content could not be parsed.

    FR-1.7: this must propagate. A run that cannot parse fails loudly rather
    than writing a guess into the data set.
    """


@dataclass(frozen=True)
class Cutoff:
    """One cell of a preference table."""

    status: CutoffStatus
    raw: str
    value: date | None = None

    @property
    def is_current(self) -> bool:
        return self.status == "C"

    @property
    def is_unavailable(self) -> bool:
        return self.status == "U"

    @classmethod
    def parse(cls, text: str) -> "Cutoff":
        raw = "".join(text.split()).upper()
        if not raw:
            raise ParseError("empty cutoff cell")
        if raw in ("C", "CURRENT"):
            return cls("C", raw)
        if raw in ("U", "UNAVAILABLE"):
            return cls("U", raw)
        m = _DATE_RE.match(raw)
        if not m:
            raise ParseError(f"unrecognised cutoff value: {text!r}")
        day, mon, yy = int(m.group(1)), m.group(2), int(m.group(3))
        if mon not in _MONTHS:
            raise ParseError(f"unrecognised month abbreviation in {text!r}")
        year = 2000 + yy if yy < _CENTURY_PIVOT else 1900 + yy
        try:
            return cls("DATE", raw, date(year, _MONTHS[mon], day))
        except ValueError as exc:
            raise ParseError(f"invalid calendar date {text!r}: {exc}") from exc

    def is_current_for(self, priority_date: date) -> bool:
        """Is a given priority date current under this cutoff?

        A priority date is current when it is strictly earlier than the
        cutoff. Equal dates are not current -- the bulletin's cutoff is
        exclusive.
        """
        if self.status == "C":
            return True
        if self.status == "U":
            return False
        assert self.value is not None
        return priority_date < self.value

    def days_from(self, priority_date: date) -> int | None:
        """Signed distance in days from a priority date to this cutoff.

        Positive means the cutoff has passed the priority date. None when the
        cutoff carries no date, because no honest number exists.
        """
        if self.value is None:
            return None
        return (self.value - priority_date).days

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "raw": self.raw,
            "date": self.value.isoformat() if self.value else None,
        }

    @classmethod
    def from_json(cls, obj: dict[str, Any]) -> "Cutoff":
        d = obj.get("date")
        return cls(
            status=obj["status"],
            raw=obj["raw"],
            value=date.fromisoformat(d) if d else None,
        )


@dataclass
class BulletinRecord:
    """One month's bulletin, reduced to what the tracker stores.

    `month` is the bulletin's own month in YYYY-MM form -- the month the
    bulletin governs, not the month it was published.
    """

    month: str
    url: str
    fiscal_year: int
    final_action: Cutoff
    dates_for_filing: Cutoff
    category: str = "EB-1"
    chargeability: str = "INDIA"
    chart_notice: str | None = None
    notes: list[str] = field(default_factory=list)
    content_sha256: str = ""
    fetched_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    def to_json(self) -> dict[str, Any]:
        return {
            "month": self.month,
            "url": self.url,
            "fiscal_year": self.fiscal_year,
            "category": self.category,
            "chargeability": self.chargeability,
            "final_action": self.final_action.to_json(),
            "dates_for_filing": self.dates_for_filing.to_json(),
            "chart_notice": self.chart_notice,
            "notes": self.notes,
            "content_sha256": self.content_sha256,
            "fetched_at": self.fetched_at,
        }

    @classmethod
    def from_json(cls, obj: dict[str, Any]) -> "BulletinRecord":
        return cls(
            month=obj["month"],
            url=obj["url"],
            fiscal_year=obj["fiscal_year"],
            category=obj.get("category", "EB-1"),
            chargeability=obj.get("chargeability", "INDIA"),
            final_action=Cutoff.from_json(obj["final_action"]),
            dates_for_filing=Cutoff.from_json(obj["dates_for_filing"]),
            chart_notice=obj.get("chart_notice"),
            notes=obj.get("notes", []),
            content_sha256=obj.get("content_sha256", ""),
            fetched_at=obj.get("fetched_at", ""),
        )
