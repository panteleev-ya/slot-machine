#!/usr/bin/env bash
# Install slot-machine on the Pi and manage its cron entry. Usually run through make:
#   make deploy      build, copy the app, create folders, install the settings template once
#   make cron-on     add the hourly cron entry (idempotent)
#   make cron-off    remove it
# Nothing here needs sudo. Settings that already exist on the Pi are never overwritten.
set -euo pipefail

PI=${PI:-yarikp@yarikos.local}
SSH_KEY=${SSH_KEY:-$HOME/.ssh/pi}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
APP="$ROOT/dist/slot-machine"
MARK="# slot-machine (managed by deploy.sh)"
CRON_MINUTE=17

ssh_pi() { ssh -i "$SSH_KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o LogLevel=ERROR "$PI" "$@"; }

deploy() {
    [[ -f "$APP" ]] || { echo "No $APP - run 'make build' first." >&2; exit 1; }
    echo "==> $PI"
    ssh_pi 'mkdir -p ~/.local/bin && mkdir -p -m 700 ~/.config/slot-machine ~/.local/state/slot-machine'
    # upload next to the target, then rename: a cron run never sees a half-written file
    ssh_pi 'cat > ~/.local/bin/slot-machine.new && chmod 755 ~/.local/bin/slot-machine.new &&
            mv -f ~/.local/bin/slot-machine.new ~/.local/bin/slot-machine' < "$APP"
    echo "app:      ~/.local/bin/slot-machine ($(ssh_pi '~/.local/bin/slot-machine --version'))"
    if ssh_pi 'test -e ~/.config/slot-machine/slot-machine.env'; then
        echo "settings: ~/.config/slot-machine/slot-machine.env (kept as is)"
    else
        ssh_pi '(umask 077 && cat > ~/.config/slot-machine/slot-machine.env)' < "$ROOT/config/slot-machine.env.example"
        echo "settings: ~/.config/slot-machine/slot-machine.env (template installed - fill it in)"
    fi
    if ssh_pi "crontab -l 2>/dev/null | grep -qF '$MARK'"; then
        echo "cron:     on"
    else
        echo "cron:     off (make cron-on when the settings and cookie are in place)"
    fi
}

cron_on() {
    ssh_pi bash -s <<EOF
set -e
line="$CRON_MINUTE * * * * \$HOME/.local/bin/slot-machine >> \$HOME/.local/state/slot-machine/watch.log 2>&1 $MARK"
(crontab -l 2>/dev/null | grep -vF '$MARK' || true; echo "\$line") | crontab -
echo "cron:     on"
crontab -l | grep -F '$MARK'
EOF
}

cron_off() {
    ssh_pi bash -s <<EOF
set -e
(crontab -l 2>/dev/null | grep -vF '$MARK' || true) | crontab -
echo "cron:     off"
EOF
}

case "${1:-}" in
    "") deploy ;;
    --cron-on) cron_on ;;
    --cron-off) cron_off ;;
    *) echo "usage: $0 [--cron-on | --cron-off]" >&2; exit 2 ;;
esac
