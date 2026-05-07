"""Shared HTTP fetching utilities for queue monitors."""

from __future__ import annotations

import re
from pathlib import Path

import requests

# Standard browser-like User-Agent. Many corporate/CDN sites reject default
# requests UA. This is a low-effort, high-reliability fix.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

DEFAULT_TIMEOUT = 30


def get(url: str, timeout: int = DEFAULT_TIMEOUT) -> requests.Response:
    """GET a URL with a browser-like UA. Raises on HTTP errors."""
    resp = requests.get(
        url,
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp


def download(url: str, dest: Path, timeout: int = DEFAULT_TIMEOUT) -> Path:
    """Download a URL to a local file path. Returns the path."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    resp = get(url, timeout=timeout)
    dest.write_bytes(resp.content)
    return dest


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
