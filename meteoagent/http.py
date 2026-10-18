"""Shared HTTP session with retry and backoff.

An agent running every 15 minutes for months will hit 502s, timeouts, and
connection resets: those are normal, not errors. Transients are absorbed
here; whatever gets through is a real failure and must be logged.
"""

from __future__ import annotations

import logging

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import __version__

log = logging.getLogger(__name__)

USER_AGENT = f"meteo-agent/{__version__} (+https://github.com/local/meteo-agent)"


class FetchError(Exception):
    """Data fetch failed after retries. The cycle continues with the other sources."""


def make_session(total_retries: int = 3) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=total_retries,
        connect=total_retries,
        read=total_retries,
        backoff_factor=1.5,  # 0s, 1.5s, 3s, 6s
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_maxsize=4)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"})
    return session


def get_json(session: requests.Session, url: str, params: dict, timeout: float) -> dict:
    try:
        response = session.get(url, params=params, timeout=timeout)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        raise FetchError(f"GET {url} failed: {exc}") from exc
    except ValueError as exc:
        raise FetchError(f"GET {url}: response is not valid JSON: {exc}") from exc
