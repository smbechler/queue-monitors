"""SPP DPNS (Delivery Point Network Study) posting monitor.

Watches the SPP OpsPortal DPNS page for a given year and emails when new
study postings appear. These are PDF links named like:
    DPA-2026-January-1234 Some Project.pdf
    DPNS-2026-March-5678 Another Project.pdf

We only track the LINKS (study name + URL), not the PDF contents — the
goal is to know when a new study is posted, not to parse it.

Runs weekly. Always emails (heartbeat), noting any new postings or "none new".

YEAR HANDLING (IMPORTANT):
  This monitor is hardcoded to watch 2026 via YEAR / YEAR_TYPE_ID below.
  SPP's `yearTypeId` values are not predictable year-over-year, so when
  2026 ends you must update these two constants. The email footer and the
  run logs both flag this near year-end so it's hard to miss.
"""

from __future__ import annotations

import json
import os
import re
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Make the parent dir importable so `from lib import ...` works
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from lib import fetch, notify  # noqa: E402

# ---------------------------------------------------------------------------
# Configuration  ← UPDATE THESE TWO LINES WHEN 2026 ENDS
# ---------------------------------------------------------------------------

YEAR = 2026
YEAR_TYPE_ID = 259  # SPP's internal ID for the 2026 page (changes each year)

# ---------------------------------------------------------------------------

MONITOR_NAME = f"SPP DPNS {YEAR}"
PAGE_URL = f"https://opsportal.spp.org/Studies/DPNS?yearTypeId={YEAR_TYPE_ID}"
LANDING_URL = "https://opsportal.spp.org/Studies/DPNS"

HERE = Path(__file__).resolve().parent
SNAPSHOT_DIR = HERE / "snapshots"
SEEN_FILE = SNAPSHOT_DIR / "seen.json"
WORKDIR = HERE / "_workdir"

# A study link is a PDF under the year's DPNS folder whose filename starts
# with DPA- or DPNS-. We capture the study identifier (e.g. DPA-2026-January-1234)
# and the full URL.
# Match href to a PDF in the DPNS files area.
STUDY_LINK_RE = re.compile(
    r'href=["\']([^"\']*?/(?:DPA|DPNS)-' + str(YEAR) + r'-[^"\']*?\.pdf)["\']',
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Fetch + extract
# ---------------------------------------------------------------------------


def fetch_studies() -> list[dict[str, str]]:
    """Fetch the DPNS page and extract study postings for the configured year.

    Returns a list of {"id": <study id>, "url": <pdf url>, "title": <readable>}.
    """
    resp = fetch.get(PAGE_URL)
    html = resp.text

    studies: list[dict[str, str]] = []
    seen_urls: set[str] = set()

    for match in STUDY_LINK_RE.finditer(html):
        raw_url = match.group(1)
        # Normalize relative URLs to absolute
        if raw_url.startswith("//"):
            url = "https:" + raw_url
        elif raw_url.startswith("/"):
            url = "https://opsportal.spp.org" + raw_url
        else:
            url = raw_url

        if url in seen_urls:
            continue
        seen_urls.add(url)

        study_id, title = _parse_study_name(url)
        studies.append({"id": study_id, "url": url, "title": title})

    return studies


def _parse_study_name(url: str) -> tuple[str, str]:
    """Extract the study identifier and a readable title from the PDF URL.

    Example URL filename:
        "DPA-2026-January-1234 Shale and Mica.pdf"
    →  id:    "DPA-2026-January-1234"
       title: "Shale and Mica"
    """
    # Get the filename portion (after the last slash), URL-decoded for spaces
    filename = url.rsplit("/", 1)[-1]
    filename = filename.replace("%20", " ").strip()
    # Drop the .pdf extension
    if filename.lower().endswith(".pdf"):
        filename = filename[:-4]

    # The study ID is the leading DPA-YYYY-Month-#### (or DPNS-...) token.
    m = re.match(r"((?:DPA|DPNS)-\d{4}-[A-Za-z]+-\d+)\s*(.*)", filename)
    if m:
        study_id = m.group(1)
        title = m.group(2).strip() or "(no title)"
    else:
        # Fallback: whole filename is the id
        study_id = filename
        title = "(no title)"
    return study_id, title


# ---------------------------------------------------------------------------
# Seen-set I/O
# ---------------------------------------------------------------------------


def load_seen() -> set[str]:
    if not SEEN_FILE.exists():
        return set()
    try:
        with SEEN_FILE.open("r") as f:
            data = json.load(f)
        return set(data.get("study_ids", []))
    except Exception:  # noqa: BLE001
        return set()


def save_seen(study_ids: set[str]) -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "year": YEAR,
        "count": len(study_ids),
        "study_ids": sorted(study_ids),
    }
    with SEEN_FILE.open("w") as f:
        json.dump(payload, f, indent=2)


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------


def _year_end_warning() -> str | None:
    """Return a warning string if we're near/after the end of the configured year."""
    now = datetime.now(timezone.utc)
    if now.year > YEAR:
        return (
            f"⚠️ This monitor is still configured for {YEAR}, but it is now "
            f"{now.year}. You need to update YEAR and YEAR_TYPE_ID in "
            f"monitors/spp_dpns/monitor.py to watch {now.year}. Until you do, "
            f"this monitor is watching last year's page."
        )
    if now.year == YEAR and now.month == 12:
        return (
            f"ℹ️ Reminder: {YEAR} is ending soon. When {YEAR + 1} begins, "
            f"update YEAR and YEAR_TYPE_ID in monitors/spp_dpns/monitor.py "
            f"to point at the {YEAR + 1} DPNS page. (Find the new yearTypeId "
            f"by opening the DPNS page and selecting {YEAR + 1} from the year "
            f"dropdown — the number is in the URL.)"
        )
    return None


def format_email(
    all_studies: list[dict[str, str]],
    new_studies: list[dict[str, str]],
) -> tuple[str, str]:
    new_count = len(new_studies)
    if new_count > 0:
        subject = (
            f"SPP DPNS {YEAR} — {new_count} new "
            f"stud{'ies' if new_count != 1 else 'y'} posted"
        )
    else:
        subject = f"SPP DPNS {YEAR} — no new studies"

    parts: list[str] = []
    parts.append(
        "<div style='font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;"
        "max-width:680px;color:#222;'>"
    )
    parts.append(f"<h2 style='margin-bottom:4px;'>SPP DPNS {YEAR} Update</h2>")
    parts.append(
        f"<p style='color:#666;margin-top:0;font-size:13px;'>"
        f"Checked at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · "
        f"{len(all_studies)} total stud{'ies' if len(all_studies) != 1 else 'y'} "
        f"posted for {YEAR} · "
        f"<strong>{new_count} new since last check</strong>"
        f"</p>"
    )

    # Year-end / stale-config warning, if applicable
    warning = _year_end_warning()
    if warning:
        parts.append(
            f"<p style='background:#fff8d4;border:1px solid #e0c95a;"
            f"padding:10px 14px;border-radius:4px;font-size:13px;'>{_esc(warning)}</p>"
        )

    # New studies (highlighted)
    if new_studies:
        parts.append("<h3>New studies</h3>")
        parts.append(_study_list(new_studies, highlight=True))

    # Full current list
    parts.append(f"<h3 style='margin-top:20px;'>All {YEAR} studies</h3>")
    if all_studies:
        parts.append(_study_list(all_studies, highlight=False))
    else:
        parts.append(
            f"<p>No studies have been posted for {YEAR} yet. This email "
            f"confirms the monitor is running and watching.</p>"
        )

    parts.append(
        f"<p style='margin-top:24px;font-size:13px;'>"
        f"Source: <a href='{PAGE_URL}'>SPP DPNS {YEAR} page</a>"
        f"</p>"
    )
    parts.append("</div>")
    return subject, "".join(parts)


def _study_list(studies: list[dict[str, str]], highlight: bool) -> str:
    bg = "background:#fff8d4;" if highlight else ""
    items = []
    for s in studies:
        items.append(
            f"<li style='margin-bottom:8px;{bg}padding:4px 6px;border-radius:3px;'>"
            f"<a href='{_esc(s['url'])}' style='font-weight:600;'>{_esc(s['id'])}</a>"
            f" — {_esc(s['title'])}"
            f"</li>"
        )
    return "<ul style='font-size:14px;padding-left:20px;'>" + "".join(items) + "</ul>"


def _esc(value: Any) -> str:
    s = str(value) if value is not None else ""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    recipient = os.environ.get("MONITOR_TO_ADDR")
    if not recipient:
        print("ERROR: MONITOR_TO_ADDR not set", file=sys.stderr)
        return 2

    try:
        WORKDIR.mkdir(parents=True, exist_ok=True)

        warning = _year_end_warning()
        if warning:
            print(f"[{MONITOR_NAME}] {warning}")

        print(f"[{MONITOR_NAME}] fetching {PAGE_URL}…")
        studies = fetch_studies()
        print(f"[{MONITOR_NAME}] found {len(studies)} study postings for {YEAR}")
        for s in studies:
            print(f"[{MONITOR_NAME}]   {s['id']} — {s['title']}")

        seen = load_seen()
        new_studies = [s for s in studies if s["id"] not in seen]
        print(f"[{MONITOR_NAME}] {len(new_studies)} are new since last run")

        subject, html = format_email(studies, new_studies)
        print(f"[{MONITOR_NAME}] sending email: {subject}")
        notify.send_email(to=recipient, subject=subject, html=html)

        # Update seen-set with everything currently posted
        seen.update(s["id"] for s in studies)
        save_seen(seen)
        print(f"[{MONITOR_NAME}] seen-set now has {len(seen)} study ids")
        return 0

    except Exception:  # noqa: BLE001
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        notify.send_failure_alert(recipient, MONITOR_NAME, tb)
        return 1


if __name__ == "__main__":
    sys.exit(main())
