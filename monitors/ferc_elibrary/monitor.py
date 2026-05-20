"""FERC eLibrary new-documents monitor.

Once a week, queries FERC's eLibrary search for documents filed in the
last N days, filters by configured criteria (docket prefix AND keywords
in description), and emails a list of NEW matches we haven't reported
on before.

Differs from MISO/SPP monitors:
- Source is a search API, not a static file
- "What's new" is defined as accession numbers we haven't seen,
  not as field-level changes in a row
- Snapshot is just a record of accession numbers already reported

The FERC eLibrary search endpoint and its request/response shape are
not officially documented. The constants below reflect the current
shape based on the public-facing frontend's behavior. If FERC changes
the API (which they have done before), the failure-alert email will
tell you the script broke and we'll need to update these.
"""

from __future__ import annotations

import json
import os
import re
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Make the parent dir importable so `from lib import ...` works
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import yaml  # noqa: E402

from lib import fetch, notify  # noqa: E402

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MONITOR_NAME = "FERC eLibrary"

# FERC eLibrary public search endpoint. The eLibrary frontend at
# https://elibrary.ferc.gov/eLibrary/search calls this internally.
# This endpoint is undocumented; if it stops working, open the eLibrary
# search page in a browser, open DevTools → Network tab, perform a
# search, and copy the resulting POST request's URL and JSON body.
#
# URL and payload schema verified from live DevTools traffic.
SEARCH_URL = "https://elibrary.ferc.gov/eLibraryWebAPI/api/Search/AdvancedSearch"

# Public eLibrary doc-info URL pattern. Used to build a "view this document"
# link in the email. {accession} is the document's accession number.
DOC_INFO_URL = "https://elibrary.ferc.gov/eLibrary/docinfo?accession_number={accession}"

LANDING_URL = "https://elibrary.ferc.gov/eLibrary/search"

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "config.yml"
SNAPSHOT_DIR = HERE / "snapshots"
SEEN_FILE = SNAPSHOT_DIR / "seen.json"
WORKDIR = HERE / "_workdir"

# Cap on how many seen accession numbers we keep. The seen-set prevents
# re-reporting the same doc; we don't need infinite history.
SEEN_RETENTION_LIMIT = 50_000


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def load_config() -> dict[str, Any]:
    if not CONFIG_FILE.exists():
        raise RuntimeError(f"Config file not found: {CONFIG_FILE}")
    with CONFIG_FILE.open("r") as f:
        config = yaml.safe_load(f) or {}
    # Validate basics
    if not config.get("docket_prefixes"):
        raise RuntimeError("config.yml must define docket_prefixes")
    if not config.get("keywords"):
        raise RuntimeError("config.yml must define keywords")
    config.setdefault("lookback_days", 8)
    return config


# ---------------------------------------------------------------------------
# FERC search
# ---------------------------------------------------------------------------


def search_ferc(
    start_date: datetime,
    end_date: datetime,
    docket_prefixes: list[str],
) -> list[dict[str, Any]]:
    """Search FERC eLibrary for filings in [start_date, end_date].

    Filters by docket prefix server-side (FERC's API supports this via
    `docketSearches`), which keeps each request small. We still apply the
    keyword filter locally afterward.

    Returns a list of raw document dicts as returned by the API.
    """
    start = start_date.strftime("%Y-%m-%d")
    end = end_date.strftime("%Y-%m-%d")

    all_results: list[dict[str, Any]] = []
    seen_accessions: set[str] = set()  # dedupe across multiple prefix queries

    # Loop over each configured docket prefix. The API's docket field accepts
    # a partial value (no dash) which acts as a prefix match in FERC's UI.
    for prefix in docket_prefixes:
        print(f"[{MONITOR_NAME}] searching docket prefix '{prefix}'…")
        page = 0
        while True:
            payload = _build_payload(start, end, prefix, page)
            print(f"[{MONITOR_NAME}]   page {page}…")
            resp = fetch.post_json(SEARCH_URL, json_body=payload)

            # On the first page of the first prefix, log response shape so
            # we can adapt if FERC changes field names.
            if page == 0 and prefix == docket_prefixes[0]:
                print(
                    f"[{MONITOR_NAME}]   response top-level keys: "
                    f"{list(resp.keys()) if isinstance(resp, dict) else type(resp).__name__}"
                )
                # Log result counts if present so we know how many FERC found
                for count_key in ("totalHits", "numHits", "totalCount", "count"):
                    if count_key in resp:
                        print(
                            f"[{MONITOR_NAME}]   {count_key}: {resp[count_key]}"
                        )
                # Log if FERC reports an error
                if resp.get("errorMessage"):
                    print(
                        f"[{MONITOR_NAME}]   errorMessage: {resp['errorMessage']}"
                    )
                if resp.get("success") is False:
                    print(f"[{MONITOR_NAME}]   success=False — request rejected")
                # Save the raw response so we can inspect the full structure
                # if results aren't being extracted correctly
                try:
                    (WORKDIR / "first_response.json").write_text(
                        json.dumps(resp, indent=2, default=str)[:50000],
                        encoding="utf-8",
                    )
                    print(
                        f"[{MONITOR_NAME}]   raw response saved to "
                        f"_workdir/first_response.json"
                    )
                except Exception as e:  # noqa: BLE001
                    print(f"[{MONITOR_NAME}]   (couldn't save raw response: {e})")

            # FERC's response shape: try common keys for the doc list.
            # `searchHits` is the key FERC's AdvancedSearch endpoint uses.
            docs = (
                resp.get("searchHits")
                or resp.get("documents")
                or resp.get("results")
                or resp.get("data")
                or resp.get("items")
                or resp.get("searchResults")
                or (resp if isinstance(resp, list) else [])
            )

            if not docs:
                break

            if page == 0 and prefix == docket_prefixes[0] and docs:
                print(
                    f"[{MONITOR_NAME}]   first doc keys: {list(docs[0].keys())}"
                )

            # Dedupe (same doc could match multiple prefixes in theory)
            page_new = 0
            for doc in docs:
                accession = (
                    doc.get("accessionNumber")
                    or doc.get("accession_number")
                    or doc.get("AccessionNumber")
                )
                key = str(accession) if accession else json.dumps(doc, sort_keys=True)
                if key in seen_accessions:
                    continue
                seen_accessions.add(key)
                all_results.append(doc)
                page_new += 1
            print(
                f"[{MONITOR_NAME}]   got {len(docs)} docs "
                f"({page_new} new, running total: {len(all_results)})"
            )

            # Stop paginating this prefix if we got fewer than a full page
            if len(docs) < 100:
                break
            # Safety cap so a broken response doesn't infinite-loop
            if page >= 50:
                print(f"[{MONITOR_NAME}]   pagination safety cap at page 50; stopping")
                break
            page += 1

    return all_results


def _build_payload(
    start: str, end: str, docket_prefix: str, page: int
) -> dict[str, Any]:
    """Build the search request payload.

    Schema verified from live DevTools traffic on elibrary.ferc.gov.
    Notes:
    - `searchText: "*"` is the wildcard that means "match anything"
    - `curPage` is 0-indexed
    - `dateSearches[*].dateType = "filed_date"` filters by filed date
    - `docketSearches[*].docketNumber` is a prefix match (e.g. "ER" matches all ER dockets)
    - `searchDescription: true` indexes against the doc description
    - `searchFullText: false` skips full-text search (we filter by keyword locally)
    """
    return {
        "searchText": "*",
        "searchFullText": False,
        "searchDescription": True,
        "accessionNumber": None,
        "affiliations": [],
        "allDates": False,
        "availability": None,
        "categories": [],
        "classTypes": [],
        "curPage": page,
        "dateSearches": [
            {
                "dateType": "filed_date",
                "startDate": start,
                "endDate": end,
            }
        ],
        "docketSearches": [
            {
                "docketNumber": docket_prefix,
                "subDocketNumbers": [],
            }
        ],
        "eFiling": False,
        "groupBy": "NONE",
        "idolResultID": "",
        "libraries": [],
        "resultsPerPage": 100,
        "sortBy": "",
    }


def normalize_document(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize the FERC API response into a stable shape we use elsewhere.

    The API's field names may vary — this function maps common alternates
    to canonical keys. If a field name turns out to be different than
    expected, update the candidate lists here.
    """
    return {
        "accession_number": _first(
            raw, "accessionNumber", "accession_number", "accession", "AccessionNumber"
        ),
        "docket_number": _first(
            raw, "docketNumber", "docket_number", "docket", "DocketNumber"
        ),
        "filed_date": _first(
            raw, "filedDate", "filed_date", "filed", "FiledDate"
        ),
        "description": _first(
            raw, "description", "Description", "shortDescription", "title", "Title"
        ),
        "document_type": _first(
            raw,
            "documentType",
            "document_type",
            "classType",
            "class_type",
            "ClassType",
            "type",
        ),
        "library": _first(raw, "library", "Library"),
        "category": _first(raw, "category", "Category"),
        "submitter": _first(
            raw, "submitter", "Submitter", "submittedBy", "filedBy", "FiledBy"
        ),
        # Keep the raw blob too, for forensic debugging if something looks off
        "_raw": raw,
    }


def _first(d: dict[str, Any], *keys: str) -> Any:
    """Return the first non-None value from `d` matching any of `keys`."""
    for k in keys:
        if k in d and d[k] is not None and d[k] != "":
            return d[k]
    return None


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def filter_documents(
    documents: list[dict[str, Any]], config: dict[str, Any]
) -> list[dict[str, Any]]:
    """Apply the AND filter: docket prefix AND keyword in description."""
    docket_prefixes = tuple(p.upper() for p in config["docket_prefixes"])
    keywords = config["keywords"]

    # Precompile keyword patterns with word boundaries for cleaner matching.
    # Word boundaries prevent "PJM" from matching "PJMASTER", and let
    # multi-word phrases like "Southwest Power Pool" match naturally.
    keyword_patterns = [
        re.compile(r"\b" + re.escape(kw) + r"\b", re.IGNORECASE) for kw in keywords
    ]

    matches = []
    for doc in documents:
        docket = (doc.get("docket_number") or "").upper().strip()
        description = doc.get("description") or ""

        # Filter 1: docket prefix
        if not any(docket.startswith(prefix) for prefix in docket_prefixes):
            continue

        # Filter 2: at least one keyword matches the description
        matched_keywords = [
            kw for kw, pat in zip(keywords, keyword_patterns) if pat.search(description)
        ]
        if not matched_keywords:
            continue

        doc["matched_keywords"] = matched_keywords
        matches.append(doc)

    return matches


# ---------------------------------------------------------------------------
# Seen-set I/O
# ---------------------------------------------------------------------------


def load_seen() -> set[str]:
    if not SEEN_FILE.exists():
        return set()
    try:
        with SEEN_FILE.open("r") as f:
            data = json.load(f)
        return set(data.get("accession_numbers", []))
    except Exception:  # noqa: BLE001
        return set()


def save_seen(accession_numbers: set[str]) -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    # Cap the seen-set size to avoid unbounded growth. Keep newest by
    # sorting accession numbers descending (FERC accession numbers start
    # with YYYYMMDD so lexical sort = chronological sort).
    trimmed = sorted(accession_numbers, reverse=True)[:SEEN_RETENTION_LIMIT]
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(trimmed),
        "accession_numbers": trimmed,
    }
    with SEEN_FILE.open("w") as f:
        json.dump(payload, f, indent=2)


# ---------------------------------------------------------------------------
# Email formatting
# ---------------------------------------------------------------------------


def format_email(
    new_matches: list[dict[str, Any]],
    total_filings_searched: int,
    total_matches_this_period: int,
    lookback_days: int,
    config: dict[str, Any],
) -> tuple[str, str]:
    """Return (subject, html_body)."""
    count = len(new_matches)
    if count == 0:
        subject = f"FERC eLibrary — no new matches this week"
    else:
        subject = f"FERC eLibrary — {count} new match{'es' if count != 1 else ''}"

    html = _render_html(
        new_matches,
        total_filings_searched,
        total_matches_this_period,
        lookback_days,
        config,
    )
    return subject, html


def _render_html(
    new_matches: list[dict[str, Any]],
    total_filings_searched: int,
    total_matches_this_period: int,
    lookback_days: int,
    config: dict[str, Any],
) -> str:
    parts: list[str] = []
    parts.append(
        "<div style='font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;"
        "max-width:680px;color:#222;'>"
    )
    parts.append("<h2 style='margin-bottom:4px;'>FERC eLibrary Weekly Update</h2>")
    parts.append(
        f"<p style='color:#666;margin-top:0;font-size:13px;'>"
        f"Run at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · "
        f"searched last {lookback_days} days · "
        f"{total_filings_searched} total filings · "
        f"{total_matches_this_period} matched criteria · "
        f"<strong>{len(new_matches)} new since last week</strong>"
        f"</p>"
    )

    # ---- The list ----
    if not new_matches:
        parts.append(
            "<p>No new documents matched your criteria this week. "
            "This email confirms the monitor is running correctly.</p>"
        )
    else:
        parts.append("<h3>New matching documents</h3>")
        parts.append(_doc_list(new_matches))

    # ---- Criteria reminder ----
    parts.append("<h3 style='margin-top:24px;'>Current criteria</h3>")
    parts.append(
        f"<p style='font-size:13px;color:#666;'>"
        f"<strong>Docket prefixes:</strong> "
        f"{', '.join(config['docket_prefixes'])}<br>"
        f"<strong>Keywords (any match in description):</strong> "
        f"{', '.join(repr(k) for k in config['keywords'])}<br>"
        f"<em>Edit monitors/ferc_elibrary/config.yml to change.</em>"
        f"</p>"
    )

    # ---- Source link ----
    parts.append(
        f"<p style='margin-top:24px;font-size:13px;'>"
        f"<a href='{LANDING_URL}'>FERC eLibrary search page</a>"
        f"</p>"
    )
    parts.append("</div>")
    return "".join(parts)


def _doc_list(docs: list[dict[str, Any]]) -> str:
    items = []
    for doc in docs:
        accession = doc.get("accession_number") or "?"
        docket = doc.get("docket_number") or "?"
        filed = doc.get("filed_date") or "?"
        # Trim time portion if present (e.g. "2026-05-12T00:00:00" → "2026-05-12")
        if isinstance(filed, str) and "T" in filed:
            filed = filed.split("T")[0]
        description = doc.get("description") or "(no description)"
        doc_type = doc.get("document_type") or "(unknown type)"
        submitter = doc.get("submitter") or ""
        matched = doc.get("matched_keywords") or []
        link = DOC_INFO_URL.format(accession=accession)

        items.append(
            f"<li style='margin-bottom:14px;'>"
            f"<a href='{link}' style='font-weight:600;'>{_esc(docket)}</a> · "
            f"<span style='color:#666;'>{_esc(filed)} · {_esc(doc_type)}</span><br>"
            f"<span>{_esc(description)}</span>"
            + (
                f"<br><span style='color:#666;font-size:12px;'>"
                f"Submitter: {_esc(submitter)}</span>"
                if submitter
                else ""
            )
            + f"<br><span style='color:#666;font-size:12px;'>"
            f"Accession: {_esc(accession)} · "
            f"Matched keywords: {_esc(', '.join(matched))}</span>"
            f"</li>"
        )
    return "<ul style='font-size:14px;padding-left:20px;'>" + "".join(items) + "</ul>"


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

    try:
        WORKDIR.mkdir(parents=True, exist_ok=True)

        print(f"[{MONITOR_NAME}] loading config…")
        config = load_config()
        lookback_days = int(config["lookback_days"])
        print(
            f"[{MONITOR_NAME}] criteria: docket_prefixes={config['docket_prefixes']}, "
            f"keywords={config['keywords']}, lookback_days={lookback_days}"
        )

        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=lookback_days)

        print(f"[{MONITOR_NAME}] searching FERC…")
        raw_docs = search_ferc(start_date, end_date, config["docket_prefixes"])
        print(f"[{MONITOR_NAME}] retrieved {len(raw_docs)} raw documents")

        # Save raw response for debugging (gitignored _workdir)
        (WORKDIR / "raw_results.json").write_text(
            json.dumps(raw_docs[:5], indent=2, default=str),
            encoding="utf-8",
        )

        documents = [normalize_document(d) for d in raw_docs]
        matched = filter_documents(documents, config)
        print(f"[{MONITOR_NAME}] {len(matched)} matched criteria")

        seen = load_seen()
        new_matches = [
            d
            for d in matched
            if d.get("accession_number") and d["accession_number"] not in seen
        ]
        print(f"[{MONITOR_NAME}] {len(new_matches)} are new since last run")

        subject, html = format_email(
            new_matches,
            total_filings_searched=len(documents),
            total_matches_this_period=len(matched),
            lookback_days=lookback_days,
            config=config,
        )
        print(f"[{MONITOR_NAME}] sending email: {subject}")
        notify.send_email(to=recipient, subject=subject, html=html)

        # Update seen-set with everything we matched (not just new),
        # so partial overlaps in lookback periods don't re-trigger.
        seen.update(
            d["accession_number"] for d in matched if d.get("accession_number")
        )
        save_seen(seen)
        print(f"[{MONITOR_NAME}] seen-set now has {len(seen)} accession numbers")
        return 0

    except Exception:  # noqa: BLE001
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        notify.send_failure_alert(recipient, MONITOR_NAME, tb)
        return 1


if __name__ == "__main__":
    sys.exit(main())
