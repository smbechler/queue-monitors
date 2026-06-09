"""Shared HTTP fetching utilities for queue monitors."""

from __future__ import annotations

import re
import time
from pathlib import Path

import requests

# Standard browser-like User-Agent. Many corporate/CDN sites reject default
# requests UA. This is a low-effort, high-reliability fix.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

DEFAULT_TIMEOUT = 60  # generous; some gov APIs are slow under load
DEFAULT_RETRIES = 3   # retry transient failures (timeouts, connection errors)
RETRY_BACKOFF = 5     # seconds; multiplied by attempt number


# Errors that are worth retrying — transient network/server issues.
# We do NOT retry on HTTP status errors (404, 403, etc.) since those
# won't fix themselves; only on connection-level failures.
_RETRYABLE = (
    requests.exceptions.ConnectionError,
    requests.exceptions.ConnectTimeout,
    requests.exceptions.ReadTimeout,
    requests.exceptions.Timeout,
)


def _with_retries(func, *, what: str):
    """Call `func()`, retrying transient network failures with backoff.

    `func` should perform one HTTP attempt and return its result.
    Raises the last exception if all attempts fail.
    """
    last_exc = None
    for attempt in range(1, DEFAULT_RETRIES + 1):
        try:
            return func()
        except _RETRYABLE as exc:
            last_exc = exc
            if attempt < DEFAULT_RETRIES:
                wait = RETRY_BACKOFF * attempt
                print(
                    f"  [fetch] {what} failed (attempt {attempt}/{DEFAULT_RETRIES}): "
                    f"{type(exc).__name__}. Retrying in {wait}s…"
                )
                time.sleep(wait)
            else:
                print(
                    f"  [fetch] {what} failed after {DEFAULT_RETRIES} attempts: "
                    f"{type(exc).__name__}"
                )
    raise last_exc


def get(url: str, timeout: int = DEFAULT_TIMEOUT) -> requests.Response:
    """GET a URL with a browser-like UA. Retries transient failures."""

    def _attempt() -> requests.Response:
        resp = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp

    return _with_retries(_attempt, what=f"GET {url}")


def download(url: str, dest: Path, timeout: int = DEFAULT_TIMEOUT) -> Path:
    """Download a URL to a local file path. Returns the path."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    resp = get(url, timeout=timeout)
    dest.write_bytes(resp.content)
    return dest


def post_json(
    url: str,
    json_body: dict,
    timeout: int = DEFAULT_TIMEOUT,
    extra_headers: dict | None = None,
) -> dict:
    """POST a JSON body, return parsed JSON response.

    Adds standard browser-like headers; retries transient failures;
    raises on HTTP errors. `extra_headers` is merged in last so callers
    can override.
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)

    def _attempt() -> dict:
        resp = requests.post(
            url, json=json_body, headers=headers, timeout=timeout
        )
        resp.raise_for_status()
        return resp.json()

    return _with_retries(_attempt, what=f"POST {url}")


def find_link(html: str, pattern: str) -> str | None:
    """Find the first href in HTML matching a regex pattern.

    Returns the URL or None. Pattern is matched against the href value,
    not the full anchor tag.
    """
    # Match href="..." or href='...'
    for match in re.finditer(r'''href=["']([^"']+)["']''', html, re.IGNORECASE):
        href = match.group(1)
        if re.search(pattern, href, re.IGNORECASE):
            return href
    return None
