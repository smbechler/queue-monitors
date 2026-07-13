name: MISO Regulatory Filings Monitor

# Daily check at 5:00 PM Eastern. Always emails — showing all of the current
# month's FERC filings and flagging any new ones. The month auto-advances
# (the script computes the current month), so no config change is needed at
# month boundaries.
#
# DST handling: during EDT (UTC-4), 5pm ET = 21:00 UTC. During EST (UTC-5),
# 5pm ET = 22:00 UTC. Change "21" to "22" when DST flips back in November.

on:
  schedule:
    - cron: "0 21 * * *"   # 5:00 PM EDT daily (change to 22 in winter)
  workflow_dispatch:

permissions:
  contents: write

jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - name: Show timing context
        run: |
          echo "UTC time:     $(date -u)"
          echo "Eastern time: $(TZ='America/New_York' date)"
          echo "Trigger:      ${{ github.event_name }}"

      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Run monitor
        env:
          RESEND_API_KEY: ${{ secrets.RESEND_API_KEY }}
          MONITOR_TO_ADDR: ${{ secrets.MONITOR_TO_ADDR }}
          MONITOR_FROM_ADDR: ${{ secrets.MONITOR_FROM_ADDR }}
        run: python monitors/miso_filings/monitor.py

      - name: Commit updated seen-set
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          for f in monitors/miso_filings/snapshots/seen_*.json; do
            [ -f "$f" ] && git add "$f"
          done
          if git diff --cached --quiet; then
            echo "No seen-set changes to commit."
          else
            git commit -m "miso_filings: seen-set update"
            for attempt in 1 2 3; do
              if git push; then break; fi
              echo "Push failed (attempt $attempt) — pulling and retrying"
              git pull --rebase origin main
              [ "$attempt" = "3" ] && exit 1
              sleep 2
            done
          fi
