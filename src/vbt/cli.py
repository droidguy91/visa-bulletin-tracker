"""Command line entry points: backfill, poll, series, verify, show."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date

from . import store
from .fetch import Fetcher, RateCeilingExceeded, USER_AGENT
from .model import ParseError
from .parse import parse_bulletin, parse_index
from .urls import bulletin_url, months_to_backfill, month_key, parse_month_key


def _emit_github_output(**kwargs) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        for key, value in kwargs.items():
            fh.write(f"{key}={value}\n")


def _fetch_and_store(fetcher: Fetcher, month: str) -> bool:
    """Fetch one bulletin and store it. True if newly stored."""
    year, mon = parse_month_key(month)
    url = bulletin_url(year, mon)
    resp = fetcher.get(url)
    if resp.body is None:
        return False
    # Archive first, parse second. A parse failure then leaves the page on
    # disk so the fix can be built offline without spending more requests.
    store.save_raw(month, resp.body)
    record = parse_bulletin(resp.body, month, url)
    store.save_bulletin(record, raw_html=None)
    print(
        f"  {month}  FA={record.final_action.raw:<8} "
        f"DFF={record.dates_for_filing.raw}"
    )
    return True


# -- commands ------------------------------------------------------------


def cmd_backfill(args) -> int:
    """FR-1.5 / FR-2.8: one-time sequential seeding, resumable, polite."""
    state = store.load_state()
    fetcher = Fetcher(state)

    months = [month_key(y, m) for y, m in months_to_backfill()]
    if args.start:
        months = [m for m in months if m >= args.start]
    pending = [m for m in months if not store.has_bulletin(m)]
    if args.limit:
        pending = pending[: args.limit]

    if not pending:
        print("Nothing to backfill; every month is already stored.")
        return 0

    print(f"Backfilling {len(pending)} bulletin(s), {args.delay}s apart.")
    failures: list[tuple[str, str]] = []
    try:
        for i, month in enumerate(pending):
            try:
                _fetch_and_store(fetcher, month)
            except ParseError as exc:
                # A month that will not parse is recorded and skipped so the
                # run keeps going, but the command still exits non-zero.
                failures.append((month, f"parse: {exc}"))
                print(f"  {month}  PARSE FAILED: {exc}", file=sys.stderr)
            except Exception as exc:  # noqa: BLE001
                failures.append((month, str(exc)))
                print(f"  {month}  FETCH FAILED: {exc}", file=sys.stderr)
            if i < len(pending) - 1:
                time.sleep(args.delay)
    except RateCeilingExceeded as exc:
        print(f"\nStopped: {exc}", file=sys.stderr)
        print("Re-run tomorrow; progress so far is saved.", file=sys.stderr)
    finally:
        store.save_state(state)
        store.build_series()
        store.save_run_report({
            "command": "backfill",
            "attempted": len(pending),
            "succeeded": len(pending) - len(failures),
            "requests_today": fetcher.requests_today(),
            "robots_status": fetcher.robots_status,
            "user_agent": USER_AGENT,
            "failures": [{"month": m, "error": e} for m, e in failures],
        })

    if failures:
        print(f"\n{len(failures)} month(s) failed:", file=sys.stderr)
        for month, why in failures:
            print(f"  {month}: {why}", file=sys.stderr)
        return 1
    print("\nBackfill complete.")
    return 0


def cmd_poll(args) -> int:
    """FR-2.5: index page on the loop, bulletin body only when new."""
    state = store.load_state()
    fetcher = Fetcher(state)
    new_months: list[str] = []
    exit_code = 0

    try:
        resp = fetcher.get_index()
        if resp.not_modified:
            print("Index unchanged (304).")
        else:
            published = parse_index(resp.body or "")
            missing = [m for m in published if not store.has_bulletin(m)]
            # Only months at or after the two-chart era, newest first.
            missing = [m for m in missing if m >= "2015-10"][: args.max_new]
            for month in missing:
                try:
                    if _fetch_and_store(fetcher, month):
                        new_months.append(month)
                except (ParseError, Exception) as exc:  # noqa: BLE001
                    # FR-1.7: never write a guess, and make the failure loud.
                    print(f"FAILED {month}: {exc}", file=sys.stderr)
                    exit_code = 1
    except RateCeilingExceeded as exc:
        print(f"Stopped: {exc}", file=sys.stderr)
        exit_code = 1
    finally:
        state["last_checked_at"] = _now()
        store.save_state(state)
        series = store.build_series()
        store.save_run_report({
            "command": "poll",
            "new_months": new_months,
            "requests_today": fetcher.requests_today(),
            "robots_status": fetcher.robots_status,
            "exit_code": exit_code,
        })

    _emit_github_output(
        new_bulletin="true" if new_months else "false",
        months=",".join(new_months),
        latest=series.get("latest_month") or "",
    )
    print(
        f"Poll done. New: {new_months or 'none'}. "
        f"Requests today: {fetcher.requests_today()}."
    )
    return exit_code


def cmd_ingest(args) -> int:
    """Parse a bulletin page from a local file instead of the network.

    Useful when a run happens somewhere without egress, and for replaying an
    archived page after a parser fix.
    """
    from pathlib import Path

    html = Path(args.path).read_text("utf-8")
    year, mon = parse_month_key(args.month)
    url = bulletin_url(year, mon)
    record = parse_bulletin(html, args.month, url)
    store.save_bulletin(record, raw_html=html)
    store.build_series()
    print(f"{args.month}  FA={record.final_action.raw}  "
          f"DFF={record.dates_for_filing.raw}")
    return 0


def cmd_series(args) -> int:
    series = store.build_series()
    print(f"Wrote series.json: {series['count']} months, "
          f"latest {series['latest_month']}.")
    return 0


def cmd_verify(args) -> int:
    """Check stored data against known-good values and internal consistency."""
    golden_path = store.ROOT / "tests" / "golden.json"
    golden = json.loads(golden_path.read_text("utf-8")) if golden_path.exists() else {}

    problems: list[str] = []
    months = store.all_months()
    if not months:
        print("No bulletins stored; nothing to verify.", file=sys.stderr)
        return 1

    for month, expected in golden.items():
        if not store.has_bulletin(month):
            problems.append(f"{month}: expected in golden set but not stored")
            continue
        rec = store.load_bulletin(month)
        for field in ("final_action", "dates_for_filing"):
            got = getattr(rec, field).raw
            want = expected[field]
            if got != want:
                problems.append(f"{month}.{field}: got {got!r}, want {want!r}")

    # Continuity: no gaps in the month sequence.
    for a, b in zip(months, months[1:]):
        ay, am = parse_month_key(a)
        expected_next = month_key(ay + 1, 1) if am == 12 else month_key(ay, am + 1)
        if b != expected_next:
            problems.append(f"gap in series between {a} and {b}")

    if problems:
        print(f"{len(problems)} problem(s):", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 1

    print(f"Verified {len(months)} months ({months[0]} to {months[-1]}); "
          f"{len(golden)} golden value(s) matched; no gaps.")
    return 0


def cmd_show(args) -> int:
    config = store.load_config()
    pd = date.fromisoformat(config["priority_date"])
    months = store.all_months()
    if not months:
        print("No data yet. Run `backfill` first.", file=sys.stderr)
        return 1
    rec = store.load_bulletin(months[-1])

    def line(label, cutoff):
        if cutoff.is_current:
            return f"{label:<20} CURRENT"
        if cutoff.is_unavailable:
            return f"{label:<20} UNAVAILABLE"
        days = cutoff.days_from(pd)
        state = "current" if cutoff.is_current_for(pd) else "not current"
        return (f"{label:<20} {cutoff.value:%d %b %Y}  "
                f"({days:+d} days vs your PD -- {state})")

    print(f"Bulletin {rec.month}  ({config['category']} {config['chargeability']})")
    print(f"Priority date        {pd:%d %b %Y}")
    print(line("Final Action", rec.final_action))
    print(line("Dates for Filing", rec.dates_for_filing))
    print(f"Source: {rec.url}")
    return 0


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="vbt", description="Visa Bulletin tracker")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("backfill", help="seed history (one-time)")
    p.add_argument("--start", help="first month, YYYY-MM")
    p.add_argument("--limit", type=int, help="max bulletins this run")
    p.add_argument("--delay", type=float, default=3.0, help="seconds between fetches")
    p.set_defaults(func=cmd_backfill)

    p = sub.add_parser("poll", help="check for a new bulletin")
    p.add_argument("--max-new", type=int, default=2)
    p.set_defaults(func=cmd_poll)

    p = sub.add_parser("ingest", help="parse a bulletin from a local file")
    p.add_argument("path")
    p.add_argument("--month", required=True, help="YYYY-MM")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("series", help="rebuild series.json")
    p.set_defaults(func=cmd_series)

    p = sub.add_parser("verify", help="check stored data")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("show", help="print current status")
    p.set_defaults(func=cmd_show)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
