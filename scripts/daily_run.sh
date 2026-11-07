#!/usr/bin/env bash
# One day of job hunting, driven by Claude Code:
#   1. discover postings (and pick up boxes you ticked in earlier reports)
#   2. one Claude session scores today's candidates
#   3. one Claude session per job tailors the resume and applies (any site)
#   4. one Claude session reads the inbox
# Works in GitHub Actions and on your own computer (Claude Code uses your login).
#
#   bash scripts/daily_run.sh                      # real run
#   JOBAGENT_DRY_RUN=1 bash scripts/daily_run.sh   # rehearsal: nothing is submitted
#
# If Claude runs out of quota (subscription limit or API credit) or the token is
# rejected, it stops cleanly, keeps unfinished jobs for the next run, and the
# report says what happened.
set -uo pipefail

MAX_MINUTES="${JOBAGENT_MAX_MINUTES:-300}"      # stop starting new applications after this
APPLY_TIMEOUT="${JOBAGENT_APPLY_TIMEOUT:-15}"   # minutes allowed per application
APPLY_TURNS="${JOBAGENT_APPLY_TURNS:-80}"       # Claude turns allowed per application (a stuck form stops costing tokens)
LOG_DIR="${JOBAGENT_HOME:-$HOME/.jobagent}/logs"
mkdir -p "$LOG_DIR"
START=$(date +%s)
SKILL=".claude/skills/job-hunt/SKILL.md"
cd "$(dirname "$0")/.."

ARGS=(--permission-mode dontAsk --tools "Bash,Read,Write,Edit,Glob,Grep"
      --allowedTools "Bash(jobagent *)" Read Write Edit Glob Grep)

# Models: Sonnet for scoring, the inbox and most applications; Opus for the best matches
# (score >= JOBAGENT_TOP_SCORE), where the writing matters most. If Opus runs out of quota,
# the rest of the day continues on Sonnet. JOBAGENT_CLAUDE_MODEL, if set, is used for everything.
BASE_MODEL="${JOBAGENT_CLAUDE_MODEL:-sonnet}"
TOP_MODEL="${JOBAGENT_CLAUDE_MODEL:-${JOBAGENT_MODEL_TOP:-sonnet}}"  # set JOBAGENT_MODEL_TOP=opus for the best matches
TOP_SCORE="${JOBAGENT_TOP_SCORE:-85}"
TOP_OFF=""
DRY=""
if [ "${JOBAGENT_DRY_RUN:-}" = "1" ]; then
  DRY=" This is a DRY RUN: nothing may be submitted (use --dry-run with submit; finish browser applications with 'jobagent web finish dry_run')."
fi

STOP=""
classify() {  # quota | auth | "" from a failed Claude run's output
  if grep -qiE "usage limit|limit reached|hit your (usage |session |weekly )?limit|limit[^a-z]{1,4}resets|credit balance is too low|insufficient (credit|balance|quota)|exceeded your (current )?quota|billing" "$1"; then
    echo quota
  elif grep -qiE "invalid (api key|x-api-key|bearer token|token)|authentication_error|oauth token (has )?expired|token (has )?expired|please run /login|not logged in|invalid_grant|401 unauthorized" "$1"; then
    echo auth
  fi
}

TIMEOUT_BIN="$(command -v timeout || command -v gtimeout || true)"  # gtimeout: macOS with coreutils

run_claude() {  # run_claude <log name> <max turns> <timeout minutes> <model> <prompt>
  local log="$LOG_DIR/$1.log"
  if [ -n "$TIMEOUT_BIN" ]; then
    "$TIMEOUT_BIN" "${3}m" claude -p "$5$DRY" --max-turns "$2" --model "$4" "${ARGS[@]}" < /dev/null > "$log" 2>&1
  else
    claude -p "$5$DRY" --max-turns "$2" --model "$4" "${ARGS[@]}" < /dev/null > "$log" 2>&1
  fi
  local status=$?
  if [ $status -ne 0 ]; then
    case "$(classify "$log")" in
      quota)
        STOP=quota
        jobagent set-run quota_exhausted "$(grep -oiE '(usage limit|limit reached|hit your [a-z ]*limit|credit balance)[^|]{0,100}' "$log" | head -1)" > /dev/null
        ;;
      auth)
        STOP=auth
        jobagent set-run auth_error "token rejected" > /dev/null
        ;;
    esac
  fi
  echo "[$1] $4: exit $status ${STOP:+($STOP)}"
  return $status
}

if [ -n "${JOBAGENT_ONLY_JOB:-}" ]; then
  # One job only (e.g. to try the agent out): no discovery, scoring or inbox.
  score=$(jobagent queue --scores | awk -v id="$JOBAGENT_ONLY_JOB" '$1 == id {print $2}')
  model="$BASE_MODEL"
  if [ "${score:-0}" -ge "$TOP_SCORE" ]; then model="$TOP_MODEL"; fi
  run_claude "apply-1" "$APPLY_TURNS" "$APPLY_TIMEOUT" "$model" "Mode APPLY of the job-hunt skill ($SKILL) for job $JOBAGENT_ONLY_JOB: read the skill first, then tailor the resume and apply."
  status=$?
  jobagent settle "$JOBAGENT_ONLY_JOB" --stopped "$STOP" --exit "$status" > /dev/null
  jobagent web close > /dev/null 2>&1 || true
  exit 0
fi

jobagent sync-checklist > /dev/null || true
jobagent discover > "$LOG_DIR/discover.json" || echo "::warning::discovery failed"
if [ -n "${JOBAGENT_RUN_NOTE:-}" ]; then jobagent note "$JOBAGENT_RUN_NOTE" > /dev/null; fi

if ! jobagent check | grep -q '"profile": "ok"'; then
  echo "No valid profile: discovery only."
  exit 0
fi

# By default nothing is scored first (it costs tokens): every candidate goes straight to the
# queue and its APPLY session checks the hard blockers while tailoring the resume.
# Set the repository variable JOBAGENT_SCORE=1 to score candidates in a separate pass first.
if [ "${JOBAGENT_SCORE:-0}" != "1" ]; then
  jobagent enqueue > /dev/null
fi
BATCH="${JOBAGENT_SCORE_BATCH:-40}"
for round in 1 2 3 4 5 6 7 8; do
  left=$(jobagent candidates | grep -c '"id"' || true)
  if [ "$left" -eq 0 ] || [ -n "$STOP" ]; then break; fi
  run_claude "score-$round" $((BATCH * 3 + 20)) 60 "$BASE_MODEL" "Mode SCORE of the job-hunt skill ($SKILL): read the skill first, then score at most $BATCH of the remaining candidates (best-ranked first) and stop."
  if [ "$(jobagent candidates | grep -c '"id"' || true)" -ge "$left" ]; then break; fi  # no progress: do not loop
done

if [ -z "$STOP" ]; then
  n=0
  while read -r id score; do
    n=$((n + 1))
    elapsed=$(( ($(date +%s) - START) / 60 ))
    if [ "$elapsed" -ge "$MAX_MINUTES" ]; then
      jobagent note "⏱️ 今天的运行时间用完了，剩下的岗位下次继续。" > /dev/null
      break
    fi
    name="apply-$n"  # numbered, not named: Actions logs of public repositories are public
    prompt="Mode APPLY of the job-hunt skill ($SKILL) for job $id: read the skill first, then tailor the resume and apply."
    model="$BASE_MODEL"
    if [ -z "$TOP_OFF" ] && [ "${score:-0}" -ge "$TOP_SCORE" ]; then model="$TOP_MODEL"; fi
    run_claude "$name" "$APPLY_TURNS" "$APPLY_TIMEOUT" "$model" "$prompt"
    status=$?
    if [ "$STOP" = quota ] && [ "$model" != "$BASE_MODEL" ]; then
      # The stronger model's limit ran out: the rest of the day continues on the base model.
      TOP_OFF=1
      STOP=""
      jobagent set-run quota_exhausted "" > /dev/null
      jobagent note "⚡ $model 的额度用完了，之后的岗位改用 $BASE_MODEL 继续投递。" > /dev/null
      jobagent web close > /dev/null 2>&1 || true
      if jobagent settle "$id" --stopped quota --exit "$status" | grep -q '"status": "scored"'; then
        run_claude "$name-retry" "$APPLY_TURNS" "$APPLY_TIMEOUT" "$BASE_MODEL" "$prompt"
        status=$?
      fi
    fi
    jobagent settle "$id" --stopped "$STOP" --exit "$status" > /dev/null
    jobagent web close > /dev/null 2>&1 || true
    if [ -n "$STOP" ]; then break; fi
  done < <(jobagent queue --scores)
fi

if [ -z "$STOP" ]; then
  run_claude inbox 60 15 "$BASE_MODEL" "Mode INBOX of the job-hunt skill ($SKILL): read the skill first, then check the inbox and add notes."
fi
exit 0
