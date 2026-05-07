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


def summarize(projects: list[dict[str, Any]]) -> dict[str, int]:
    """Compute the headline counts."""
    total = len(projects)
    withdrawn = sum(
        1 for p in projects if (p.get("Request Status") or "").lower() == "withdrawn"
    )
    ipp_carveouts = sum(
        1 for p in projects if (p.get("Carve Out Requested") or "").upper() == "IPP"
    )
    return {
        "total": total,
        "withdrawn": withdrawn,
        "ipp_carveouts": ipp_carveouts,
    }


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


# ---------------------------------------------------------------------------
# Email formatting
# ---------------------------------------------------------------------------


def format_email(
    counts: dict[str, int],
    prev_counts: dict[str, int] | None,
    diff: diff_lib.Diff | None,
    xlsx_url: str,
    last_check: str | None,
) -> tuple[str, str]:
    """Return (subject, html_body)."""
    delta = _format_deltas(counts, prev_counts) if prev_counts else None

    # Subject line packs the most important info
    if diff is None:
        subject = f"MISO ERAS — initial snapshot ({counts['total']} projects)"
    elif diff.has_changes:
        bits = []
        if diff.added:
            bits.append(f"+{len(diff.added)}")
        if diff.removed:
            bits.append(f"-{len(diff.removed)}")
        if diff.changed:
            bits.append(f"~{len(diff.changed)}")
        subject = f"MISO ERAS — {' '.join(bits)} | total {counts['total']}"
    else:
        subject = f"MISO ERAS — no changes | total {counts['total']}"

    html = _render_html(counts, delta, diff, xlsx_url, last_check)
    return subject, html


def _format_deltas(
    current: dict[str, int], previous: dict[str, int]
) -> dict[str, str]:
    out = {}
    for key in current:
        diff = current[key] - previous.get(key, 0)
        if diff > 0:
            out[key] = f"+{diff}"
        elif diff < 0:
            out[key] = f"{diff}"
        else:
            out[key] = "—"
    return out


def _render_html(
    counts: dict[str, int],
    delta: dict[str, str] | None,
    diff: diff_lib.Diff | None,
    xlsx_url: str,
    last_check: str | None,
) -> str:
    parts: list[str] = []
    parts.append(
        "<div style='font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;"
        "max-width:680px;color:#222;'>"
    )
    parts.append("<h2 style='margin-bottom:4px;'>MISO ERAS Queue Update</h2>")
    parts.append(
        f"<p style='color:#666;margin-top:0;font-size:13px;'>"
        f"Checked at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        + (f" · last check: {last_check}" if last_check else "")
        + "</p>"
    )

    # ---- Changes summary (top of email) ----
    parts.append("<h3>What changed</h3>")
    if diff is None:
        parts.append(
            "<p><em>This is the first snapshot — no previous data to compare against.</em></p>"
        )
    elif not diff.has_changes:
        parts.append("<p>No changes since last check.</p>")
    else:
        if diff.added:
            parts.append(f"<h4>Added ({len(diff.added)})</h4>")
            parts.append(_project_list(diff.added))
        if diff.removed:
            parts.append(f"<h4>Removed ({len(diff.removed)})</h4>")
            parts.append(_project_list(diff.removed))
        if diff.changed:
            parts.append(f"<h4>Status / field changes ({len(diff.changed)})</h4>")
            parts.append(_changes_list(diff.changed))

    # ---- Counts table ----
    parts.append("<h3>Counts</h3>")
    parts.append(_counts_table(counts, delta))

    # ---- Source link ----
    parts.append(
        f"<p style='margin-top:24px;font-size:13px;'>"
        f"Source: <a href='{xlsx_url}'>latest spreadsheet</a> · "
        f"<a href='{LANDING_URL}'>MISO Generator Interconnection page</a>"
        f"</p>"
    )
    parts.append("</div>")
    return "".join(parts)


def _counts_table(counts: dict[str, int], delta: dict[str, str] | None) -> str:
    rows = [
        ("Total projects", counts["total"], delta["total"] if delta else None),
        ("Withdrawn", counts["withdrawn"], delta["withdrawn"] if delta else None),
        ("IPP carveouts", counts["ipp_carveouts"], delta["ipp_carveouts"] if delta else None),
    ]
    html = (
        "<table style='border-collapse:collapse;font-size:14px;'>"
        "<thead><tr>"
        "<th style='text-align:left;padding:6px 12px;border-bottom:1px solid #ddd;'>Metric</th>"
        "<th style='text-align:right;padding:6px 12px;border-bottom:1px solid #ddd;'>Count</th>"
    )
    if delta:
        html += (
            "<th style='text-align:right;padding:6px 12px;border-bottom:1px solid #ddd;'>"
            "Δ since last</th>"
        )
    html += "</tr></thead><tbody>"
    for label, count, change in rows:
        html += (
            f"<tr><td style='padding:6px 12px;'>{label}</td>"
            f"<td style='padding:6px 12px;text-align:right;'>{count}</td>"
        )
        if delta:
            color = "#888" if change == "—" else ("#0a7d2c" if change.startswith("+") else "#b3261e")
            html += f"<td style='padding:6px 12px;text-align:right;color:{color};'>{change}</td>"
        html += "</tr>"
    html += "</tbody></table>"
    return html


def _project_list(projects: list[dict[str, Any]]) -> str:
    items = []
    for p in projects:
        label = _project_label(p)
        items.append(f"<li>{label}</li>")
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
            f"<li><strong>Application {project_id}</strong>"
            f"<ul style='margin-top:4px;'>{''.join(change_bits)}</ul></li>"
        )
    return "<ul style='font-size:14px;'>" + "".join(items) + "</ul>"


def _project_label(p: dict[str, Any]) -> str:
    pid = p.get("id", "?")
    customer = p.get("Interconnection Customer") or "(no customer listed)"
    status = p.get("Request Status") or "?"
    state = p.get("State") or ""
    fuel = p.get("Fuel Type") or ""
    bits = [f"<strong>App {pid}</strong>", f"{customer}", f"[{status}]"]
    if state:
        bits.append(state)
    if fuel:
        bits.append(fuel)
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
            prev_counts = None
        else:
            diff = diff_lib.diff_snapshots(
                previous, projects, id_field="id", ignore_fields=IGNORE_FIELDS
            )
            prev_counts = summarize(previous)

        last_check = previous_capture_time()
        subject, html = format_email(counts, prev_counts, diff, xlsx_url, last_check)

        print(f"[{MONITOR_NAME}] sending email: {subject}")
        notify.send_email(to=recipient, subject=subject, html=html)

        save_snapshot(projects, xlsx_url)
        print(f"[{MONITOR_NAME}] snapshot saved")
        return 0

    except Exception as exc:  # noqa: BLE001
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        notify.send_failure_alert(recipient, MONITOR_NAME, tb)
        return 1


if __name__ == "__main__":
    sys.exit(main())
