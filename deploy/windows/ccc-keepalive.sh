#!/usr/bin/env bash
# ccc-keepalive: keep Control Center up on a Windows + WSL machine, and back its board up.
#
# Started hidden at Windows logon by ccc-keepalive.vbs (a Startup-folder shortcut). Its
# wsl.exe stays running, which keeps WSL up, so closing an Ubuntu window never takes the
# dashboard with it. Every $CCC_KEEPALIVE_INTERVAL seconds it:
#   - asks the dashboard for /api/board/meta. Nothing answering twice in a row -> the
#     `dashboard` session on tmux socket `ccc` is (re)started from $CCC_REPO. A dashboard
#     that answers is never touched, whoever started it.
#   - every $CCC_BACKUP_EVERY seconds, takes an ONLINE backup of cc.db (sqlite3's backup
#     API: cc.db is in WAL mode, so copying the file can capture a torn database), checks
#     it with PRAGMA integrity_check, and files it under $CCC_BACKUP_DIR/hourly. The first
#     good backup of each day is also kept under daily/. Old copies rotate out:
#     $CCC_KEEP_HOURLY hourly, $CCC_KEEP_DAILY daily.
#   - mirrors $CCC_EVIDENCE_DIR (the board keeps evidence as paths) into $CCC_BACKUP_DIR/evidence.
#
# Run it from WSL. This repo is checked out with CRLF on Windows, so read it through tr:
#   CCC_REPO=/mnt/c/.../CControlCenter CCC_BACKUP_DIR=/mnt/c/.../ccc-backups \
#     bash <(tr -d '\r' < deploy/windows/ccc-keepalive.sh) [--once | --backup-now | --status]
#
#   --once        one check-and-repair pass (backup included if due), then exit
#   --backup-now  one backup, now, then exit
#   --status      print what it knows and exit
# Log: ~/.local/state/ccc-keepalive/keepalive.log. One looping instance per user (flock).
set -u

REPO=${CCC_REPO:-}
BACKUP_DIR=${CCC_BACKUP_DIR:-}
EVIDENCE_DIR=${CCC_EVIDENCE_DIR:-}
INTERVAL=${CCC_KEEPALIVE_INTERVAL:-60}
BACKUP_EVERY=${CCC_BACKUP_EVERY:-3600}
KEEP_HOURLY=${CCC_KEEP_HOURLY:-48}
KEEP_DAILY=${CCC_KEEP_DAILY:-14}
URL=${CCC_DASHBOARD:-http://127.0.0.1:8787}
SOCKET=${CCC_TMUX_SOCKET:-ccc}
SESSION=dashboard
DB=${AGENTMUX_HOME:-$HOME/.agentmux}/cc.db
STATE=${CCC_KEEPALIVE_STATE:-$HOME/.local/state/ccc-keepalive}
mkdir -p "$STATE"
LOG=$STATE/keepalive.log
MISSES=0

log() { printf '%s  %s\n' "$(date -Is)" "$*" >> "$LOG"; }

answering() {
  python3 - "$URL/api/board/meta" <<'PY' >/dev/null 2>&1
import sys, urllib.request
urllib.request.urlopen(sys.argv[1], timeout=3).read()
PY
}

ensure_dashboard() {
  if answering; then MISSES=0; return 0; fi
  MISSES=$((MISSES + 1))
  # One miss can be a restart in progress or a slow request; two is down.
  if [ "$MISSES" -lt 2 ] && tmux -L "$SOCKET" has-session -t "$SESSION" 2>/dev/null; then
    log "dashboard not answering (miss $MISSES); waiting one more interval"
    return 0
  fi
  if [ -z "$REPO" ] || [ ! -f "$REPO/dashboard/server.py" ]; then
    log "dashboard down and CCC_REPO ($REPO) has no dashboard/server.py; cannot start it"
    return 1
  fi
  tmux -L "$SOCKET" kill-session -t "$SESSION" 2>/dev/null
  tmux -L "$SOCKET" new-session -d -s "$SESSION" -c "$REPO" \
    "python3 dashboard/server.py >> '$STATE/server.log' 2>&1" 9>&-
  log "dashboard was down -> started tmux -L $SOCKET session $SESSION from $REPO"
  MISSES=0
}

backup() {
  [ -n "$BACKUP_DIR" ] || { log "backup skipped: CCC_BACKUP_DIR is not set"; return 1; }
  [ -f "$DB" ] || { log "backup skipped: no database at $DB"; return 1; }
  mkdir -p "$BACKUP_DIR/hourly" "$BACKUP_DIR/daily" || { log "backup failed: cannot create $BACKUP_DIR"; return 1; }
  local out
  out=$(python3 - "$DB" "$BACKUP_DIR" "$KEEP_HOURLY" "$KEEP_DAILY" <<'PY' 2>&1
import datetime as dt, json, os, shutil, sqlite3, sys, tempfile
db, root, keep_hourly, keep_daily = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
now = dt.datetime.now(dt.timezone.utc)
stamp = now.strftime("%Y%m%dT%H%M%SZ")
# Back up to the Linux side first: SQLite writing straight onto a Windows (drvfs)
# path is exactly the kind of file locking this is meant to be safe from.
fd, tmp = tempfile.mkstemp(prefix="cc-backup-", suffix=".db")
os.close(fd)
try:
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    dst = sqlite3.connect(tmp)
    src.backup(dst)
    src.close()
    check = dst.execute("PRAGMA integrity_check").fetchone()[0]
    counts = {}
    for table in ("epics", "tasks", "journal", "board_history"):
        try:
            counts[table] = dst.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.Error:
            counts[table] = None
    dst.close()
    if check != "ok":
        bad = os.path.join(root, f"cc-{stamp}.bad.db")
        shutil.copyfile(tmp, bad)
        print(f"INTEGRITY FAILED ({check}); kept as {bad}")
        sys.exit(1)
    hourly = os.path.join(root, "hourly", f"cc-{stamp}.db")
    shutil.copyfile(tmp, hourly + ".part")
    os.replace(hourly + ".part", hourly)
    daily = os.path.join(root, "daily", f"cc-{now.strftime('%Y%m%d')}.db")
    if not os.path.exists(daily):
        shutil.copyfile(hourly, daily + ".part")
        os.replace(daily + ".part", daily)
finally:
    os.unlink(tmp)
for folder, keep in (("hourly", keep_hourly), ("daily", keep_daily)):
    path = os.path.join(root, folder)
    names = sorted(n for n in os.listdir(path) if n.startswith("cc-") and n.endswith(".db"))
    for name in names[:-keep] if keep > 0 else names:
        os.unlink(os.path.join(path, name))
latest = {"at": now.isoformat(), "file": hourly, "integrity": check,
          "bytes": os.path.getsize(hourly), "counts": counts}
with open(os.path.join(root, "latest.json"), "w", encoding="utf-8") as f:
    json.dump(latest, f, indent=2)
print(f"ok {hourly} {latest['bytes']} bytes, {counts}")
PY
  )
  local rc=$?
  log "backup: $out"
  if [ $rc -eq 0 ] && [ -n "$EVIDENCE_DIR" ] && [ -d "$EVIDENCE_DIR" ]; then
    mkdir -p "$BACKUP_DIR/evidence" && cp -ru "$EVIDENCE_DIR/." "$BACKUP_DIR/evidence/" \
      && log "evidence mirrored from $EVIDENCE_DIR" || log "evidence mirror FAILED"
  fi
  [ $rc -eq 0 ] && date +%s > "$STATE/last-backup"
  return $rc
}

backup_due() {
  local last
  last=$(cat "$STATE/last-backup" 2>/dev/null || echo 0)
  [ $(( $(date +%s) - last )) -ge "$BACKUP_EVERY" ]
}

pass() {
  ensure_dashboard
  if backup_due; then backup; fi
}

case "${1:-}" in
  --backup-now)
    exec 8>"$STATE/backup.lock"; flock 8
    backup; exit $? ;;
  --status)
    echo "dashboard: $(answering && echo answering || echo DOWN) at $URL"
    echo "tmux:      $(tmux -L "$SOCKET" has-session -t "$SESSION" 2>/dev/null && echo "session $SESSION on -L $SOCKET" || echo 'no session')"
    echo "database:  $DB"
    echo "backups:   ${BACKUP_DIR:-(CCC_BACKUP_DIR not set)}"
    [ -f "${BACKUP_DIR:-/nonexistent}/latest.json" ] && cat "$BACKUP_DIR/latest.json"
    exit 0 ;;
esac

exec 9>"$STATE/lock"
if ! flock -n 9; then log "another keepalive is already running; exiting"; exit 0; fi
log "keepalive started (pid $$, interval ${INTERVAL}s, backups every ${BACKUP_EVERY}s to ${BACKUP_DIR:-nowhere})"
if [ "${1:-}" = "--once" ]; then pass; log "once: done"; exit 0; fi
# Children never inherit the lock (fd 9): a leftover sleep holding it would stop a restarted keepalive.
while true; do pass; sleep "$INTERVAL" 9>&-; done
