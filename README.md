# Visa Bulletin Tracker — P0

Fetcher, parser and data store for tracking **EB-1 India** cutoff dates in the
U.S. Department of State Visa Bulletin against a priority date of
**18 September 2023**.

This is phase P0 of the [project requirements](../requirements.md): the data
layer only. No dashboard, no push notifications yet — those are P1 and P2.
The point of doing this first is that the hard part of this project is having
a correct, complete, trustworthy series; the UI is easy once that exists.

## What it does

- Polls the bulletin index page, detects when a new month is published
- Fetches and parses that month's bulletin
- Extracts the EB-1 / India cell from both charts (Final Action, Dates for Filing)
- Archives the source HTML so the data can be rebuilt without re-fetching
- Rolls everything into `data/series.json` for the dashboard to read

## Data model

A cutoff cell is one of three things and stays three things all the way to
the JSON — a **date**, **C** (current), or **U** (unavailable):

```json
{ "status": "DATE", "raw": "15OCT22", "date": "2022-10-15" }
{ "status": "C",    "raw": "C",       "date": null }
```

Collapsing `C` or `U` into a sentinel date is how a tracker ends up showing
confidently wrong numbers, so nothing here does that. A month-over-month
delta is `null` whenever either endpoint has no real date.

A priority date is current when it is **strictly earlier** than the cutoff.
An equal date is not current.

## Layout

```
config.json              priority date, category, chargeability
src/vbt/
  urls.py                URL building; fiscal-year folder logic
  fetch.py               polite HTTP: conditional, backoff, rate ceiling
  parse.py               HTML -> BulletinRecord
  model.py               Cutoff / BulletinRecord types
  store.py               file-backed store + series roll-up
  cli.py                 backfill / poll / ingest / series / verify / show
data/
  bulletins/YYYY-MM.json one immutable record per bulletin
  raw/YYYY-MM.html.gz    archived source page
  series.json            rolled-up series for the PWA
  state.json             request ledger + ETag cache
tests/                   parser tests + golden values
.github/workflows/       poll (scheduled), backfill (manual), test
```

## Commands

```bash
pip install -r requirements.txt

./run backfill --limit 55 --delay 4   # seed history, resumable
./run poll                            # check for a new bulletin
./run show                            # current status vs your PD
./run verify                          # golden values + gap check
./run series                          # rebuild series.json
./run ingest page.html --month 2026-10  # parse a saved page offline
```

## Being a good citizen

Every constraint from FR-2 lives in `fetch.py` rather than being scattered
across call sites, because that is how a bug turns into a request storm:

| Constraint | Implementation |
|---|---|
| Conditional requests | `If-None-Match` / `If-Modified-Since`; a 304 costs no body |
| Honest identity | descriptive `User-Agent` with a contact URL, no browser spoofing |
| Cheap polling | the small index page on the loop; a bulletin body only when a new link appears |
| Backoff | 30s → 2m → 8m with jitter on 403/429/5xx |
| Hard ceiling | 60 requests/day, persisted in `data/state.json`, enforced before every request |
| robots.txt | honoured if one ever appears (the site root currently 404s) |

**Set the contact URL.** `USER_AGENT` in `src/vbt/fetch.py` contains
`REPLACE_ME` — point it at the real repo before the first live run.

## Backfill

October 2015 is the first bulletin with the two-chart structure, so the
series starts there — about 135 months. The 60-request daily ceiling is
deliberately not bypassable, so a full backfill takes three runs of the
manual **Backfill history** workflow on three separate days. It is resumable:
months already stored are skipped.

## Verification

Two independent layers, because they catch different things:

- `pytest` runs the parser against fixtures in `tests/fixtures/`. These are
  faithful reconstructions of the real markup — same headings, row labels,
  column order and values, including a 2015-era page whose heading wording
  differs ("APPLICATION FINAL ACTION DATES…") and cells wrapped in `<strong>`.
  They are reconstructions, not captures, so they prove the parser's logic
  but not that the live page still looks like this.
- `./run verify` checks stored data against `tests/golden.json` (known real
  values: September 2026 FA `15OCT22` / DFF `01DEC23`; October 2015 both `C`)
  and asserts there are no gaps in the month sequence. This is the check that
  runs against live data in CI, and it is the one that matters.

Once the first live backfill lands, replace the reconstructed fixtures with
real archived pages from `data/raw/` and add more golden values.

## Where this runs, and why it is not GitHub Actions

The first live backfill made **zero requests**. `travel.state.gov` sits
behind Cloudflare, which serves an "Attention Required!" challenge page to
GitHub's runner IPs -- including for `/robots.txt`. The fetcher correctly
reads a 403 on robots.txt as a full disallow (RFC 9309) and refuses to
crawl, so nothing was ever fetched.

Worth being precise about what this is: the site publishes **no robots.txt
at all** (it returns 404 from a residential connection). The 403 is edge bot
protection reacting to *where the request comes from*, not a crawl directive.
Every politeness measure in `fetch.py` is beside the point here -- the
request is refused for who it is, not what it does.

**This is not worked around.** Rotating IPs, spoofing a browser User-Agent
or solving the challenge are all bot-detection evasion. The fetcher moves to
a machine on an ordinary residential connection instead.

## Deployment (Linux, systemd)

On a Linux box that stays on -- a home server, a Raspberry Pi:

```bash
git clone https://github.com/droidguy91/visa-bulletin-tracker.git
cd visa-bulletin-tracker
./deploy/install.sh
```

The installer checks reachability **first** and aborts if this machine is
challenged too, rather than installing something that cannot work. Then it
creates a virtualenv, asks for a push token (stored mode 600 outside the
repo), installs a systemd **user** timer, and runs one poll so you see a
real result immediately.

| | |
|---|---|
| Schedule | `systemctl --user list-timers visa-bulletin.timer` |
| Logs | `journalctl --user -u visa-bulletin.service -n 50` |
| Last run | `cat ~/.local/share/visa-bulletin/repo/data/last_run.json` |
| Run now | `systemctl --user start visa-bulletin.service` |

`Persistent=true` catches up a run missed while the machine was off, and
`RandomizedDelaySec=300` keeps requests off a fixed second. For the timer to
fire while you are not logged in: `sudo loginctl enable-linger $USER`.

macOS has no systemd; the equivalent is a launchd agent, which this repo
does not ship. The GitHub Actions workflows remain in the tree but are
**disabled** -- they are kept for the test suite and for manual dispatch if
the source ever stops challenging datacentre IPs.

## Known constraint

The parser has still never seen a live page: both machines available while
this was built are blocked from `travel.state.gov`, one by Cloudflare and
one by an egress policy. It is written defensively -- anything unexpected
raises `ParseError` rather than writing a guess -- and each page is archived
**before** parsing, so the first run on your own hardware leaves the real
HTML on disk to fix against without spending further requests.

## Next

**P1** — static dashboard reading `series.json`: hero cards, the gap to your
PD, the movement chart with your PD as a reference line.
