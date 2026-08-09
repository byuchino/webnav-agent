#!/usr/bin/env bash
# Batch-harvest the CCFA recommended reading. Runs unattended; expect an hour or more.
#
# Prerequisites, both of which fail silently if missing:
#   - a Chrome signed in to docs.crowdstrike.com, reachable on $CDP_PORT
#     (on the Windows box: ssh -N -L 9333:127.0.0.1:9222 gaming-pc)
#   - that SSO session staying valid for the duration. docs_fetch aborts a book if it
#     starts seeing the sign-in page, so a mid-batch expiry costs the rest of that book,
#     not a directory full of login pages.
#
# The books are those named in the CCFA Certification Guide's Recommended Reading, plus
# Audit Logs for objective 7.2.
set -u
cd "$(dirname "$0")/.."

export CDP_PORT="${CDP_PORT:-9333}"
export DOCS_DELAY="${DOCS_DELAY:-2.5}"
OUTDIR="${DOCS_OUT:-$HOME/falcon-docs}"
LOG="$OUTDIR/harvest.log"
mkdir -p "$OUTDIR"

run() {  # label url limit subdir
  echo "" | tee -a "$LOG"
  echo "=== $1  [$(date '+%H:%M:%S')] ===" | tee -a "$LOG"
  timeout 5400 ./.venv/bin/python tools/docs_fetch.py book "$2" \
      --subdir "$4" --limit "$3" 2>&1 | grep -v 'cdp_use' | tee -a "$LOG"
}

echo "harvest started $(date)" | tee -a "$LOG"

# Falcon Management covers CCFA domains 1, 3, 4, 5 and 7 -- the bulk of the syllabus.
run "Falcon Management"  https://docs.crowdstrike.com/r/en-US/g6auvcg3  120 falcon-management
# Endpoint Security: Response, Configuration, Additional Features (domains 5, 6).
run "Endpoint Security"  https://docs.crowdstrike.com/r/en-US/a5kj6wfu  120 endpoint-security
# CrowdStrike APIs -- General Info (objective 1.3, manage API keys).
run "CrowdStrike APIs"   https://docs.crowdstrike.com/r/en-US/kgsgkjd3   60 crowdstrike-apis
# Audit logs (objective 7.2).
run "Audit Logs"         https://docs.crowdstrike.com/r/en-US/dpxel4ag   40 audit-logs
# The guide's "CrowdStrike Marketplace" entry.
run "CrowdStrike Store"  https://docs.crowdstrike.com/r/en-US/wlmfpr5u   40 crowdstrike-store

echo "" | tee -a "$LOG"
echo "harvest finished $(date)" | tee -a "$LOG"
du -sh "$OUTDIR"/* 2>/dev/null | tee -a "$LOG"
