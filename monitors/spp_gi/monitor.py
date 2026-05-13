"""SPP Generator Interconnection Active Request monitor.

Fetches the GI Active Requests CSV from SPP's Operations Portal once
a day, diffs it against the previous snapshot, and emails a summary
of what changed.

Unlike the MISO monitor, this one is simpler:
- One run per day (no silent checks, no counter, no scheduled-vs-change distinction)
- Always emails (even on no-change days, as a "still working" heartbeat)
- Focuses entirely on the change list (added / removed / field changes)

Source: https://opsportal.spp.org/Studies/GIActive
CSV download: https://opsportal.spp.org/Studies/GenerateActiveCSV
"""

from __future__ import annotations

import csv
import io
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Make the parent dir importable so `from lib import ...` works
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from lib import diff as diff_lib  # noqa: E402
from lib import fetch, notify  # noqa: E402

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MONITOR_NAME = "SPP GI Active Queue"
LANDING_URL = "https://opsportal.spp.org/Studies/GIActive"
CSV_URL = "https://opsportal.spp.org/Studies/GenerateActiveCSV"

# Stable identifier column for diffing
ID_COLUMN = "Generation Interconnection Number"

HERE = Path(__file__).resolve().parent
SNAPSHOT_DIR = HERE / "snapshots"
LATEST_SNAPSHOT = SNAPSHOT_DIR / "latest.json"
WORKDIR = HERE / "_workdir"

# Fields ignored when comparing rows (none for now — we want to see all changes)
IGNORE_FIELDS: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Fetch + parse
# ---------------------------------------------------------------------------


def parse_csv(text: str) -> tuple[list[dict[str, Any]], str | None]:
    """Parse the SPP GI Active CSV.

    The CSV starts with a metadata row like:
        "Last Updated On",5/4/2026,
    followed by the real header row and then data rows. We skip the
    metadata row, extract the date for the email header, and return
    a list of project dicts keyed by column name.
    """
    reader = csv.reader(io.StringIO(text))
    rows = iter(reader)

    # Row 1: metadata (Last Updated On)
    last_updated = None
    first_row = next(rows, None)
    if first_row and first_row[0].strip().lower().startswith("last updated"):
        if len(first_row) >= 2:
            last_updated = first_row[1].strip() or None
    else:
        # Unexpected — treat first row as header instead
        if first_row:
            return _parse_with_header(first_row, rows), None

    # Row 2: real header
    header_row = next(rows, None)
    if not header_row:
        raise RuntimeError("SPP CSV has no header row")
    return _parse_with_header(header_row, rows), last_updated


def _parse_with_header(
    header: list[str], rows: "csv._reader",
) -> list[dict[str, Any]]:
    headers = [_clean(h) for h in header]
    projects: list[dict[str, Any]] = []
    for row in rows:
        if not any(_clean(cell) for cell in row):
            continue  # skip blank lines
        record = {
            headers[i]: _clean(row[i]) if i < len(row) else None
            for i in range(len(headers))
        }
        project_id = record.get(ID_COLUMN)
        if not project_id:
            continue
        # Normalize the id field used by the diff library
        record["id"] = str(project_id)
        projects.append(record)
    return projects


def _clean(value: Any) -> Any:
    """Strip and collapse whitespace; empty strings become None."""
    if value is None:
        return None
    if isinstance(value, str):
        # Collapse any internal whitespace (including embedded newlines in CSV cells)
        stripped = " ".join(value.split())
        return stripped if stripped else None
    return value


# ---------------------------------------------------------------------------
# Snapshot I/O
# ---------------------------------------------------------------------------


def load_previous() -> tuple[list[dict[str, Any]] | None, str | None]:
    if not LATEST_SNAPSHOT.exists():
        return None, None
    try:
        with LATEST_SNAPSHOT.open("r") as f:
            data = json.load(f)
        return data.get("projects"), data.get("captured_at")
    except Exception:  # noqa: BLE001
        return None, None


def save_snapshot(
    projects: list[dict[str, Any]],
    source_last_updated: str | None,
) -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "source_last_updated": source_last_updated,
        "source_url": CSV_URL,
        "projects": projects,
    }
    with LATEST_SNAPSHOT.open("w") as f:
        json.dump(snapshot, f, indent=2, default=str)


# ---------------------------------------------------------------------------
# Email formatting
# ---------------------------------------------------------------------------


def format_email(
    current: list[dict[str, Any]],
    previous: list[dict[str, Any]] | None,
    diff: diff_lib.Diff | None,
    source_last_updated: str | None,
    last_check: str | None,
) -> tuple[str, str]:
    """Return (subject, html_body)."""
    if diff is None:
        subject = f"SPP GI — initial snapshot ({len(current)} projects)"
    elif diff.has_changes:
        bits = []
        if diff.added:
            bits.append(f"+{len(diff.added)}")
        if diff.removed:
            bits.append(f"-{len(diff.removed)}")
        if diff.changed:
            bits.append(f"~{len(diff.changed)}")
        subject = f"SPP GI — {' '.join(bits)} | total {len(current)}"
    else:
        subject = f"SPP GI — no changes | total {len(current)}"

    html = _render_html(current, previous, diff, source_last_updated, last_check)
    return subject, html


def _render_html(
    current: list[dict[str, Any]],
    previous: list[dict[str, Any]] | None,
    diff: diff_lib.Diff | None,
    source_last_updated: str | None,
    last_check: str | None,
) -> str:
    parts: list[str] = []
    parts.append(
        "<div style='font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;"
        "max-width:680px;color:#222;'>"
    )
    parts.append("<h2 style='margin-bottom:4px;'>SPP GI Active Queue Update</h2>")

    header_bits = [
        f"Checked at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    ]
    if source_last_updated:
        header_bits.append(f"source last updated: {source_last_updated}")
    if last_check:
        header_bits.append(f"previous check: {last_check}")
    parts.append(
        f"<p style='color:#666;margin-top:0;font-size:13px;'>"
        + " · ".join(header_bits)
        + f"<br>Total projects: <strong>{len(current)}</strong></p>"
    )

    # ---- Changes summary ----
    parts.append("<h3>What changed since yesterday</h3>")
    if diff is None:
        parts.append(
            "<p><em>This is the first snapshot — no previous data to compare against. "
            "Tomorrow's email will show changes since today.</em></p>"
        )
    elif not diff.has_changes:
        parts.append("<p>No changes since the previous snapshot.</p>")
    else:
        if diff.added:
            parts.append(f"<h4>Added ({len(diff.added)})</h4>")
            parts.append(_project_table(diff.added, kind="added"))
        if diff.removed:
            parts.append(f"<h4>Removed ({len(diff.removed)})</h4>")
            parts.append(_project_table(diff.removed, kind="removed"))
        if diff.changed:
            parts.append(f"<h4>Field changes ({len(diff.changed)})</h4>")
            current_by_id = {p["id"]: p for p in current if p.get("id")}
            previous_by_id = (
                {p["id"]: p for p in previous if p.get("id")}
                if previous
                else {}
            )
            parts.append(
                _render_changed_table_with_state(
                    diff.changed, current_by_id, previous_by_id
                )
            )

    # ---- Source link ----
    parts.append(
        f"<p style='margin-top:24px;font-size:13px;'>"
        f"Source: <a href='{CSV_URL}'>latest CSV</a> · "
        f"<a href='{LANDING_URL}'>SPP GI Active page</a>"
        f"</p>"
    )
    parts.append("</div>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Table rendering for changed/added/removed projects
# ---------------------------------------------------------------------------

# The column lineup for the change tables. Each entry is (CSV field name, display label).
# These are the columns the user wants to see. "COD" maps to Commercial
# Operation Date.
TABLE_COLUMNS: list[tuple[str, str]] = [
    ("Generation Interconnection Number", "GI Number"),
    ("Current Cluster", "Current Cluster"),
    ("Cluster Group", "Cluster Group"),
    ("Nearest Town or County", "Town/County"),
    ("State", "State"),
    ("TO at POI", "TO at POI"),
    ("Commercial Operation Date", "COD"),
    ("Capacity", "Capacity (MW)"),
    ("Service Type", "Service Type"),
    ("Nameplate Capacity", "Nameplate"),
    ("Generation Type", "Gen Type"),
    ("Substation or Line", "Substation/Line"),
    ("Request Received", "Request Received"),
    ("Date Withdrawn", "Date Withdrawn"),
    ("Status", "Status"),
]

# Inline CSS shared by all change tables. Tables scroll horizontally when
# they overflow the email container (15 columns is wider than a phone screen).
_TABLE_STYLES = (
    "border-collapse:collapse;font-size:12px;font-family:-apple-system,"
    "BlinkMacSystemFont,Segoe UI,sans-serif;white-space:nowrap;"
)
_TH_STYLES = (
    "text-align:left;padding:6px 10px;background:#f4f4f4;"
    "border-bottom:2px solid #ccc;font-weight:600;"
)
_TD_STYLES = "padding:6px 10px;border-bottom:1px solid #eee;vertical-align:top;"


def _project_table(projects: list[dict[str, Any]], kind: str) -> str:
    """Render a flat table of projects (used for Added and Removed sections)."""
    parts: list[str] = []
    parts.append("<div style='overflow-x:auto;margin-bottom:16px;'>")
    parts.append(f"<table style='{_TABLE_STYLES}'>")
    parts.append("<thead><tr>")
    for _, label in TABLE_COLUMNS:
        parts.append(f"<th style='{_TH_STYLES}'>{label}</th>")
    parts.append("</tr></thead><tbody>")
    for project in projects:
        parts.append("<tr>")
        for csv_field, _ in TABLE_COLUMNS:
            value = _fmt(project.get(csv_field))
            parts.append(f"<td style='{_TD_STYLES}'>{value}</td>")
        parts.append("</tr>")
    parts.append("</tbody></table></div>")
    return "".join(parts)


def _render_changed_table_with_state(
    changes: list[tuple[str, dict[str, tuple[Any, Any]]]],
    current_by_id: dict[str, dict[str, Any]],
    previous_by_id: dict[str, dict[str, Any]],
) -> str:
    """Render the changed-projects table with per-cell change highlighting."""
    parts: list[str] = []
    parts.append("<div style='overflow-x:auto;margin-bottom:16px;'>")
    parts.append(f"<table style='{_TABLE_STYLES}'>")
    parts.append("<thead><tr>")
    for _, label in TABLE_COLUMNS:
        parts.append(f"<th style='{_TH_STYLES}'>{label}</th>")
    parts.append("</tr></thead><tbody>")

    for project_id, field_changes in changes:
        new_row = current_by_id.get(project_id, {})
        old_row = previous_by_id.get(project_id, {})
        parts.append("<tr>")
        for csv_field, _ in TABLE_COLUMNS:
            if csv_field in field_changes:
                old_val, new_val = field_changes[csv_field]
                cell = (
                    f"<span style='color:#b3261e;text-decoration:line-through;'>"
                    f"{_fmt(old_val)}</span><br>"
                    f"<span style='color:#0a7d2c;font-weight:600;'>"
                    f"{_fmt(new_val)}</span>"
                )
                # Light yellow background to draw the eye to the changed cell
                td_style = _TD_STYLES + "background:#fff8d4;white-space:normal;"
            else:
                cell = _fmt(new_row.get(csv_field))
                td_style = _TD_STYLES
            parts.append(f"<td style='{td_style}'>{cell}</td>")
        parts.append("</tr>")
    parts.append("</tbody></table></div>")
    return "".join(parts)
    return " · ".join(bits)


def _fmt(value: Any) -> str:
    if value is None or value == "":
        return "<em>(empty)</em>"
    return str(value)


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

        print(f"[{MONITOR_NAME}] downloading CSV…")
        resp = fetch.get(CSV_URL)
        # Save a copy for debugging; not committed (gitignored _workdir)
        (WORKDIR / "current.csv").write_bytes(resp.content)
        csv_text = resp.text

        print(f"[{MONITOR_NAME}] parsing…")
        projects, source_last_updated = parse_csv(csv_text)
        print(
            f"[{MONITOR_NAME}] parsed {len(projects)} projects "
            f"(source last updated: {source_last_updated or 'unknown'})"
        )

        previous, last_check = load_previous()
        if previous is None:
            diff = None
        else:
            diff = diff_lib.diff_snapshots(
                previous, projects, id_field="id", ignore_fields=IGNORE_FIELDS
            )
            print(
                f"[{MONITOR_NAME}] diff: "
                f"+{len(diff.added)} -{len(diff.removed)} ~{len(diff.changed)}"
            )

        subject, html = format_email(
            projects, previous, diff, source_last_updated, last_check
        )
        print(f"[{MONITOR_NAME}] sending email: {subject}")
        notify.send_email(to=recipient, subject=subject, html=html)

        save_snapshot(projects, source_last_updated)
        print(f"[{MONITOR_NAME}] snapshot saved")
        return 0

    except Exception:  # noqa: BLE001
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        notify.send_failure_alert(recipient, MONITOR_NAME, tb)
        return 1


if __name__ == "__main__":
    sys.exit(main())
