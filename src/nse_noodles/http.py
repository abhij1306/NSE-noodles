"""The one polite HTTP client: browser-like session, cookie warm-up, pacing and retries.

NSE's website JSON endpoints (`www.nseindia.com/api/...`) answer only a session that first
loaded the home page (it sets cookies) and sends a browser User-Agent and Referer. The archive
host (`nsearchives.nseindia.com`) needs the headers but no cookies. These endpoints are not a
documented public API: keep the pace slow (one request every 2 seconds by default), never run
two pulls at once, and respect NSE's terms of use.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Literal

import httpx

logger = logging.getLogger(__name__)

HOME = "https://www.nseindia.com"
DEFAULT_INTERVAL = 2.0  # seconds between two request starts
DEFAULT_TRIES = 5
DEFAULT_TIMEOUT = 40.0
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
HEADERS = {
    "Accept": "application/json, text/plain, text/csv, application/zip, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-actions",
}


@dataclass(frozen=True)
class Fetched:
    status: Literal["ok", "not_found", "error"]
    data: bytes = b""


class NseClient:
    """Paced GETs with retries. `get` never raises on HTTP or network errors: it answers
    `not_found` for a 404 (a holiday's missing archive file) and `error` after the last try."""

    def __init__(
        self,
        interval: float = DEFAULT_INTERVAL,
        tries: int = DEFAULT_TRIES,
        timeout: float = DEFAULT_TIMEOUT,
        user_agent: str = USER_AGENT,
        home: str = HOME,
    ) -> None:
        self.interval = interval
        self.tries = tries
        self.timeout = timeout
        self.headers = HEADERS | {"User-Agent": user_agent}
        self.home = home
        self._last = 0.0
        self._client: httpx.Client | None = None
        self._warm = False

    def __enter__(self) -> NseClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
        self._warm = False  # the cookies went with the session

    def _session(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                headers=self.headers, timeout=self.timeout, follow_redirects=True
            )
            self._warm = False
        return self._client

    def _pace(self) -> None:
        wait = self._last + self.interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def warm_up(self) -> None:
        """Load the home page once so the session holds NSE's cookies."""
        self._pace()
        try:
            self._session().get(self.home)
        except httpx.HTTPError as exc:
            logger.warning("warm-up failed: %s", type(exc).__name__)
        self._warm = True

    def reset(self) -> None:
        """A fresh session (new cookies), as a browser reload would get."""
        self.close()

    def get(
        self, url: str, params: dict[str, str] | None = None, *, cookies: bool = False
    ) -> Fetched:
        for attempt in range(self.tries):
            if cookies and not self._warm:
                self.warm_up()
            self._pace()
            got = self._attempt(url, params, attempt)
            if got is not None:
                return got
            if attempt + 1 == self.tries:
                break
            time.sleep(2 + 2 * attempt)
            if attempt == 1:
                self.reset()
        return Fetched("error")

    def _attempt(self, url: str, params: dict[str, str] | None, attempt: int) -> Fetched | None:
        """One request: its answer, or None when it is worth asking again."""
        try:
            response = self._session().get(url, params=params)
        except httpx.HTTPError as exc:
            logger.warning("try %d %s: %s", attempt + 1, url, type(exc).__name__)
            return None
        if response.status_code == 404:
            return Fetched("not_found")
        if response.status_code == 200 and response.content:
            return Fetched("ok", response.content)
        logger.warning("try %d %s: HTTP %d", attempt + 1, url, response.status_code)
        return None
