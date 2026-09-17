"""Polite HTTP access to travel.state.gov.

Every constraint in FR-2 of the requirements lives here, because scattering
rate limiting across call sites is how a bug turns into a request storm:

  FR-2.3  conditional requests; a 304 costs no body
  FR-2.4  honest, identifying User-Agent -- no browser spoofing
  FR-2.6  exponential backoff with jitter on 403/429/5xx
  FR-2.7  hard ceiling of 60 requests per day, persisted across runs
  FR-2.9  honour robots.txt if one ever appears (today the root 404s)
"""

from __future__ import annotations

import random
import time
import urllib.error
import urllib.request
import urllib.robotparser
from dataclasses import dataclass
from datetime import date

from .urls import INDEX_URL

USER_AGENT = (
    "visa-bulletin-tracker/0.1 "
    "(personal priority-date tracker; "
    "+https://github.com/droidguy91/visa-bulletin-tracker)"
)

DAILY_REQUEST_CEILING = 60
ROBOTS_URL = "https://travel.state.gov/robots.txt"

_RETRY_STATUSES = {403, 408, 429, 500, 502, 503, 504}


class RateCeilingExceeded(RuntimeError):
    """The daily request ceiling was hit. Never bypass this."""


class FetchError(RuntimeError):
    """A request failed after exhausting backoff."""


@dataclass
class Response:
    status: int
    body: str | None
    etag: str | None = None
    last_modified: str | None = None

    @property
    def not_modified(self) -> bool:
        return self.status == 304


class Fetcher:
    """Rate-limited, conditional, backing-off HTTP client.

    `state` is the mutable dict persisted in data/state.json; the request
    counter lives there so the ceiling survives process restarts and CI runs.
    """

    def __init__(self, state: dict, *, timeout: int = 45, sleep=time.sleep):
        self.state = state
        self.timeout = timeout
        self._sleep = sleep
        self._robots: urllib.robotparser.RobotFileParser | None = None
        self.robots_status: object = None

    # -- rate ceiling ----------------------------------------------------

    def _counter(self) -> dict:
        counts = self.state.setdefault("requests", {})
        today = date.today().isoformat()
        # Keep the ledger small but auditable.
        for key in [k for k in counts if k < today][:-14]:
            counts.pop(key, None)
        counts.setdefault(today, 0)
        return counts

    def requests_today(self) -> int:
        return self._counter()[date.today().isoformat()]

    def _spend(self) -> None:
        counts = self._counter()
        today = date.today().isoformat()
        if counts[today] >= DAILY_REQUEST_CEILING:
            raise RateCeilingExceeded(
                f"daily ceiling of {DAILY_REQUEST_CEILING} requests reached "
                f"({counts[today]} used). Refusing to fetch."
            )
        counts[today] += 1

    # -- robots ----------------------------------------------------------

    def robots_allows(self, url: str) -> bool:
        """FR-2.9. A missing robots.txt (404) means no restrictions.

        RFC 9309 says 401/403 on robots.txt means access denied, so we honour
        that as a full disallow rather than crawling anyway. We record the
        status because a 403 here is ambiguous: it can be a real policy, or a
        bot-protection layer reacting to the IP we happen to be calling from.
        """
        if self._robots is None:
            body = ""
            try:
                req = urllib.request.Request(
                    ROBOTS_URL, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    self.robots_status = resp.status
                    body = resp.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                self.robots_status = exc.code
            except Exception as exc:  # noqa: BLE001
                self.robots_status = f"error: {exc}"

            rp = urllib.robotparser.RobotFileParser()
            rp.parse(body.splitlines())
            if self.robots_status in (401, 403):
                rp.disallow_all = True
            self._robots = rp
        try:
            return self._robots.can_fetch(USER_AGENT, url)
        except Exception:
            return True

    # -- fetching --------------------------------------------------------

    def get(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        max_attempts: int = 4,
    ) -> Response:
        if not self.robots_allows(url):
            raise FetchError(f"robots.txt disallows {url}")

        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Encoding": "identity",
        }
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        last_error: Exception | None = None
        for attempt in range(max_attempts):
            self._spend()
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    return Response(
                        status=resp.status,
                        body=raw.decode("utf-8", "replace"),
                        etag=resp.headers.get("ETag"),
                        last_modified=resp.headers.get("Last-Modified"),
                    )
            except urllib.error.HTTPError as exc:
                if exc.code == 304:
                    return Response(status=304, body=None, etag=etag,
                                    last_modified=last_modified)
                last_error = exc
                if exc.code not in _RETRY_STATUSES:
                    raise FetchError(f"{url} -> HTTP {exc.code}") from exc
            except urllib.error.URLError as exc:
                last_error = exc

            if attempt < max_attempts - 1:
                # FR-2.6: 30s, 2m, 8m, with jitter. Deliberately slow --
                # a blocked run should back off for hours, not hammer.
                delay = 30 * (4 ** attempt)
                self._sleep(delay + random.uniform(0, 0.25 * delay))

        raise FetchError(f"{url} failed after {max_attempts} attempts: {last_error}")

    def get_index(self) -> Response:
        """Fetch the lightweight index page conditionally (FR-2.5)."""
        cache = self.state.setdefault("index_cache", {})
        resp = self.get(
            INDEX_URL,
            etag=cache.get("etag"),
            last_modified=cache.get("last_modified"),
        )
        if not resp.not_modified:
            cache["etag"] = resp.etag
            cache["last_modified"] = resp.last_modified
        return resp
