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

    html = _render_html(current, diff, source_last_updated, last_check)
    return subject, html


def _render_html(
    current: list[dict[str, Any]],
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
            parts.append(_project_list(diff.added))
        if diff.removed:
            parts.append(f"<h4>Removed ({len(diff.removed)})</h4>")
            parts.append(_project_list(diff.removed))
        if diff.changed:
            parts.append(f"<h4>Field changes ({len(diff.changed)})</h4>")
            parts.append(_changes_list(diff.changed))

    # ---- Source link ----
    parts.append(
        f"<p style='margin-top:24px;font-size:13px;'>"
        f"Source: <a href='{CSV_URL}'>latest CSV</a> · "
        f"<a href='{LANDING_URL}'>SPP GI Active page</a>"
        f"</p>"
    )
    parts.append("</div>")
    return "".join(parts)


def _project_list(projects: list[dict[str, Any]]) -> str:
    items = []
    for p in projects:
        items.append(f"<li>{_project_label(p)}</li>")
    return "<ul style='font-size:14px;'>" + "".join(items) + "</ul>"


def _changes_list(
    changes: list[tuple[str, dict[str, tuple[Any, Any]]]],
) -> str:
    items = []
    for project_id, field_changes in changes:
        change_bits = []
        for field_name, (old, new) in field_changes.items():
            change_bits.append(
                f"<li><strong>{field_name}:</strong> "
                f"<span style='color:#b3261e;'>{_fmt(old)}</span> → "
                f"<span style='color:#0a7d2c;'>{_fmt(new)}</span></li>"
            )
        items.append(
            f"<li><strong>{project_id}</strong>"
            f"<ul style='margin-top:4px;'>{''.join(change_bits)}</ul></li>"
        )
    return "<ul style='font-size:14px;'>" + "".join(items) + "</ul>"


def _project_label(p: dict[str, Any]) -> str:
    pid = p.get("id", "?")
    cluster = p.get("Current Cluster") or ""
    location = ", ".join(
        x for x in [p.get("Nearest Town or County"), p.get("State")] if x
    )
    capacity = p.get("Capacity") or p.get("MAX Summer MW") or ""
    fuel = p.get("Fuel Type") or p.get("Generation Type") or ""
    status = p.get("Status") or ""

    bits = [f"<strong>{pid}</strong>"]
    if status:
        bits.append(f"[{status}]")
    if location:
        bits.append(location)
    if capacity:
        bits.append(f"{capacity} MW")
    if fuel:
        bits.append(fuel)
    if cluster:
        bits.append(cluster)
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

        subject, html = format_email(projects, diff, source_last_updated, last_check)
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

