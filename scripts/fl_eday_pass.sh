#!/usr/bin/env bash
# One pass of the Election-Day Florida loop (fl_eday.yml, 5 seconds past each 10-minute mark): run Florida and
# commit at once. Broward's dashboard stalls right after the marks (11/3), so fl_update gives it 20 seconds; if it
# wasn't back, a second pass waits up to 100 seconds for it and commits Broward a minute or two later. The loop
# re-reads this file after its git pull each pass, so changes here apply without restarting the loop.

commit_push() {
  git add data/fl data/_health.json
  if git diff --cached --quiet; then
    echo "No Florida change."
    return
  fi
  git commit --quiet -m "Florida turnout $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
  for try in 1 2 3; do
    # another commit landed first: a file it also changed keeps its copy (only _health.json can clash)
    git pull --quiet --rebase -X ours --autostash origin "${GITHUB_REF_NAME:-main}" || { git rebase --abort || true; }
    git push --quiet && { echo "pushed $(date -u +%H:%M:%S)"; return; }
    sleep 3
  done
}

out=$(python scripts/run_all_states.py --only fl 2>&1)
echo "$out"
commit_push
if echo "$out" | grep -q "Broward dashboard not back"; then
  echo "== Broward second pass $(date -u +%H:%M:%S)"
  FL_BROWARD_WAIT=100 python scripts/run_all_states.py --only fl 2>&1 | grep -E "Broward|CHANGED|NOCHANGE|state run"
  commit_push
fi
