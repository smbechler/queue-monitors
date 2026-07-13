"""MISO Regulatory Filings (FERC Filing) monitor.

Watches MISO's regulatory-filings listing for the CURRENT month and emails
daily. Shows all filings for the month (Title linked + full Description) and
flags newly-filed ones since the last check.

The month auto-advances: it always queries whatever the current calendar
month is, so on August 1 it starts showing August filings with no code change.

Data source: MISO's public search API (Elasticsearch), the same backend the
regulatory-filings web page calls. Endpoint and query shape were captured
from live DevTools traffic. If MISO changes the API, the failure-alert email
will fire and we'll need to update the constants below.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from lib import fetch, notify  # noqa: E402

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MONITOR_NAME = "MISO Regulatory Filings"

# MISO's public search endpoint (Elasticsearch _search).
SEARCH_URL = (
    "https://www.misoenergy.org/api/find/"
    "Optics_Models_Find_RemoteHostedContentItem/_search"
)

# The public-facing page, for a "view all" link in the email.
LANDING_URL = (
    "https://www.misoenergy.org/legal/filings-and-orders/regulatory-filings/"
)

# We filter to this legal doc type (matches the web page's own filter).
LEGAL_DOC_TYPE = "FERC Filing"

HERE = Path(__file__).resolve().parent
SNAPSHOT_DIR = HERE / "snapshots"
WORKDIR = HERE / "_workdir"

# Month names padded exactly as MISO's index stores them: "07. July"
_MONTH_LABELS = {
    1: "01. January", 2: "02. February", 3: "03. March", 4: "04. April",
    5: "05. May", 6: "06. June", 7: "07. July", 8: "08. August",
    9: "09. September", 10: "10. October", 11: "11. November",
    12: "12. December",
}


def current_year_month() -> tuple[str, str]:
    """Return (year_str, month_label) for the current date in US Eastern.

    We use Eastern because MISO is an Eastern-timezone entity and the daily
    email fires at 5pm ET; using ET avoids an edge case where a late-UTC run
    on the last day of a month would roll to the next month early.
    """
    # Compute "now" in US Eastern without external tz libs: Eastern is UTC-4
    # (EDT) or UTC-5 (EST). We only need the calendar month, and month
    # boundaries at 5pm ET are never near the UTC date line, so a fixed -5
    # offset is safe for determining the month.
    from datetime import timedelta

    now_utc = datetime.now(timezone.utc)
    now_et = now_utc - timedelta(hours=5)
    return str(now_et.year), _MONTH_LABELS[now_et.month]


def _seen_file(year: str, month_label: str) -> Path:
    # Month label like "07. July" -> "07_July" for a clean filename
    safe = month_label.replace(". ", "_").replace(" ", "_")
    return SNAPSHOT_DIR / f"seen_{year}_{safe}.json"


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


def _build_query(year: str, month_label: str, from_offset: int, size: int) -> dict:
    """Build the Elasticsearch query payload.

    Mirrors the query captured from the live page: filter by legaldoctype,
    filingyear, and filingmonth (each year/month OR'd with a Properties.* variant
    to match whichever field the index uses).
    """
    return {
        "from": from_offset,
        "size": size,
        "sort": [
            {"displaytitle": {"order": "asc"}},
            {"Name": "asc"},
            "_score",
        ],
        "query": {
            "filtered": {
                "filter": {
                    "and": [
                        {"query": {"term": {"legaldoctype": LEGAL_DOC_TYPE}}},
                        {
                            "or": [
                                {"query": {"term": {"filingyear": year}}},
                                {"query": {"term": {"Properties.filingyear": year}}},
                            ]
                        },
                        {
                            "or": [
                                {"query": {"term": {"filingmonth": month_label}}},
                                {
                                    "query": {
                                        "term": {"Properties.filingmonth": month_label}
                                    }
                                },
                            ]
                        },
                    ]
                },
                "query": {"query_string": {"query": "*"}},
            }
        },
    }


def fetch_filings(year: str, month_label: str) -> list[dict[str, str]]:
    """Fetch all filings for the given year/month, paging through results.

    Returns a list of {"id", "title", "description", "url", "created"}.
    """
    page_size = 50
    from_offset = 0
    all_filings: list[dict[str, str]] = []

    while True:
        payload = _build_query(year, month_label, from_offset, page_size)
        print(f"[{MONITOR_NAME}] querying from={from_offset} size={page_size}…")
        resp = fetch.post_json(SEARCH_URL, json_body=payload)

        hits_obj = resp.get("hits", {})
        hits = hits_obj.get("hits", [])
        total = hits_obj.get("total", 0)

        if from_offset == 0:
            print(f"[{MONITOR_NAME}]   total hits reported: {total}")
            if hits:
                # Log the field names of the first _source for debugging
                src0 = hits[0].get("_source", {})
                print(
                    f"[{MONITOR_NAME}]   sample fields: "
                    f"{[k for k in src0.keys()][:12]}…"
                )

        if not hits:
            break

        for hit in hits:
            src = hit.get("_source", {})
            filing = _normalize_hit(hit.get("_id"), src)
            if filing:
                all_filings.append(filing)

        from_offset += len(hits)
        # Stop when we've collected everything the index says exists
        if isinstance(total, int) and from_offset >= total:
            break
        # Safety cap
        if from_offset >= 1000:
            print(f"[{MONITOR_NAME}]   safety cap at 1000; stopping")
            break

    return all_filings


def _normalize_hit(hit_id: Any, src: dict[str, Any]) -> dict[str, str] | None:
    """Extract the fields we care about from a hit's _source.

    Field names come from the live response, using the $$-suffixed keys.
    """
    def _s(*keys: str) -> str | None:
        for k in keys:
            if k in src and src[k] not in (None, ""):
                return src[k]
        return None

    url = _s("SearchHitUrl$$string", "SearchHitUrl")
    title = _s(
        "displaytitle",
        "SearchTitle$$string",
        "SearchTitle",
        "Name$$string",
        "Name",
    )
    description = _s("Description$$string", "Description") or ""

    if not (url or title):
        return None

    # Clean a trailing ".pdf" off the title for readability
    if title and title.lower().endswith(".pdf"):
        title = title[:-4]

    fid = str(hit_id) if hit_id is not None else (url or title)
    created = _s("Created$$date", "SearchPublishDate$$date", "Created") or ""

    return {
        "id": fid,
        "title": title or "(untitled)",
        "description": description,
        "url": url or LANDING_URL,
        "created": created,
    }


# ---------------------------------------------------------------------------
# Seen-set I/O
# ---------------------------------------------------------------------------


def load_seen(year: str, month_label: str) -> set[str]:
    f = _seen_file(year, month_label)
    if not f.exists():
        return set()
    try:
        with f.open("r") as fh:
            return set(json.load(fh).get("filing_ids", []))
    except Exception:  # noqa: BLE001
        return set()


def save_seen(year: str, month_label: str, filing_ids: set[str]) -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "year": year,
        "month": month_label,
        "count": len(filing_ids),
        "filing_ids": sorted(filing_ids),
    }
    with _seen_file(year, month_label).open("w") as fh:
        json.dump(payload, fh, indent=2)


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------


def _month_display(month_label: str) -> str:
    """'07. July' -> 'July'."""
    if ". " in month_label:
        return month_label.split(". ", 1)[1]
    return month_label


def format_email(
    filings: list[dict[str, str]],
    new_filings: list[dict[str, str]],
    year: str,
    month_label: str,
) -> tuple[str, str]:
    month = _month_display(month_label)
    new_count = len(new_filings)

    if new_count > 0:
        subject = (
            f"MISO Filings — {month} {year}: {new_count} new "
            f"filing{'s' if new_count != 1 else ''}"
        )
    else:
        subject = f"MISO Filings — {month} {year}: no new filings"

    parts: list[str] = []
    parts.append(
        "<div style='font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;"
        "max-width:680px;color:#222;'>"
    )
    parts.append(
        f"<h2 style='margin-bottom:4px;'>MISO Regulatory Filings — {month} {year}</h2>"
    )
    parts.append(
        f"<p style='color:#666;margin-top:0;font-size:13px;'>"
        f"Checked at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · "
        f"{len(filings)} filing{'s' if len(filings) != 1 else ''} this month · "
        f"<strong>{new_count} new since last check</strong>"
        f"</p>"
    )

    if new_filings:
        parts.append("<h3>New filings</h3>")
        parts.append(_filing_list(new_filings, highlight=True))

    parts.append(f"<h3 style='margin-top:20px;'>All {month} {year} filings</h3>")
    if filings:
        parts.append(_filing_list(filings, highlight=False))
    else:
        parts.append(
            f"<p>No filings posted for {month} {year} yet. This email "
            f"confirms the monitor is running.</p>"
        )

    parts.append(
        f"<p style='margin-top:24px;font-size:13px;'>"
        f"<a href='{LANDING_URL}'>MISO Regulatory Filings page</a></p>"
    )
    parts.append("</div>")
    return subject, "".join(parts)


def _filing_list(filings: list[dict[str, str]], highlight: bool) -> str:
    bg = "background:#fff8d4;" if highlight else ""
    items = []
    for f in filings:
        items.append(
            f"<li style='margin-bottom:14px;{bg}padding:6px 8px;border-radius:3px;'>"
            f"<a href='{_esc(f['url'])}' style='font-weight:600;'>{_esc(f['title'])}</a>"
            f"<br><span style='font-size:14px;'>{_esc(f['description'])}</span>"
            f"</li>"
        )
    return "<ul style='font-size:14px;padding-left:20px;list-style:none;'>" + "".join(items) + "</ul>"


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

        year, month_label = current_year_month()
        print(f"[{MONITOR_NAME}] tracking {month_label} {year}")

        filings = fetch_filings(year, month_label)
        print(f"[{MONITOR_NAME}] found {len(filings)} filings")
        for f in filings:
            print(f"[{MONITOR_NAME}]   {f['title']}")

        seen = load_seen(year, month_label)
        new_filings = [f for f in filings if f["id"] not in seen]
        print(f"[{MONITOR_NAME}] {len(new_filings)} new since last run")

        subject, html = format_email(filings, new_filings, year, month_label)
        print(f"[{MONITOR_NAME}] sending email: {subject}")
        notify.send_email(to=recipient, subject=subject, html=html)

        seen.update(f["id"] for f in filings)
        save_seen(year, month_label, seen)
        print(f"[{MONITOR_NAME}] seen-set updated ({len(seen)} ids)")
        return 0

    except Exception:  # noqa: BLE001
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        notify.send_failure_alert(recipient, MONITOR_NAME, tb)
        return 1


if __name__ == "__main__":
    sys.exit(main())
