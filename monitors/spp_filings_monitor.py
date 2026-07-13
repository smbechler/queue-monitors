"""SPP Documents & Filings monitor.

Watches an SPP Documents & Filings folder page (identified by its `id`) and
emails daily. Shows all document entries in that folder (Title linked) and
flags newly-appeared ones since the last check.

Unlike the MISO filings page, this SPP page is SERVER-RENDERED HTML — the
document list is embedded directly in the page, so there's no API to call.
We fetch the page and parse the document entries out of the HTML.

The page has two entry types, both tracked:
  - Docket folders:  <div class="doc doc-folder"><a href="?id=NNN">TITLE</a></div>
  - PDF documents:   <div class="doc doc-pdf"><a href="/Documents/...">TITLE</a>
                      ...<small>DATE</small>...<small>DOCKET #: XXX</small></div>

Each item shows Title (linked). PDFs additionally carry a date and docket
number, which we capture when present.

Runs daily. Always emails (heartbeat), noting new items or "none new".
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import traceback
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from lib import fetch, notify  # noqa: E402

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MONITOR_NAME = "SPP Documents & Filings"

# The folder to watch. The `id` identifies the folder (here: 2026 FERC Dockets).
# If you want to watch a different folder later, change FOLDER_ID to that
# folder's id (visible in the page URL when you navigate to it).
FOLDER_ID = "544714"

BASE_URL = "https://www.spp.org/spp-documents-filings/"
PAGE_URL = f"{BASE_URL}?id={FOLDER_ID}"

HERE = Path(__file__).resolve().parent
SNAPSHOT_DIR = HERE / "snapshots"
SEEN_FILE = SNAPSHOT_DIR / f"seen_{FOLDER_ID}.json"
WORKDIR = HERE / "_workdir"


# ---------------------------------------------------------------------------
# HTML parsing (stdlib html.parser — no external dependency)
# ---------------------------------------------------------------------------


class _DocListParser(HTMLParser):
    """Extract document entries from the SPP filings page.

    We look for <div class="doc ..."> blocks that live inside the
    <div class="...spp-docs"> container, and pull the first <a> in each
    (its href + text), plus any <small> date / docket lines for PDFs.
    """

    def __init__(self) -> None:
        super().__init__()
        self._in_docs_container = False
        self._docs_container_depth = 0
        self._div_depth = 0

        # Current doc entry being built
        self._in_doc = False
        self._doc_div_depth = 0
        self._current_kind: str | None = None
        self._in_anchor = False
        self._captured_anchor = False  # only take the FIRST anchor per entry
        self._current_href: str | None = None
        self._current_title_parts: list[str] = []
        self._in_small = False
        self._current_smalls: list[str] = []

        self.entries: list[dict[str, str]] = []

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _classes(attrs: list[tuple[str, str | None]]) -> list[str]:
        for name, value in attrs:
            if name == "class" and value:
                return value.split()
        return []

    # -- tag handlers -------------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "div":
            self._div_depth += 1
            classes = self._classes(attrs)

            # Enter the docs container
            if "spp-docs" in classes and not self._in_docs_container:
                self._in_docs_container = True
                self._docs_container_depth = self._div_depth
                return

            # A document entry: <div class="doc doc-folder|doc-pdf|...">
            if self._in_docs_container and "doc" in classes and not self._in_doc:
                self._in_doc = True
                self._doc_div_depth = self._div_depth
                self._captured_anchor = False
                self._current_href = None
                self._current_title_parts = []
                self._current_smalls = []
                if "doc-folder" in classes:
                    self._current_kind = "folder"
                elif "doc-pdf" in classes:
                    self._current_kind = "pdf"
                else:
                    self._current_kind = "other"
                return

        if self._in_doc:
            if tag == "a" and not self._captured_anchor:
                self._in_anchor = True
                for name, value in attrs:
                    if name == "href":
                        self._current_href = value
            elif tag == "small":
                self._in_small = True
                self._current_small_buf = ""

    def handle_endtag(self, tag: str) -> None:
        if self._in_doc:
            if tag == "a" and self._in_anchor:
                self._in_anchor = False
                self._captured_anchor = True
            elif tag == "small" and self._in_small:
                self._in_small = False
                text = getattr(self, "_current_small_buf", "").strip()
                if text:
                    self._current_smalls.append(text)

        if tag == "div":
            # Closing a doc entry?
            if self._in_doc and self._div_depth == self._doc_div_depth:
                self._finalize_entry()
                self._in_doc = False
                self._current_kind = None

            # Leaving the docs container?
            if self._in_docs_container and self._div_depth == self._docs_container_depth:
                self._in_docs_container = False

            self._div_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._in_doc and self._in_anchor and not self._captured_anchor:
            self._current_title_parts.append(data)
        elif self._in_doc and self._in_small:
            self._current_small_buf = getattr(self, "_current_small_buf", "") + data

    # -- entry assembly -----------------------------------------------------
    def _finalize_entry(self) -> None:
        href = (self._current_href or "").strip()
        title = html.unescape("".join(self._current_title_parts)).strip()
        title = re.sub(r"\s+", " ", title)
        if not href or not title:
            return

        # Normalize URL to absolute
        if href.startswith("?"):
            url = f"{BASE_URL}{href}"
        elif href.startswith("/"):
            url = f"https://www.spp.org{href}"
        elif href.startswith("http"):
            url = href
        else:
            url = f"{BASE_URL}{href}"

        # For PDFs, the <small> lines are date then "DOCKET #: XXX"
        date_str = ""
        docket = ""
        for s in self._current_smalls:
            s_clean = html.unescape(s).strip()
            if s_clean.upper().startswith("DOCKET"):
                docket = s_clean.split(":", 1)[-1].strip()
            elif s_clean:
                # First non-docket small is the date
                if not date_str:
                    date_str = s_clean

        # Stable id: prefer the ?id=NNN for folders; for PDFs use the doc path.
        m = re.search(r"[?&]id=(\d+)", url)
        if m:
            stable_id = f"id:{m.group(1)}"
        else:
            stable_id = f"url:{url}"

        self.entries.append(
            {
                "id": stable_id,
                "title": title,
                "url": url,
                "kind": self._current_kind or "other",
                "date": date_str,
                "docket": docket,
            }
        )


def parse_documents(page_html: str) -> list[dict[str, str]]:
    parser = _DocListParser()
    parser.feed(page_html)
    # De-duplicate by id, preserving order
    seen_ids: set[str] = set()
    unique: list[dict[str, str]] = []
    for e in parser.entries:
        if e["id"] in seen_ids:
            continue
        seen_ids.add(e["id"])
        unique.append(e)
    return unique


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


def fetch_documents() -> list[dict[str, str]]:
    resp = fetch.get(PAGE_URL)
    return parse_documents(resp.text)


# ---------------------------------------------------------------------------
# Seen-set I/O
# ---------------------------------------------------------------------------


def load_seen() -> set[str]:
    if not SEEN_FILE.exists():
        return set()
    try:
        with SEEN_FILE.open("r") as fh:
            return set(json.load(fh).get("doc_ids", []))
    except Exception:  # noqa: BLE001
        return set()


def save_seen(doc_ids: set[str]) -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "folder_id": FOLDER_ID,
        "count": len(doc_ids),
        "doc_ids": sorted(doc_ids),
    }
    with SEEN_FILE.open("w") as fh:
        json.dump(payload, fh, indent=2)


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------


def format_email(
    docs: list[dict[str, str]],
    new_docs: list[dict[str, str]],
) -> tuple[str, str]:
    new_count = len(new_docs)
    if new_count > 0:
        subject = (
            f"SPP Filings — {new_count} new "
            f"filing{'s' if new_count != 1 else ''}"
        )
    else:
        subject = "SPP Filings — no new filings"

    parts: list[str] = []
    parts.append(
        "<div style='font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;"
        "max-width:680px;color:#222;'>"
    )
    parts.append("<h2 style='margin-bottom:4px;'>SPP Documents &amp; Filings Update</h2>")
    parts.append(
        f"<p style='color:#666;margin-top:0;font-size:13px;'>"
        f"Checked at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · "
        f"{len(docs)} filing{'s' if len(docs) != 1 else ''} in this folder · "
        f"<strong>{new_count} new since last check</strong>"
        f"</p>"
    )

    if new_docs:
        parts.append("<h3>New filings</h3>")
        parts.append(_doc_list(new_docs, highlight=True))

    parts.append("<h3 style='margin-top:20px;'>All filings</h3>")
    if docs:
        parts.append(_doc_list(docs, highlight=False))
    else:
        parts.append(
            "<p>No filings found in this folder. This email confirms the "
            "monitor is running. (If the SPP page shows filings but this is "
            "empty, the page structure may have changed — check the run log.)</p>"
        )

    parts.append(
        f"<p style='margin-top:24px;font-size:13px;'>"
        f"<a href='{PAGE_URL}'>SPP Documents &amp; Filings folder</a></p>"
    )
    parts.append("</div>")
    return subject, "".join(parts)


def _doc_list(docs: list[dict[str, str]], highlight: bool) -> str:
    bg = "background:#fff8d4;" if highlight else ""
    items = []
    for d in docs:
        # Build an optional trailing meta line for PDFs (date / docket)
        meta_bits = []
        if d.get("date"):
            meta_bits.append(_esc(d["date"]))
        if d.get("docket"):
            meta_bits.append("Docket " + _esc(d["docket"]))
        meta = ""
        if meta_bits:
            meta = (
                f"<br><span style='font-size:12px;color:#666;'>"
                f"{' · '.join(meta_bits)}</span>"
            )
        icon = "📄 " if d.get("kind") == "pdf" else ""
        items.append(
            f"<li style='margin-bottom:8px;{bg}padding:4px 6px;border-radius:3px;'>"
            f"{icon}<a href='{_esc(d['url'])}' style='font-weight:600;'>{_esc(d['title'])}</a>"
            f"{meta}"
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

        print(f"[{MONITOR_NAME}] fetching {PAGE_URL}…")
        docs = fetch_documents()
        print(f"[{MONITOR_NAME}] parsed {len(docs)} document entries")
        # Log a few for sanity
        for d in docs[:5]:
            print(f"[{MONITOR_NAME}]   [{d['kind']}] {d['title']}")
        if len(docs) > 5:
            print(f"[{MONITOR_NAME}]   …and {len(docs) - 5} more")

        seen = load_seen()
        new_docs = [d for d in docs if d["id"] not in seen]
        print(f"[{MONITOR_NAME}] {len(new_docs)} new since last run")

        subject, html_body = format_email(docs, new_docs)
        print(f"[{MONITOR_NAME}] sending email: {subject}")
        notify.send_email(to=recipient, subject=subject, html=html_body)

        seen.update(d["id"] for d in docs)
        save_seen(seen)
        print(f"[{MONITOR_NAME}] seen-set updated ({len(seen)} ids)")
        return 0

    except Exception:  # noqa: BLE001
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        notify.send_failure_alert(recipient, MONITOR_NAME, tb)
        return 1


if __name__ == "__main__":
    sys.exit(main())
