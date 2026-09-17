"""On-disk data store.

Everything is plain files in the repo, so the data set is version-controlled
and every correction shows up as a visible commit (NFR-5). There is no
database to run and nothing to pay for (NFR-1).

Layout:
    data/state.json            request ledger + conditional-request cache
    data/bulletins/YYYY-MM.json one immutable record per bulletin
    data/raw/YYYY-MM.html.gz    archived source page (FR-1.3)
    data/series.json            rolled-up series for the dashboard
"""

from __future__ import annotations

import gzip
import json
from datetime import date, datetime, timezone
from pathlib import Path

from .model import BulletinRecord

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
BULLETINS = DATA / "bulletins"
RAW = DATA / "raw"
STATE_PATH = DATA / "state.json"
SERIES_PATH = DATA / "series.json"
CONFIG_PATH = ROOT / "config.json"


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=False) + "\n", "utf-8")
    tmp.replace(path)


def load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text("utf-8"))
    return {
        "priority_date": "2023-09-18",
        "category": "EB-1",
        "chargeability": "INDIA",
    }


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text("utf-8"))
    return {}


def save_state(state: dict) -> None:
    _write_json(STATE_PATH, state)


def bulletin_path(month: str) -> Path:
    return BULLETINS / f"{month}.json"


def has_bulletin(month: str) -> bool:
    return bulletin_path(month).exists()


def save_bulletin(record: BulletinRecord, raw_html: str | None = None) -> None:
    _write_json(bulletin_path(record.month), record.to_json())
    if raw_html is not None:
        RAW.mkdir(parents=True, exist_ok=True)
        with gzip.open(RAW / f"{record.month}.html.gz", "wt", encoding="utf-8") as fh:
            fh.write(raw_html)


def save_raw(month: str, html: str) -> None:
    """Archive the source page BEFORE parsing it.

    If the parser then fails, the page is still on disk and the fix can be
    developed offline against it -- which matters a lot when the machine
    doing the debugging cannot reach the source site at all.
    """
    RAW.mkdir(parents=True, exist_ok=True)
    with gzip.open(RAW / f"{month}.html.gz", "wt", encoding="utf-8") as fh:
        fh.write(html)


def save_run_report(report: dict) -> None:
    """Machine-readable outcome of the last run.

    Committed with the data, so a failure is diagnosable without reading CI
    logs -- and it is what the dashboard's freshness indicator reads.
    """
    report["written_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _write_json(DATA / "last_run.json", report)


def load_bulletin(month: str) -> BulletinRecord:
    return BulletinRecord.from_json(
        json.loads(bulletin_path(month).read_text("utf-8"))
    )


def load_raw(month: str) -> str | None:
    path = RAW / f"{month}.html.gz"
    if not path.exists():
        return None
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return fh.read()


def all_months() -> list[str]:
    if not BULLETINS.exists():
        return []
    return sorted(p.stem for p in BULLETINS.glob("*.json"))


def build_series() -> dict:
    """Roll every stored bulletin into one file the PWA can fetch."""
    config = load_config()
    pd = date.fromisoformat(config["priority_date"])

    points: list[dict] = []
    prev_fa = None
    prev_dff = None

    for month in all_months():
        rec = load_bulletin(month)
        fa, dff = rec.final_action, rec.dates_for_filing

        def delta(current, previous):
            # A month-over-month delta only means something when both
            # endpoints are real dates. C and U produce no number.
            if current.value and previous and previous.value:
                return (current.value - previous.value).days
            return None

        points.append({
            "month": rec.month,
            "fiscal_year": rec.fiscal_year,
            "url": rec.url,
            "final_action": fa.to_json(),
            "dates_for_filing": dff.to_json(),
            "fa_delta_days": delta(fa, prev_fa),
            "dff_delta_days": delta(dff, prev_dff),
            "fa_days_from_pd": fa.days_from(pd),
            "dff_days_from_pd": dff.days_from(pd),
            "fa_current": fa.is_current_for(pd),
            "dff_current": dff.is_current_for(pd),
        })
        prev_fa, prev_dff = fa, dff

    latest = points[-1] if points else None
    if latest:
        # The dashboard shows which chart USCIS is accepting this month; it
        # lives on the bulletin record, so surface it on the latest point.
        last_rec = load_bulletin(latest["month"])
        latest["chart_notice"] = last_rec.chart_notice
        latest["notes"] = last_rec.notes[:3]
    series = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "priority_date": config["priority_date"],
        "category": config["category"],
        "chargeability": config["chargeability"],
        "count": len(points),
        "latest_month": latest["month"] if latest else None,
        "latest": latest,
        "points": points,
    }
    _write_json(SERIES_PATH, series)
    return series
