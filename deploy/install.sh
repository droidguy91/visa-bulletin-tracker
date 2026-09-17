#!/usr/bin/env bash
#
# Install the Visa Bulletin tracker as a systemd user timer on Linux
# (Ubuntu, Raspberry Pi OS, anything with systemd).
#
#   curl -fsSL https://raw.githubusercontent.com/droidguy91/visa-bulletin-tracker/main/deploy/install.sh | bash
#
# or, from a checkout:  ./deploy/install.sh
#
# Runs entirely as your user -- no root, except an optional `enable-linger`
# so the timer fires when you are not logged in.
set -euo pipefail

REPO_URL="${VBT_REPO_URL:-https://github.com/droidguy91/visa-bulletin-tracker.git}"
BASE="$HOME/.local/share/visa-bulletin"
REPO="$BASE/repo"
VENV="$BASE/venv"
UNITS="$HOME/.config/systemd/user"
CREDS="$HOME/.config/visa-bulletin/git-credentials"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# --- prerequisites -----------------------------------------------------

command -v git     >/dev/null || die "git is not installed"
command -v python3 >/dev/null || die "python3 is not installed"
command -v systemctl >/dev/null || die "systemd not found -- use cron instead (see README)"
python3 -c 'import venv' 2>/dev/null || die "python3-venv is missing (apt install python3-venv)"

# --- step 1: can this machine even reach the source? -------------------
#
# This is the whole reason the tracker is being moved off GitHub Actions:
# Cloudflare serves a challenge page to datacentre IPs. Find out now,
# before installing anything.

say "Checking whether this machine can reach travel.state.gov"
UA="visa-bulletin-tracker/0.1 (personal priority-date tracker; +$REPO_URL)"
CODE=$(curl -sS -o /tmp/vbt-probe.$$ -w '%{http_code}' --max-time 30 \
        -A "$UA" https://travel.state.gov/robots.txt || echo "000")

if grep -qi 'Attention Required\|cf-browser-verification\|cdn-cgi/challenge' /tmp/vbt-probe.$$ 2>/dev/null; then
  rm -f /tmp/vbt-probe.$$
  die "Cloudflare is challenging this machine too (HTTP $CODE).

This host is no better than a GitHub runner for fetching the bulletin.
Do NOT try to work around the challenge -- that is bot-detection evasion.
Use the community-mirror data source instead (see README), or try a
different network."
fi
rm -f /tmp/vbt-probe.$$

case "$CODE" in
  404) echo "  robots.txt: 404 -- no crawl restrictions published. Good." ;;
  200) echo "  robots.txt: 200 -- a real robots.txt exists; the fetcher will honour it." ;;
  000) die "could not reach travel.state.gov at all -- check this machine's network" ;;
  *)   warn "robots.txt returned HTTP $CODE. The fetcher honours 401/403 as a full disallow, so it may refuse to run." ;;
esac

# --- step 2: code ------------------------------------------------------

say "Installing to $BASE"
mkdir -p "$BASE" "$UNITS" "$(dirname "$CREDS")"

if [ -d "$REPO/.git" ]; then
  echo "  updating existing checkout"
  git -C "$REPO" pull --rebase --quiet origin main
else
  echo "  cloning $REPO_URL"
  git clone --quiet "$REPO_URL" "$REPO"
fi

say "Creating virtualenv"
[ -d "$VENV" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$REPO/requirements.txt"

install -m 0755 "$REPO/deploy/sync.sh" "$BASE/sync.sh"

# --- step 3: push credentials -----------------------------------------

say "GitHub push credentials"
if git -C "$REPO" ls-remote --exit-code origin >/dev/null 2>&1 && \
   git -C "$REPO" config --get credential.helper >/dev/null 2>&1; then
  echo "  existing credentials work; leaving them alone"
else
  echo "  This machine needs to push data commits back to GitHub."
  echo "  Paste a token with 'Contents: read and write' on this repo."
  echo "  (Input is hidden. Stored at $CREDS, mode 600.)"
  printf '  Token: '
  read -rs TOKEN
  echo
  [ -n "$TOKEN" ] || die "no token given"
  umask 077
  printf 'https://x-access-token:%s@github.com\n' "$TOKEN" > "$CREDS"
  chmod 600 "$CREDS"
  git -C "$REPO" config credential.helper "store --file=$CREDS"
  unset TOKEN
  git -C "$REPO" ls-remote --exit-code origin >/dev/null 2>&1 \
    || die "those credentials did not work"
  echo "  credentials verified"
fi

# --- step 4: timer -----------------------------------------------------

say "Installing systemd user units"
install -m 0644 "$REPO/deploy/visa-bulletin.service" "$UNITS/visa-bulletin.service"
install -m 0644 "$REPO/deploy/visa-bulletin.timer"   "$UNITS/visa-bulletin.timer"
systemctl --user daemon-reload
systemctl --user enable --now visa-bulletin.timer

if ! loginctl show-user "$USER" 2>/dev/null | grep -q 'Linger=yes'; then
  echo
  echo "  To run when you are not logged in:  sudo loginctl enable-linger $USER"
fi

# --- step 5: prove it works -------------------------------------------

say "Running one poll now"
"$BASE/sync.sh" || warn "the poll exited non-zero -- see $REPO/data/last_run.json"

say "Done"
echo "  Schedule:  systemctl --user list-timers visa-bulletin.timer"
echo "  Logs:      journalctl --user -u visa-bulletin.service -n 50"
echo "  Status:    cat $REPO/data/last_run.json"
echo "  Backfill:  PYTHONPATH=$REPO/src $VENV/bin/python -m vbt.cli backfill --limit 55"
