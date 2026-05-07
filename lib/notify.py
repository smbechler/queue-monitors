"""Email notification via Resend.

Designed to be swappable: if you switch providers later, replace this
module's `send_email` implementation. Callers should not import requests
or know about Resend.
"""

from __future__ import annotations

import os

import requests

RESEND_API_URL = "https://api.resend.com/emails"
DEFAULT_FROM = "ERAS Monitor <onboarding@resend.dev>"


def send_email(
    to: str | list[str],
    subject: str,
    html: str,
    *,
    text: str | None = None,
    from_addr: str | None = None,
    api_key: str | None = None,
) -> dict:
    """Send an email via Resend.

    Reads RESEND_API_KEY from env if api_key not provided.
    Reads MONITOR_FROM_ADDR from env if from_addr not provided.
    Returns the parsed JSON response.
    """
    key = api_key or os.environ.get("RESEND_API_KEY")
    if not key:
        raise RuntimeError("RESEND_API_KEY not set")

    sender = from_addr or os.environ.get("MONITOR_FROM_ADDR") or DEFAULT_FROM

    recipients = [to] if isinstance(to, str) else list(to)

    payload = {
        "from": sender,
        "to": recipients,
        "subject": subject,
        "html": html,
    }
    if text:
        payload["text"] = text

    resp = requests.post(
        RESEND_API_URL,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def send_failure_alert(to: str, monitor_name: str, error: str) -> None:
    """Send a 'monitor broken' alert. Best-effort: swallows its own errors."""
    try:
        send_email(
            to=to,
            subject=f"[Monitor FAILED] {monitor_name}",
            html=(
                f"<h2>Monitor failed: {monitor_name}</h2>"
                f"<pre style='background:#f4f4f4;padding:12px;border-radius:4px;"
                f"white-space:pre-wrap;font-family:monospace;font-size:12px;'>"
                f"{_escape(error)}</pre>"
                f"<p>Check the GitHub Actions logs for the full traceback.</p>"
            ),
            text=f"Monitor failed: {monitor_name}\n\n{error}",
        )
    except Exception as exc:  # noqa: BLE001 - we really do want to swallow
        print(f"Failed to send failure alert: {exc}")


def _escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
