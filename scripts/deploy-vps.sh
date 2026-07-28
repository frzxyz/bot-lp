#!/usr/bin/env bash
#
# Deploy a new revision onto a running LP bot.
#
# The bot holds funds and its cron jobs fire every minute, so this is written as
# an interruption rather than an install: halt, prove the new revision, and only
# then hand control back. Any failed gate leaves the bot halted — never running
# unverified code against real money.
#
# Resuming is opt-in (--resume). The default finishes halted and prints the one
# command that restarts trading, so a human decides when capital moves again.
#
#   ./scripts/deploy-vps.sh --token 0xCANDIDATE
#   ./scripts/deploy-vps.sh --token 0xCANDIDATE --resume
#
set -euo pipefail

BRANCH="claude/bot-lp-strategy-analysis-vc6tf6"
REPO="${RH_REPO:-/root/money-printer-repos/robinhood-lp-bot}"
STATE="${RH_STATE:-/root/.hermes/state/rh_meme_lp}"
HALT="${RH_HALT:-/root/.hermes/state/rh_meme_lp_halt}"
LOCK="$STATE/wallet.lock"
LOCK_WAIT="${RH_LOCK_WAIT:-420}"
TOKEN=""
RESUME=0
SKIP_CONTRACTS=0
PRE_HALTED=0
ROLLBACK_REF=""

while [ $# -gt 0 ]; do
  case "$1" in
    --branch) BRANCH="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    --token) TOKEN="$2"; shift 2 ;;
    --resume) RESUME=1; shift ;;
    --skip-contracts) SKIP_CONTRACTS=1; shift ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
info() { printf '   %s\n' "$*"; }
die()  { printf '\n\033[31mFAILED: %s\033[0m\n' "$*" >&2; exit 1; }

on_exit() {
  local code=$?
  [ "$code" -eq 0 ] && return 0
  printf '\n\033[31m─── deploy aborted ───\033[0m\n' >&2
  if [ -e "$HALT" ]; then
    printf 'The bot is HALTED and will not trade. Nothing was resumed.\n' >&2
  fi
  [ -n "$ROLLBACK_REF" ] && printf 'Roll back with:  git -C %s checkout %s\n' "$REPO" "$ROLLBACK_REF" >&2
  return "$code"
}
trap on_exit EXIT

# ── preflight ────────────────────────────────────────────────────────────────
say "Preflight"
[ -d "$REPO/.git" ] || die "no git repository at $REPO (pass --repo)"
[ -d "$STATE" ] || die "no state directory at $STATE (pass RH_STATE)"
command -v git >/dev/null || die "git not found"
command -v npm >/dev/null || die "npm not found"
command -v python3 >/dev/null || die "python3 not found"
info "repo   $REPO"
info "state  $STATE"
info "branch $BRANCH"

# An operator may already have halted the bot for their own reasons. Remember
# that, so a --resume here never silently overrides someone else's decision.
if [ -e "$HALT" ]; then
  PRE_HALTED=1
  info "kill switch was ALREADY armed before this run; it will be left armed"
fi

# ── halt and quiesce ─────────────────────────────────────────────────────────
say "Halting strategy operations"
touch "$HALT"
info "armed $HALT"

if command -v flock >/dev/null && [ -e "$LOCK" ]; then
  info "waiting up to ${LOCK_WAIT}s for the wallet lock (an in-flight tick)"
  # Acquiring the same lock the bot uses proves no tick is mid-transaction.
  flock -w "$LOCK_WAIT" "$LOCK" true \
    || die "wallet lock still held after ${LOCK_WAIT}s; a transaction may be in flight"
  info "wallet lock free"
else
  info "flock unavailable or no lock file; sleeping 90s instead"
  sleep 90
fi

# ── backup ───────────────────────────────────────────────────────────────────
say "Backing up state"
BACKUP="${STATE}.bak.$(date +%Y%m%d-%H%M%S)"
cp -a "$STATE" "$BACKUP"
info "copied to $BACKUP"

# ── update ───────────────────────────────────────────────────────────────────
say "Updating source"
ROLLBACK_REF="$(git -C "$REPO" rev-parse --abbrev-ref HEAD)"
[ "$ROLLBACK_REF" = "HEAD" ] && ROLLBACK_REF="$(git -C "$REPO" rev-parse HEAD)"
info "current ref $ROLLBACK_REF"
if [ -n "$(git -C "$REPO" status --porcelain)" ]; then
  die "working tree is dirty; commit or stash before deploying"
fi
git -C "$REPO" fetch origin "$BRANCH"
git -C "$REPO" checkout "$BRANCH"
git -C "$REPO" pull --ff-only origin "$BRANCH"
info "now at $(git -C "$REPO" rev-parse --short HEAD) $(git -C "$REPO" log -1 --format=%s | cut -c1-60)"

say "Installing dependencies"
( cd "$REPO" && npm install --no-audit --no-fund )
# No requirements.txt in the repo; web3 pulls eth_abi/eth_utils transitively.
python3 -m pip install --quiet --upgrade web3 requests mypy ruff

# ── verification gates ───────────────────────────────────────────────────────
say "Verifying — the bot stays halted unless every gate passes"
cd "$REPO"

run_gate() {
  local label="$1"; shift
  printf '   %-28s' "$label"
  if "$@" >/tmp/rh-deploy-gate.log 2>&1; then
    printf '\033[32mPASS\033[0m\n'
  else
    printf '\033[31mFAIL\033[0m\n'
    tail -25 /tmp/rh-deploy-gate.log >&2
    die "$label failed"
  fi
}

run_gate "typecheck (typescript)" npm run --silent typecheck
run_gate "typecheck (python)"     npm run --silent typecheck:py
run_gate "tests (python)"         npm run --silent test:py
run_gate "tests (typescript)"     node --import tsx --test test/v4-safety.test.ts test/v4-settlement.test.ts test/atomic-v4-plan.test.ts test/kyber-decode.test.ts

if [ "$SKIP_CONTRACTS" -eq 1 ]; then
  printf '   %-28s\033[33mSKIPPED\033[0m — the contracts hold the funds; run "npx hardhat test" before enabling V4\n' "tests (contracts)"
else
  run_gate "tests (contracts)" npx hardhat test
fi

# ── V4 readiness (informational) ─────────────────────────────────────────────
say "V4 certification status"
if [ -n "$TOKEN" ]; then
  ( cd "$REPO/automation" && python3 v4_certify.py --token "$TOKEN" ) || true
else
  ( cd "$REPO/automation" && python3 v4_certify.py ) || true
  info "pass --token 0x... to also check pool discovery and quoting"
fi
info "this is read-only and never blocks the deploy; V4 stays fail-closed until certified"

# ── resume ───────────────────────────────────────────────────────────────────
say "Result"
if [ "$PRE_HALTED" -eq 1 ]; then
  info "the bot was halted before this deploy; leaving it halted"
  info "resume when you intend to:  rm $HALT"
elif [ "$RESUME" -eq 1 ]; then
  rm -f "$HALT"
  info "all gates passed; kill switch cleared, cron will pick up the new revision"
else
  info "all gates passed; the bot is still HALTED by design"
  info "resume with:  rm $HALT"
fi
info "backup:   $BACKUP"
info "rollback: git -C $REPO checkout $ROLLBACK_REF"
printf '\n'
