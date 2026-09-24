#!/usr/bin/env bash
# deploy/windows/ccc-keepalive.sh against a fake tmux, a scratch cc.db and a scratch
# backup folder. No real dashboard is touched and no real tmux server is started.
#   bash <(tr -d '\r' < dashboard/test_ccc_keepalive.sh)
set -u
[ -f deploy/windows/ccc-keepalive.sh ] || { echo 'run this from the agentmux repo root' >&2; exit 2; }
SCRIPT="$(mktemp)"; tr -d '\r' < deploy/windows/ccc-keepalive.sh > "$SCRIPT"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK" "$SCRIPT"; [ -n "${SRV:-}" ] && kill "$SRV" 2>/dev/null' EXIT
passed=0; failed=0
ok()   { passed=$((passed + 1)); echo "PASS $1"; }
fail() { failed=$((failed + 1)); echo "  FAIL $1"; }
check() { if eval "$2"; then ok "$1"; else fail "$1"; fi; }

# A fake tmux: has-session answers from a state file, new-session creates it, and every
# call is recorded. The keepalive must never need more than that.
mkdir -p "$WORK/bin" "$WORK/repo/dashboard" "$WORK/home/.agentmux" "$WORK/evidence/TM-001"
cat > "$WORK/bin/tmux" <<'EOF'
#!/usr/bin/env bash
echo "$*" >> "$FAKE_TMUX_LOG"
case " $* " in
  *" has-session "*) [ -f "$FAKE_TMUX_STATE" ] ;;
  *" new-session "*) touch "$FAKE_TMUX_STATE" ;;
  *" kill-session "*) rm -f "$FAKE_TMUX_STATE" ;;
esac
EOF
chmod +x "$WORK/bin/tmux"
touch "$WORK/repo/dashboard/server.py"
echo "proof" > "$WORK/evidence/TM-001/run.log"
python3 - "$WORK/home/.agentmux/cc.db" <<'PY'
import sqlite3, sys
db = sqlite3.connect(sys.argv[1]); db.execute("PRAGMA journal_mode=WAL")
db.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, key TEXT, title TEXT)")
db.execute("CREATE TABLE epics (id INTEGER PRIMARY KEY, key TEXT)")
db.executemany("INSERT INTO tasks (key,title) VALUES (?,?)", [(f"TM-{i:03d}", "t") for i in range(1, 8)])
db.commit()
PY

PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')
run() {
  env -i PATH="$WORK/bin:/usr/bin:/bin" HOME="$WORK/home" \
    FAKE_TMUX_LOG="$WORK/tmux.log" FAKE_TMUX_STATE="$WORK/tmux.state" \
    CCC_REPO="$WORK/repo" CCC_BACKUP_DIR="$WORK/backups" CCC_EVIDENCE_DIR="$WORK/evidence" \
    CCC_DASHBOARD="http://127.0.0.1:$PORT" CCC_KEEPALIVE_STATE="$WORK/state" \
    ${EXTRA:-} bash "$SCRIPT" "$@"
}
LOG="$WORK/state/keepalive.log"

# ── the dashboard ────────────────────────────────────────────────────────────
run --once
check "a dashboard that does not answer is started in tmux -L ccc from the repo" \
  "grep -q -- '-L ccc new-session -d -s dashboard -c $WORK/repo' '$WORK/tmux.log'"
check "the start is logged" "grep -q 'dashboard was down' '$LOG'"

: > "$WORK/tmux.log"
python3 - "$PORT" <<'PY' &
import http.server, sys
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
PY
SRV=$!
sleep 0.5
run --once
check "a dashboard that answers is left alone" "! grep -q new-session '$WORK/tmux.log'"
kill "$SRV"; SRV=

# ── backups ──────────────────────────────────────────────────────────────────
mkdir -p "$WORK/backups/hourly" "$WORK/backups/daily"
for i in $(seq -w 1 50); do touch "$WORK/backups/hourly/cc-20200101T0000${i}Z.db"; done
for i in $(seq -w 1 20); do touch "$WORK/backups/daily/cc-202001${i}.db"; done
run --backup-now; rc=$?
check "a forced backup succeeds" "[ $rc -eq 0 ]"
NEW=$(ls "$WORK/backups/hourly" | sort | tail -1)
check "the newest hourly copy is today's backup" "[[ '$NEW' != cc-2020* ]]"
check "the copy passes integrity_check and holds the rows" \
  "python3 -c \"import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); assert c.execute('pragma integrity_check').fetchone()[0]=='ok'; assert c.execute('select count(*) from tasks').fetchone()[0]==7\" '$WORK/backups/hourly/$NEW'"
check "hourly copies rotate to 48" "[ $(ls "$WORK/backups/hourly" | wc -l) -eq 48 ]"
check "daily copies rotate to 14, and today's is kept" \
  "[ $(ls "$WORK/backups/daily" | wc -l) -eq 14 ] && ls '$WORK/backups/daily' | grep -q \"cc-\$(date -u +%Y%m%d).db\""
check "latest.json records integrity and counts" \
  "python3 -c \"import json,sys; d=json.load(open(sys.argv[1])); assert d['integrity']=='ok' and d['counts']['tasks']==7\" '$WORK/backups/latest.json'"
check "evidence is mirrored into the backups" "[ \"\$(cat '$WORK/backups/evidence/TM-001/run.log')\" = proof ]"

# ── scheduling and the single-instance lock ─────────────────────────────────
before=$(ls "$WORK/backups/hourly" | sort | tail -1)
run --once
check "a backup is not repeated before it is due" "[ \"\$(ls '$WORK/backups/hourly' | sort | tail -1)\" = '$before' ]"
EXTRA="CCC_BACKUP_EVERY=0" run --once
check "a backup that is due is taken on the next pass" "[ \"\$(ls '$WORK/backups/hourly' | wc -l)\" -ge 48 ] && grep -c 'backup: ok' '$LOG' | grep -q '^[2-9]'"

: > "$WORK/tmux.log"
( exec 9>"$WORK/state/lock"; flock 9; sleep 3 ) &
HOLD=$!
sleep 0.5
run --once
wait "$HOLD"
check "a second keepalive exits while the first holds the lock" \
  "grep -q 'already running' '$LOG' && ! grep -q new-session '$WORK/tmux.log'"

# ── failures are logged, never fatal to the loop ────────────────────────────
AGENTMUX_HOME_MISSING="$WORK/nothing"
EXTRA="AGENTMUX_HOME=$AGENTMUX_HOME_MISSING" run --backup-now; rc=$?
check "a missing database is a logged refusal, not a crash" "[ $rc -ne 0 ] && grep -q 'no database' '$LOG'"

echo "passed $passed, failed $failed"
[ "$failed" -eq 0 ]
