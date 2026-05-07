# Queue Monitors

Automated monitors for energy-market interconnection queues and similar
public data sources. Each monitor runs on a schedule via GitHub Actions,
fetches a source document, diffs it against the previous snapshot, and
emails a summary of what changed.

## Repository layout

```
queue-monitors/
├── monitors/
│   └── miso_eras/
│       ├── monitor.py          # entry point for this monitor
│       └── snapshots/          # committed snapshots (history via git)
├── lib/
│   ├── fetch.py                # HTTP fetch + link discovery
│   ├── diff.py                 # snapshot diffing
│   └── notify.py               # email via Resend (swap providers here)
├── .github/workflows/
│   └── miso_eras.yml           # cron schedule + run steps
└── requirements.txt
```

The `lib/` modules are shared. New monitors live as a sibling under
`monitors/` and reuse the same library.

## First-time setup

### 1. Create the GitHub repository

Push this directory to a new (private) GitHub repo:

```bash
cd queue-monitors
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin git@github.com:YOUR_USERNAME/queue-monitors.git
git push -u origin main
```

### 2. Create a Resend account and API key

1. Sign up at https://resend.com (free tier is plenty for this).
2. Go to **API Keys** → **Create API Key**. Give it sending permission.
3. Copy the key. You'll only see it once.

### 3. Add GitHub Actions secrets

In your repo on GitHub, go to **Settings → Secrets and variables → Actions
→ New repository secret** and add:

| Name | Value |
|------|-------|
| `RESEND_API_KEY` | The API key from step 2 |
| `MONITOR_TO_ADDR` | The email address that should receive summaries |
| `MONITOR_FROM_ADDR` | *(optional)* `Your Name <onboarding@resend.dev>` — leave unset to use the default |

> **Note on the From address:** Resend's free tier lets you send from
> `onboarding@resend.dev` without owning a domain. If you'd rather emails
> come from `monitor@yourdomain.com`, verify your domain in the Resend
> dashboard and set `MONITOR_FROM_ADDR` accordingly.

### 4. Test it manually

Before waiting for the cron schedule, kick off a manual run:

1. In GitHub, go to **Actions → MISO ERAS Monitor → Run workflow**.
2. Watch the logs. The first run will produce an "initial snapshot"
   email (no diff to compute yet).
3. Check your inbox — including spam, since the first email from a new
   sender often gets routed there. Mark it "not spam" if so.

After the first successful run, the next scheduled run will produce a
real diff.

### 5. Schedule

The workflow is configured to run at 9am and 5pm Eastern Time, every
day. The cron file lists multiple UTC times and the script gates
execution on the actual Eastern hour, which makes DST a non-issue.

## How it works

1. `monitor.py` scrapes the MISO landing page to find the current
   `.xlsx` URL (the filename includes a numeric ID and `?v=` timestamp
   that change with each republish).
2. It downloads the spreadsheet and parses it with `openpyxl`.
3. It loads the previous snapshot from `snapshots/latest.json` and
   computes a diff: added projects, removed projects, and any
   row-level field changes.
4. It formats an HTML email and sends it via Resend.
5. It writes the new snapshot to `snapshots/latest.json`.
6. The workflow commits and pushes the updated snapshot back to the
   repo, giving you full git history of the queue over time.

## Running locally

To test or debug without waiting for the workflow:

```bash
python -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\activate
pip install -r requirements.txt

export RESEND_API_KEY="re_..."
export MONITOR_TO_ADDR="you@example.com"

python monitors/miso_eras/monitor.py
```

The first run creates `snapshots/latest.json`. Run it again and you'll
get a "no changes since last check" email — confirming the diff logic.

## Adding a new monitor

The pattern is deliberately simple. To monitor another site:

1. Create `monitors/<name>/monitor.py`. Use `monitors/miso_eras/monitor.py`
   as a template.
2. Update the source-specific bits: the URL discovery logic, the parsing
   function (CSV? PDF? HTML table?), and the `summarize()` counts.
3. Reuse `lib.fetch`, `lib.diff`, and `lib.notify` as-is.
4. Add a workflow file at `.github/workflows/<name>.yml` modeled on
   `miso_eras.yml`. Adjust the cron schedule.
5. Push. GitHub Actions will pick up the new workflow automatically.

The shared modules are intentionally minimal so they don't lock you into
assumptions. If a new source needs a fundamentally different shape (e.g.
no stable IDs, only deltas), add a new helper to `lib/` rather than
contorting the existing one.

## Failure handling

If a monitor errors out (MISO redesigns their page, the xlsx parser
hits an unexpected schema change, the network is down), the script
catches the exception and sends a `[Monitor FAILED]` email with the
traceback. You'll know within minutes that something needs fixing.
The snapshot is *not* overwritten on failure, so the next successful
run still has a valid baseline to diff against.

## Cost

- **GitHub Actions:** 2,000 free minutes/month for private repos. Each
  run takes ~30 seconds, so 4 runs/day × 30 days × ~0.5 min ≈ 60 min/month.
  Well under the limit.
- **Resend:** 3,000 emails/month free. You'll send ~60.
- **Total:** $0.
