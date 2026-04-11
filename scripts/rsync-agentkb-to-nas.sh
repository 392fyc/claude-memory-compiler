#!/usr/bin/env bash
# rsync-agentkb-to-nas.sh — one-way mirror of local AgentKB to NAS
#
# Source: $AGENTKB_DIR (defaults to D:/Mercury/AgentKB)
# Target: 392fyc@192.168.0.254:/share/CACHEDEV1_DATA/AgentKB/
# Trigger: Windows Task Scheduler hourly (see README / PR body)
#
# Safety contract:
#   - Strictly one-way: local -> NAS. Never pulls.
#   - --delete scoped to target dir only; exclude list gates what gets touched.
#   - Always exits 0 so Task Scheduler doesn't flag red; failures logged.
#   - Single-instance lock prevents overlapping runs.
#
# Requires: rsync, ssh, ~/.ssh/id_ed25519.
# On Windows hosts without native rsync, install via:
#   - `choco install rsync`  (Chocolatey)
#   - or MSYS2: `pacman -S rsync`
#   - or cwRsync bundle
# Not available in winget as of Apr 2026.

set -u  # strict unset-var; intentionally NOT set -e so logging always completes

AGENTKB_DIR="${AGENTKB_DIR:-D:/Mercury/AgentKB}"
LOCAL_SRC="${AGENTKB_DIR}/"
REMOTE_HOST="392fyc@192.168.0.254"
REMOTE_DIR="/share/CACHEDEV1_DATA/AgentKB/"
SSH_KEY="${HOME}/.ssh/id_ed25519"
EXCLUDE_FILE="${AGENTKB_DIR}/scripts/rsync-exclude.list"
LOG_FILE="${AGENTKB_DIR}/scripts/rsync.log"
LOCKDIR="${AGENTKB_DIR}/scripts/.rsync-nas.lock"

timestamp() { date '+%Y-%m-%d %H:%M:%S%z'; }
log() { echo "$(timestamp) $*" >> "$LOG_FILE"; }

# Single-instance lock
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  log "SKIP: previous run still holding $LOCKDIR"
  exit 0
fi
trap 'rmdir "$LOCKDIR" 2>/dev/null || true' EXIT

# Pre-flight: rsync / ssh / key / exclude file present
for bin in rsync ssh; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    log "FAIL: $bin not found on PATH"
    exit 0
  fi
done
if [ ! -f "$SSH_KEY" ]; then
  log "FAIL: ssh key missing at $SSH_KEY"
  exit 0
fi
if [ ! -f "$EXCLUDE_FILE" ]; then
  log "FAIL: exclude file missing at $EXCLUDE_FILE"
  exit 0
fi
if [ ! -d "$LOCAL_SRC" ]; then
  log "FAIL: source dir missing at $LOCAL_SRC"
  exit 0
fi

log "START rsync $LOCAL_SRC -> $REMOTE_HOST:$REMOTE_DIR"

rsync \
  -az \
  --delete \
  --exclude-from="$EXCLUDE_FILE" \
  -e "ssh -i $SSH_KEY -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new" \
  "$LOCAL_SRC" \
  "${REMOTE_HOST}:${REMOTE_DIR}" \
  >> "$LOG_FILE" 2>&1
RC=$?
log "END rsync rc=$RC"

# Post-sync file-count verification (soft warning only, never hard-fails)
LOCAL_COUNT=$(find "$LOCAL_SRC" -type f \
  -not -path '*/.git/*' \
  -not -path '*/.venv/*' \
  -not -path '*/.omc/*' \
  -not -path '*/__pycache__/*' \
  -not -path '*/stats/*' \
  -not -path '*/node_modules/*' \
  -not -path '*/.mypy_cache/*' \
  -not -path '*/.pytest_cache/*' \
  -not -name '*.log' \
  -not -name '*.pyc' \
  2>/dev/null | wc -l)

REMOTE_COUNT=$(ssh -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=10 \
  "$REMOTE_HOST" \
  "find '$REMOTE_DIR' -type f 2>/dev/null | wc -l" 2>/dev/null || echo "ERR")

if [ "$REMOTE_COUNT" = "ERR" ]; then
  log "VERIFY: remote count failed (ssh error)"
elif [ "$LOCAL_COUNT" -eq 0 ]; then
  log "VERIFY: local=0 remote=$REMOTE_COUNT (skipping ratio check)"
else
  if [ "$LOCAL_COUNT" -ge "$REMOTE_COUNT" ]; then
    DIFF=$((LOCAL_COUNT - REMOTE_COUNT))
  else
    DIFF=$((REMOTE_COUNT - LOCAL_COUNT))
  fi
  PCT=$((DIFF * 100 / LOCAL_COUNT))
  if [ "$PCT" -gt 5 ]; then
    log "VERIFY WARN: file-count delta ${PCT}% (local=$LOCAL_COUNT remote=$REMOTE_COUNT)"
  else
    log "VERIFY OK: local=$LOCAL_COUNT remote=$REMOTE_COUNT delta=${PCT}%"
  fi
fi

exit 0
