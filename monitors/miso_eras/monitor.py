"""MISO ERAS Interconnection Queue monitor.

Fetches the latest ERAS Interconnection Requests spreadsheet from MISO,
parses it, diffs it against the previous snapshot, and emails a summary.

Designed to be run on a schedule (GitHub Actions cron). The previous
snapshot is read from `snapshots/latest.json` in this directory; after
a successful run, the new snapshot is written to the same path. When
committed back to the repo, this gives a full history of the queue
over time via git.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Make the parent dir importable so `from lib import ...` works
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from openpyxl import load_workbook  # noqa: E402

from lib import diff as diff_lib  # noqa: E402
from lib import fetch, notify  # noqa: E402

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MONITOR_NAME = "MISO ERAS Queue"
LANDING_URL = (
    "https://www.misoenergy.org/planning/resource-utilization/"
    "generator-interconnection/"
)
# The xlsx filename pattern on MISO's CDN. The numeric ID and ?v= query
# parameter both change when MISO republishes, so we scrape the landing
# page for the current link rather than hardcoding it.
XLSX_PATTERN = r"ERAS[%20\s]+Interconnection[%20\s]+Requests\d+\.xlsx"

HERE = Path(__file__).resolve().parent
SNAPSHOT_DIR = HERE / "snapshots"
LATEST_SNAPSHOT = SNAPSHOT_DIR / "latest.json"
STATE_FILE = SNAPSHOT_DIR / "state.json"  # tracks checks-since-last-email
WORKDIR = HERE / "_workdir"

# Fields that are noisy or expected to vary; ignored when diffing rows.
# (None for now — we want to see all changes. Add fields here if needed.)
IGNORE_FIELDS: tuple[str, ...] = ()

# ---------------------------------------------------------------------------
# Fetch + parse
# ---------------------------------------------------------------------------


def find_xlsx_url() -> str:
    """Scrape the MISO landing page to find the current xlsx URL.

    Raises a clear error if the link can't be found, so the failure
    alert email tells you exactly what broke.
    """
    resp = fetch.get(LANDING_URL)
    href = fetch.find_link(resp.text, XLSX_PATTERN)
    if not href:
        raise RuntimeError(
            f"Could not find ERAS xlsx link on {LANDING_URL}. "
            f"MISO may have changed their page structure. "
            f"Update XLSX_PATTERN in {Path(__file__).name}."
        )
    # Hrefs may be relative or protocol-relative; normalize.
    if href.startswith("//"):
        href = "https:" + href
    elif href.startswith("/"):
        href = "https://www.misoenergy.org" + href
    return href


def parse_xlsx(path: Path) -> list[dict[str, Any]]:
    """Parse the ERAS spreadsheet into a list of project dicts.

    The first row is the header. Each subsequent row becomes a dict
    keyed by column name. We use 'Application ID' as the stable
    identifier (Project Number can be blank for withdrawn entries).
    """
    wb = load_workbook(path, data_only=True, read_only=True)
    ws = wb.active
    rows = ws.iter_rows(values_only=True)

    headers_raw = next(rows, None)
    if not headers_raw:
        raise RuntimeError("Spreadsheet is empty — no header row found")
    headers = [_clean_header(h) for h in headers_raw]

    projects: list[dict[str, Any]] = []
    for row in rows:
        if not any(cell not in (None, "") for cell in row):
            continue  # skip blank rows
        record = {headers[i]: _clean_value(row[i]) for i in range(len(headers)) if i < len(row)}
        # Use Application ID as the stable id field for diffing.
        app_id = record.get("Application ID")
        if app_id is None:
            continue
        record["id"] = str(app_id)
        projects.append(record)

    wb.close()
    return projects


def _clean_header(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


def _clean_value(value: Any) -> Any:
    if isinstance(value, str):
        stripped = " ".join(value.split())
        return stripped if stripped else None
    if isinstance(value, datetime):
        return value.isoformat()
    return value


# ---------------------------------------------------------------------------
# Summarize
# ---------------------------------------------------------------------------


def summarize(projects: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute counts broken out by carve-out category and request status.

    Returns a structure like:
        {
          "statuses": ["Active", "Done", "Under review", "Withdrawn"],
          "regular": {"total": N, "by_status": {"Active": n, ...}},
          "ipp":     {"total": N, "by_status": {"Active": n, ...}},
        }

    Status columns are discovered dynamically from the data, so a new
    MISO status value shows up automatically without a code change.
    Carve Out values other than "IPP" (including blank/N/A) count as Regular.
    """
    # Discover the set of statuses present, in a stable sorted order.
    statuses = sorted(
        {_status_of(p) for p in projects if _status_of(p)}
    )

    regular_by_status: dict[str, int] = {s: 0 for s in statuses}
    ipp_by_status: dict[str, int] = {s: 0 for s in statuses}
    regular_total = 0
    ipp_total = 0

    for p in projects:
        status = _status_of(p)
        if _is_ipp(p):
            ipp_total += 1
            if status in ipp_by_status:
                ipp_by_status[status] += 1
        else:
            regular_total += 1
            if status in regular_by_status:
                regular_by_status[status] += 1

    return {
        "statuses": statuses,
        "regular": {"total": regular_total, "by_status": regular_by_status},
        "ipp": {"total": ipp_total, "by_status": ipp_by_status},
    }


def _status_of(project: dict[str, Any]) -> str:
    return (project.get("Request Status") or "").strip()


def _is_withdrawn(project: dict[str, Any]) -> bool:
    return (project.get("Request Status") or "").lower() == "withdrawn"


def _is_ipp(project: dict[str, Any]) -> bool:
    return (project.get("Carve Out Requested") or "").upper() == "IPP"


# ---------------------------------------------------------------------------
# Snapshot I/O
# ---------------------------------------------------------------------------


def load_previous() -> list[dict[str, Any]] | None:
    if not LATEST_SNAPSHOT.exists():
        return None
    with LATEST_SNAPSHOT.open("r") as f:
        data = json.load(f)
    return data.get("projects")


def save_snapshot(projects: list[dict[str, Any]], xlsx_url: str) -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "source_url": xlsx_url,
        "projects": projects,
    }
    with LATEST_SNAPSHOT.open("w") as f:
        json.dump(snapshot, f, indent=2, default=str)


def previous_capture_time() -> str | None:
    if not LATEST_SNAPSHOT.exists():
        return None
    try:
        with LATEST_SNAPSHOT.open("r") as f:
            return json.load(f).get("captured_at")
    except Exception:  # noqa: BLE001
        return None


def load_state() -> dict[str, Any]:
    """Load the run-state file. Tracks counter of checks since last email."""
    if not STATE_FILE.exists():
        return {"checks_since_last_email": 0, "last_email_at": None}
    try:
        with STATE_FILE.open("r") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {"checks_since_last_email": 0, "last_email_at": None}


def save_state(state: dict[str, Any]) -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    with STATE_FILE.open("w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# Email formatting
# ---------------------------------------------------------------------------


def format_email(
    counts: dict[str, Any],
    diff: diff_lib.Diff | None,
    current: list[dict[str, Any]],
    previous: list[dict[str, Any]] | None,
    xlsx_url: str,
    last_check: str | None,
    checks_since_last_email: int,
    is_change_alert: bool,
) -> tuple[str, str]:
    """Return (subject, html_body)."""
    # Subject line packs the most important info
    total = counts["regular"]["total"] + counts["ipp"]["total"]
    if diff is None:
        subject = f"MISO ERAS — initial snapshot ({total} projects)"
    elif diff.has_changes:
        bits = []
        if diff.added:
            bits.append(f"+{len(diff.added)}")
        if diff.removed:
            bits.append(f"-{len(diff.removed)}")
        if diff.changed:
            bits.append(f"~{len(diff.changed)}")
        prefix = "MISO ERAS [CHANGE]" if is_change_alert else "MISO ERAS"
        subject = f"{prefix} — {' '.join(bits)} | total {total}"
    else:
        subject = f"MISO ERAS — no changes | total {total}"

    html = _render_html(
        counts, diff, current, previous, xlsx_url, last_check,
        checks_since_last_email,
    )
    return subject, html


def _render_html(
    counts: dict[str, Any],
    diff: diff_lib.Diff | None,
    current: list[dict[str, Any]],
    previous: list[dict[str, Any]] | None,
    xlsx_url: str,
    last_check: str | None,
    checks_since_last_email: int,
) -> str:
    parts: list[str] = []
    parts.append(
        "<div style='font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;"
        "max-width:680px;color:#222;'>"
    )
    parts.append("<h2 style='margin-bottom:4px;'>MISO ERAS Queue Update</h2>")
    # checks_since_last_email is the number of checks performed since the
    # previous email, INCLUDING this one. So "1" = just this run, no extras.
    if checks_since_last_email <= 1:
        check_msg = "First check since last email."
    else:
        check_msg = (
            f"{checks_since_last_email} checks performed since last email "
            f"(including this one)."
        )
    parts.append(
        f"<p style='color:#666;margin-top:0;font-size:13px;'>"
        f"Checked at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        + (f" · last check: {last_check}" if last_check else "")
        + f"<br>{check_msg}"
        + "</p>"
    )

    # ---- Counts summary (top of email) ----
    parts.append("<h3>Summary</h3>")
    parts.append(_counts_table(counts))

    # ---- Changes ----
    parts.append("<h3 style='margin-top:24px;'>What changed</h3>")
    if diff is None:
        parts.append(
            "<p><em>This is the first snapshot — no previous data to compare against.</em></p>"
        )
    elif not diff.has_changes:
        parts.append("<p>No changes since last check.</p>")
    else:
        current_by_id = {p["id"]: p for p in current if p.get("id")}
        previous_by_id = (
            {p["id"]: p for p in previous if p.get("id")} if previous else {}
        )
        if diff.added:
            parts.append(f"<h4>Added ({len(diff.added)})</h4>")
            parts.append(_project_table(diff.added))
        if diff.removed:
            parts.append(f"<h4>Removed ({len(diff.removed)})</h4>")
            parts.append(_project_table(diff.removed))
        if diff.changed:
            parts.append(f"<h4>Field changes ({len(diff.changed)})</h4>")
            parts.append(
                _changed_project_table(diff.changed, current_by_id, previous_by_id)
            )

    # ---- Source link ----
    parts.append(
        f"<p style='margin-top:24px;font-size:13px;'>"
        f"Source: <a href='{xlsx_url}'>latest spreadsheet</a> · "
        f"<a href='{LANDING_URL}'>MISO Generator Interconnection page</a>"
        f"</p>"
    )
    parts.append("</div>")
    return "".join(parts)


def _counts_table(counts: dict[str, Any]) -> str:
    """Two-row summary: Regular vs IPP, broken out by request status.

    Columns: category label, Total, Total − Withdrawn, then one column
    per status discovered in the data.
    """
    statuses = counts["statuses"]

    # Header row
    header_cells = [
        "<th style='text-align:left;padding:6px 12px;border-bottom:2px solid #ccc;'></th>",
        "<th style='text-align:right;padding:6px 12px;border-bottom:2px solid #ccc;'>Total</th>",
        "<th style='text-align:right;padding:6px 12px;border-bottom:2px solid #ccc;'>Total − Withdrawn</th>",
    ]
    for status in statuses:
        header_cells.append(
            f"<th style='text-align:right;padding:6px 12px;border-bottom:2px solid #ccc;'>"
            f"{_esc(status)}</th>"
        )

    def _row(label: str, data: dict[str, Any]) -> str:
        total = data["total"]
        by_status = data["by_status"]
        # "Total − Withdrawn": everything in this category not in a
        # Withdrawn status. We compute it from the per-status counts.
        withdrawn = sum(
            n for s, n in by_status.items() if s.lower() == "withdrawn"
        )
        not_withdrawn = total - withdrawn
        cells = [
            f"<td style='padding:6px 12px;font-weight:600;'>{_esc(label)}</td>",
            f"<td style='padding:6px 12px;text-align:right;'>{total}</td>",
            f"<td style='padding:6px 12px;text-align:right;'>{not_withdrawn}</td>",
        ]
        for status in statuses:
            cells.append(
                f"<td style='padding:6px 12px;text-align:right;'>"
                f"{by_status.get(status, 0)}</td>"
            )
        return "<tr>" + "".join(cells) + "</tr>"

    html = (
        "<table style='border-collapse:collapse;font-size:14px;'>"
        "<thead><tr>" + "".join(header_cells) + "</tr></thead><tbody>"
        + _row("Regular", counts["regular"])
        + _row("IPP", counts["ipp"])
        + "</tbody></table>"
    )
    return html


# ---------------------------------------------------------------------------
# Change tables (SPP-style: full row, with changed cells highlighted)
# ---------------------------------------------------------------------------

# Columns shown in the Added/Removed/Changed tables, in display order.
# Each entry is (candidate CSV field names, display label). We try each
# candidate in order so a slightly different MISO header still resolves.
TABLE_COLUMNS: list[tuple[tuple[str, ...], str]] = [
    (("Project Number",), "Project Number"),
    (("Application ID", "Application"), "Application"),
    (("Interconnection Customer",), "Interconnection Customer"),
    (("Request Status",), "Request Status"),
    (("Submitted", "Order Submitted", "Order"), "Order Submitted"),
    (("Transmission Owner",), "Transmission Owner"),
    (("County",), "County"),
    (("State",), "State"),
    (("Study Cycle",), "Study Cycle"),
    (("Service Type",), "Service Type"),
    (("POI Name", "POI"), "POI Name"),
    (("Requested NRIS MW", "Requested NRIS", "Requested NRIS (MW)"), "Requested NRIS"),
    (("Requested ERIS MW", "Requested ERIS", "Requested ERIS (MW)"), "Requested ERIS"),
    (("Generating Facility",), "Generating Facility"),
    (("Location of Need",), "Location of Need"),
    (("Carve Out Requested",), "Carve Out Requested"),
]

_TABLE_STYLES = (
    "border-collapse:collapse;font-size:12px;font-family:-apple-system,"
    "BlinkMacSystemFont,Segoe UI,sans-serif;white-space:nowrap;"
)
_TH_STYLES = (
    "text-align:left;padding:6px 10px;background:#f4f4f4;"
    "border-bottom:2px solid #ccc;font-weight:600;"
)
_TD_STYLES = "padding:6px 10px;border-bottom:1px solid #eee;vertical-align:top;"


def _col_value(project: dict[str, Any], candidates: tuple[str, ...]) -> Any:
    """Return the first present value among candidate field names."""
    for field in candidates:
        if field in project and project[field] not in (None, ""):
            return project[field]
    return None


def _table_header() -> str:
    cells = "".join(
        f"<th style='{_TH_STYLES}'>{_esc(label)}</th>"
        for _, label in TABLE_COLUMNS
    )
    return f"<thead><tr>{cells}</tr></thead>"


def _project_table(projects: list[dict[str, Any]]) -> str:
    """Flat table for Added / Removed sections."""
    parts = ["<div style='overflow-x:auto;margin-bottom:16px;'>"]
    parts.append(f"<table style='{_TABLE_STYLES}'>")
    parts.append(_table_header())
    parts.append("<tbody>")
    for project in projects:
        parts.append("<tr>")
        for candidates, _ in TABLE_COLUMNS:
            parts.append(
                f"<td style='{_TD_STYLES}'>{_fmt(_col_value(project, candidates))}</td>"
            )
        parts.append("</tr>")
    parts.append("</tbody></table></div>")
    return "".join(parts)


def _changed_project_table(
    changes: list[tuple[str, dict[str, tuple[Any, Any]]]],
    current_by_id: dict[str, dict[str, Any]],
    previous_by_id: dict[str, dict[str, Any]],
) -> str:
    """Table for changed projects, with changed cells highlighted."""
    parts = ["<div style='overflow-x:auto;margin-bottom:16px;'>"]
    parts.append(f"<table style='{_TABLE_STYLES}'>")
    parts.append(_table_header())
    parts.append("<tbody>")

    for project_id, field_changes in changes:
        new_row = current_by_id.get(project_id, {})
        parts.append("<tr>")
        for candidates, _ in TABLE_COLUMNS:
            # A column is "changed" if any of its candidate field names
            # appears in the diff's field_changes for this project.
            changed_field = next(
                (f for f in candidates if f in field_changes), None
            )
            if changed_field:
                old_val, new_val = field_changes[changed_field]
                cell = (
                    f"<span style='color:#b3261e;text-decoration:line-through;'>"
                    f"{_fmt(old_val)}</span><br>"
                    f"<span style='color:#0a7d2c;font-weight:600;'>"
                    f"{_fmt(new_val)}</span>"
                )
                td_style = _TD_STYLES + "background:#fff8d4;white-space:normal;"
            else:
                cell = _fmt(_col_value(new_row, candidates))
                td_style = _TD_STYLES
            parts.append(f"<td style='{td_style}'>{cell}</td>")
        parts.append("</tr>")
    parts.append("</tbody></table></div>")
    return "".join(parts)


def _fmt(value: Any) -> str:
    if value is None or value == "":
        return "<em>(empty)</em>"
    return _esc(str(value))


def _esc(value: Any) -> str:
    s = str(value) if value is not None else ""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    recipient = os.environ.get("MONITOR_TO_ADDR")
    if not recipient:
        print("ERROR: MONITOR_TO_ADDR not set", file=sys.stderr)
        return 2

    # SEND_EMAIL is set to "true" by the workflow on scheduled-email times
    # (10am, 3pm, 6pm ET) and on manual runs. Otherwise we still check and
    # diff, but only email if there's an actual change to report.
    is_scheduled_email_time = (
        os.environ.get("SEND_EMAIL", "").lower() == "true"
    )

    try:
        WORKDIR.mkdir(parents=True, exist_ok=True)

        print(f"[{MONITOR_NAME}] discovering current xlsx URL…")
        xlsx_url = find_xlsx_url()
        print(f"[{MONITOR_NAME}] found: {xlsx_url}")

        print(f"[{MONITOR_NAME}] downloading…")
        local_xlsx = fetch.download(xlsx_url, WORKDIR / "current.xlsx")

        print(f"[{MONITOR_NAME}] parsing…")
        projects = parse_xlsx(local_xlsx)
        print(f"[{MONITOR_NAME}] parsed {len(projects)} projects")

        counts = summarize(projects)
        print(f"[{MONITOR_NAME}] counts: {counts}")

        previous = load_previous()
        if previous is None:
            diff = None
        else:
            diff = diff_lib.diff_snapshots(
                previous, projects, id_field="id", ignore_fields=IGNORE_FIELDS
            )

        # Decide whether to email:
        # - Yes, if this is a scheduled-email time (10am/3pm/6pm or manual)
        # - Yes, if it's the initial snapshot (no previous data)
        # - Yes, if anything changed since the last check
        # - Otherwise, just check silently and update the counter
        has_changes = diff is not None and diff.has_changes
        is_initial = diff is None
        send = is_scheduled_email_time or has_changes or is_initial
        is_change_alert = has_changes and not is_scheduled_email_time

        # Update the check counter. It tracks checks since the last email,
        # including the current one.
        state = load_state()
        state["checks_since_last_email"] = (
            state.get("checks_since_last_email", 0) + 1
        )

        last_check = previous_capture_time()

        if send:
            subject, html = format_email(
                counts,
                diff,
                projects,
                previous,
                xlsx_url,
                last_check,
                checks_since_last_email=state["checks_since_last_email"],
                is_change_alert=is_change_alert,
            )
            print(f"[{MONITOR_NAME}] sending email: {subject}")
            notify.send_email(to=recipient, subject=subject, html=html)
            # Reset counter after a successful email
            state["checks_since_last_email"] = 0
            state["last_email_at"] = datetime.now(timezone.utc).isoformat()
        else:
            print(
                f"[{MONITOR_NAME}] silent check "
                f"(#{state['checks_since_last_email']} since last email) — "
                f"no scheduled email, no changes"
            )

        save_snapshot(projects, xlsx_url)
        save_state(state)
        print(f"[{MONITOR_NAME}] snapshot + state saved")
        return 0

    except Exception as exc:  # noqa: BLE001
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        notify.send_failure_alert(recipient, MONITOR_NAME, tb)
        return 1


if __name__ == "__main__":
    sys.exit(main())
