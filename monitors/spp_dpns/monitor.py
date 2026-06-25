name: SPP DPNS Monitor

# Weekly check, Mondays at 10:00 AM Eastern. Fetches BOTH 2025 and 2026
# (configured in the YEARS list inside monitor.py) and sends ONE combined
# email with a section per year. Always emails (heartbeat).
#
# DST handling: during EDT (UTC-4), 10am ET = 14:00 UTC. During EST (UTC-5),
# 10am ET = 15:00 UTC. Change "14" to "15" when DST flips back in November.
#
# To change which years are watched, edit the YEARS list at the top of
# monitors/spp_dpns/monitor.py (no workflow change needed).

on:
  schedule:
    - cron: "0 14 * * 1"   # 10:00 AM EDT every Monday (change to 15 in winter)
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
        run: python monitors/spp_dpns/monitor.py

      - name: Commit updated seen-sets
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          for f in monitors/spp_dpns/snapshots/seen_*.json; do
            [ -f "$f" ] && git add "$f"
          done
          if git diff --cached --quiet; then
            echo "No seen-set changes to commit."
          else
            git commit -m "spp_dpns: seen-set update"
            for attempt in 1 2 3; do
              if git push; then break; fi
              echo "Push failed (attempt $attempt) — pulling and retrying"
              git pull --rebase origin main
              [ "$attempt" = "3" ] && exit 1
              sleep 2
            done
          fi
