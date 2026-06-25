"""SPP DPNS (Delivery Point Network Study) posting monitor.

Watches one or more years of the SPP OpsPortal DPNS page and sends a SINGLE
combined email with a section per year. Studies are PDF links named like:
    DPA-2026-January-1234 Some Project.pdf

We only track the LINKS (study name + URL), not the PDF contents.

Runs weekly. Always emails (heartbeat), noting any new postings or "none new"
for each year.

YEAR CONFIGURATION:
  Years to watch are defined in YEARS below. SPP's `yearTypeId` values are
  NOT predictable year-over-year, so to add a year, find its id (open the
  DPNS page, pick the year from the dropdown, copy the number from the URL).
  Known: 2025 -> 249, 2026 -> 259.
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

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from lib import fetch, notify  # noqa: E402

# ---------------------------------------------------------------------------
# Configuration — years to watch, newest first.
#   current=True marks the actively-watched year (triggers a year-end reminder
#   to update config). Past/reference years use current=False.
#   To add 2027: find its yearTypeId, add an entry with current=True, and set
#   2026's current to False.
# ---------------------------------------------------------------------------

YEARS: list[dict[str, Any]] = [
    {"year": 2026, "year_type_id": 259, "current": True},
    {"year": 2025, "year_type_id": 249, "current": False},
]

MONITOR_NAME = "SPP DPNS"
LANDING_URL = "https://opsportal.spp.org/Studies/DPNS"

HERE = Path(__file__).resolve().parent
SNAPSHOT_DIR = HERE / "snapshots"
WORKDIR = HERE / "_workdir"


def _page_url(year_type_id: int) -> str:
    return f"https://opsportal.spp.org/Studies/DPNS?yearTypeId={year_type_id}"


def _seen_file(year: int) -> Path:
    return SNAPSHOT_DIR / f"seen_{year}.json"


def _study_link_re(year: int) -> re.Pattern:
    return re.compile(
        r'href=["\']([^"\']*?/(?:DPA|DPNS)-' + str(year) + r'-[^"\']*?\.pdf)["\']',
        re.IGNORECASE,
    )


# ---------------------------------------------------------------------------
# Fetch + extract
# ---------------------------------------------------------------------------


def fetch_studies(year: int, year_type_id: int) -> list[dict[str, str]]:
    resp = fetch.get(_page_url(year_type_id))
    html = resp.text

    studies: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    pattern = _study_link_re(year)

    for match in pattern.finditer(html):
        raw_url = match.group(1)
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
    filename = url.rsplit("/", 1)[-1].replace("%20", " ").strip()
    if filename.lower().endswith(".pdf"):
        filename = filename[:-4]
    m = re.match(r"((?:DPA|DPNS)-\d{4}-[A-Za-z]+-\d+)\s*(.*)", filename)
    if m:
        return m.group(1), (m.group(2).strip() or "(no title)")
    return filename, "(no title)"


# ---------------------------------------------------------------------------
# Seen-set I/O
# ---------------------------------------------------------------------------


def load_seen(year: int) -> set[str]:
    f = _seen_file(year)
    if not f.exists():
        return set()
    try:
        with f.open("r") as fh:
            return set(json.load(fh).get("study_ids", []))
    except Exception:  # noqa: BLE001
        return set()


def save_seen(year: int, study_ids: set[str]) -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "year": year,
        "count": len(study_ids),
        "study_ids": sorted(study_ids),
    }
    with _seen_file(year).open("w") as fh:
        json.dump(payload, fh, indent=2)


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------


def _year_end_warning(year_cfg: dict[str, Any]) -> str | None:
    if not year_cfg.get("current"):
        return None
    year = year_cfg["year"]
    now = datetime.now(timezone.utc)
    if now.year > year:
        return (
            f"⚠️ The current-year entry is still {year}, but it is now "
            f"{now.year}. Update the YEARS list in "
            f"monitors/spp_dpns/monitor.py to watch {now.year}."
        )
    if now.year == year and now.month == 12:
        return (
            f"ℹ️ Reminder: {year} is ending soon. When {year + 1} begins, add "
            f"it to the YEARS list in monitors/spp_dpns/monitor.py (find the "
            f"new yearTypeId via the DPNS page's year dropdown)."
        )
    return None


def build_combined_email(per_year: list[dict[str, Any]]) -> tuple[str, str]:
    total_new = sum(len(y["new"]) for y in per_year)

    if total_new > 0:
        new_bits = [f"{y['cfg']['year']}: +{len(y['new'])}" for y in per_year if y["new"]]
        subject = f"SPP DPNS — new studies ({', '.join(new_bits)})"
    else:
        years_str = "/".join(str(y["cfg"]["year"]) for y in per_year)
        subject = f"SPP DPNS {years_str} — no new studies"

    parts: list[str] = []
    parts.append(
        "<div style='font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;"
        "max-width:680px;color:#222;'>"
    )
    parts.append("<h2 style='margin-bottom:4px;'>SPP DPNS Update</h2>")
    parts.append(
        f"<p style='color:#666;margin-top:0;font-size:13px;'>"
        f"Checked at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · "
        f"<strong>{total_new} new since last check</strong> across "
        f"{len(per_year)} year{'s' if len(per_year) != 1 else ''}"
        f"</p>"
    )

    for y in per_year:
        warning = _year_end_warning(y["cfg"])
        if warning:
            parts.append(
                f"<p style='background:#fff8d4;border:1px solid #e0c95a;"
                f"padding:10px 14px;border-radius:4px;font-size:13px;'>"
                f"{_esc(warning)}</p>"
            )

    for y in per_year:
        cfg = y["cfg"]
        year = cfg["year"]
        all_studies = y["all"]
        new_studies = y["new"]

        label_suffix = " (reference)" if not cfg.get("current") else ""
        parts.append(
            f"<h3 style='margin-top:24px;border-bottom:2px solid #eee;"
            f"padding-bottom:4px;'>{year}{label_suffix} — "
            f"{len(all_studies)} stud{'ies' if len(all_studies) != 1 else 'y'}, "
            f"{len(new_studies)} new</h3>"
        )

        if new_studies:
            parts.append(
                "<p style='font-size:13px;margin:6px 0;'><strong>New since last check:</strong></p>"
            )
            parts.append(_study_list(new_studies, highlight=True))

        if all_studies:
            parts.append(
                f"<p style='font-size:13px;color:#666;margin:10px 0 4px;'>"
                f"All {year} studies:</p>"
            )
            parts.append(_study_list(all_studies, highlight=False))
        else:
            parts.append(
                f"<p style='font-size:14px;'>No studies posted for {year} yet.</p>"
            )

        parts.append(
            f"<p style='font-size:12px;margin-top:4px;'>"
            f"<a href='{_page_url(cfg['year_type_id'])}'>View {year} DPNS page</a></p>"
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
        per_year: list[dict[str, Any]] = []

        for cfg in YEARS:
            year = cfg["year"]
            print(f"[{MONITOR_NAME}] fetching {year} ({_page_url(cfg['year_type_id'])})…")
            studies = fetch_studies(year, cfg["year_type_id"])
            print(f"[{MONITOR_NAME}]   found {len(studies)} studies for {year}")
            for s in studies:
                print(f"[{MONITOR_NAME}]     {s['id']} — {s['title']}")
            seen = load_seen(year)
            new_studies = [s for s in studies if s["id"] not in seen]
            print(f"[{MONITOR_NAME}]   {len(new_studies)} new for {year}")
            per_year.append({"cfg": cfg, "all": studies, "new": new_studies})

        subject, html = build_combined_email(per_year)
        print(f"[{MONITOR_NAME}] sending email: {subject}")
        notify.send_email(to=recipient, subject=subject, html=html)

        for y in per_year:
            year = y["cfg"]["year"]
            seen = load_seen(year)
            seen.update(s["id"] for s in y["all"])
            save_seen(year, seen)
        print(f"[{MONITOR_NAME}] seen-sets updated")
        return 0

    except Exception:  # noqa: BLE001
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        notify.send_failure_alert(recipient, MONITOR_NAME, tb)
        return 1


if __name__ == "__main__":
    sys.exit(main())
