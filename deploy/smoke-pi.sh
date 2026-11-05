#!/usr/bin/env bash
# Run the deployed app on the Pi against fake AIS and Telegram servers (tests/fakes.py),
# in a throwaway folder. It never touches the real settings or state and never
# contacts the real visa site or Telegram.   Usage: make smoke-pi
set -euo pipefail

PI=${PI:-yarikp@yarikos.local}
SSH_KEY=${SSH_KEY:-$HOME/.ssh/pi}
ROOT=$(cd "$(dirname "$0")/.." && pwd)

ssh_pi() { ssh -i "$SSH_KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o LogLevel=ERROR "$PI" "$@"; }

tmp=$(ssh_pi 'mktemp -d /tmp/slot-machine-smoke.XXXXXX')
ssh_pi "cat > $tmp/fakes.py" < "$ROOT/tests/fakes.py"

ssh_pi bash -s -- "$tmp" <<'EOF'
set -euo pipefail
tmp=$1
app=~/.local/bin/slot-machine
cleanup() { kill "$fakes_pid" 2>/dev/null || true; rm -rf "$tmp"; }
python3 "$tmp/fakes.py" --port-file "$tmp/ports.json" & fakes_pid=$!
trap cleanup EXIT
for _ in $(seq 50); do [[ -s "$tmp/ports.json" ]] && break; sleep 0.1; done
eval "$(python3 -c "
import json; p = json.load(open('$tmp/ports.json'))
for k, v in p.items(): print(f'{k}={v!r}')
")"

(umask 077; cat > "$tmp/test.env" <<ENV
AIS_SCHEDULE_ID=1234
AIS_FACILITY_ID=134
TELEGRAM_BOT_TOKEN=$token
TELEGRAM_CHAT_ID=$chat_id
TIMEZONE=UTC
STATE_DIR=$tmp/state
AIS_BASE_URL=$ais
TELEGRAM_API_BASE=$telegram
ENV
)
sm() { "$app" --config "$tmp/test.env" "$@"; }
control() { curl -fsS -X POST -H 'Content-Type: application/json' -d "$2" "$1/_control" > /dev/null; }
fail() { echo "SMOKE FAILED: $*" >&2; exit 1; }
messages() { curl -fsS "$telegram/_messages" | python3 -c 'import json,sys; [print("  tg:", m["text"].splitlines()[0]) for m in json.load(sys.stdin)]'; }

echo "== $("$app" --version) on $(hostname), $(python3 --version)"
control "$ais" '{"dates": ["2027-09-01", "2027-10-01"]}'
out=$(sm --set-cookie "$session") || fail "--set-cookie exited $?"
grep -q "Cookie works. Earliest date right now: 2027-09-01." <<<"$out" || fail "--set-cookie said: $out"
echo "ok   --set-cookie: baseline sent"

control "$ais" '{"dates": ["2027-08-01"]}'
sm --force > /dev/null || fail "check exited $?"
echo "ok   sooner date: alert sent"

sm > /dev/null && sm 2>&1 | grep -q "skipping" || fail "min-interval guard"
echo "ok   min-interval guard"

control "$ais" '{"expire": true}'
sm --force > /dev/null && fail "expired session should exit 1" || true
echo "ok   expired session: alert sent, requests stop"

[[ $(stat -c %a "$tmp/state/cookies.json") == 600 && $(stat -c %a "$tmp/state/state.json") == 600 ]] \
    || fail "state files are not private"
echo "ok   state files are chmod 600"

sm --status | sed 's/^/  /'
messages
count=$(curl -fsS "$telegram/_messages" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')
[[ $count == 3 ]] || fail "expected 3 Telegram messages, got $count"
echo "SMOKE PASSED"
EOF
