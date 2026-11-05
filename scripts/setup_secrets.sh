#!/usr/bin/env bash
# Store the job agent's secrets in this GitHub repository (needs the gh CLI, logged in).
#
#   bash scripts/setup_secrets.sh ~/.jobagent/profile.yaml
#
# It validates the profile, creates the state encryption key, and asks for your
# Claude subscription token (from `claude setup-token`) and, optionally, a Gmail
# app password. Nothing is written to the repository itself.
set -euo pipefail

PROFILE="${1:-$HOME/.jobagent/profile.yaml}"
if [ ! -f "$PROFILE" ]; then
  echo "Profile not found: $PROFILE (copy examples/profile.example.yaml and fill it in)" >&2
  exit 1
fi
command -v gh > /dev/null || { echo "Install the GitHub CLI first: https://cli.github.com" >&2; exit 1; }

echo "1/4 Checking the profile..."
JOBAGENT_PROFILE="$PROFILE" python -m job_agent check | grep -E '"profile"'
JOBAGENT_PROFILE="$PROFILE" python -m job_agent check | grep -q '"profile": "ok"' || { echo "Fix the profile first." >&2; exit 1; }
gh secret set JOBAGENT_PROFILE_YAML < "$PROFILE"

echo "2/4 State encryption key..."
if gh secret list | grep -q '^JOBAGENT_STATE_KEY'; then
  echo "    already set, keeping it (a new key would make the saved history unreadable)"
else
  python -m job_agent keygen | gh secret set JOBAGENT_STATE_KEY
fi

echo "3/4 Claude subscription token: run 'claude setup-token' in another terminal and paste the token."
read -r -s -p "    token (sk-ant-oat...): " TOKEN; echo
if [ -n "$TOKEN" ]; then printf '%s' "$TOKEN" | gh secret set CLAUDE_CODE_OAUTH_TOKEN; fi

echo "4/4 Optional: Gmail address + app password for verification codes and reply tracking (Enter to skip)."
read -r -p "    Gmail address: " IMAP_USER
if [ -n "$IMAP_USER" ]; then
  read -r -s -p "    app password: " IMAP_PASSWORD; echo
  printf '%s' "$IMAP_USER" | gh secret set JOBAGENT_IMAP_USER
  printf '%s' "$IMAP_PASSWORD" | gh secret set JOBAGENT_IMAP_PASSWORD
fi

echo "Done. Start a rehearsal: Actions > job-hunt > Run workflow (dry_run checked), or:"
echo "    gh workflow run job-hunt.yml -f dry_run=true"
