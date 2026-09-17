#!/usr/bin/env bash
# Poll for a new bulletin, then push any data changes to GitHub.
# Installed to ~/.local/share/visa-bulletin/sync.sh by deploy/install.sh.
set -euo pipefail

REPO="${VBT_REPO:-$HOME/.local/share/visa-bulletin/repo}"
VENV="${VBT_VENV:-$HOME/.local/share/visa-bulletin/venv}"

cd "$REPO"

# Take any changes made elsewhere (e.g. a GitHub Actions run) before adding
# ours, so the data file never forks.
git pull --rebase --quiet origin main || {
  echo "warning: pull failed; continuing with local state" >&2
}

set +e
PYTHONPATH="$REPO/src" "$VENV/bin/python" -m vbt.cli poll
POLL_STATUS=$?
set -e

git add data/
if git diff --staged --quiet; then
  echo "No data changes."
else
  git -c user.name="visa-bulletin-bot" \
      -c user.email="bot@users.noreply.github.com" \
      commit -q -m "data: poll $(date -u +%Y-%m-%dT%H:%MZ)"
  git push --quiet origin main
  echo "Pushed data changes."
fi

exit $POLL_STATUS
