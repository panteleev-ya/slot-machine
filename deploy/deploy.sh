#!/usr/bin/env bash
# Install slot-machine on the Pi and manage its cron entry. Usually run through make:
#   make deploy       build, copy the app, create folders, and upload config/slot-machine.env
#                     (or, if you have no local settings, install the template once)
#   make push-config  upload only the settings
#   make cron-on      add the hourly cron entry (idempotent)
#   make cron-off     remove it
# Nothing here needs sudo. Without a local config/slot-machine.env, the Pi's settings are never touched.
set -euo pipefail

PI=${PI:-yarikp@yarikos.local}
SSH_KEY=${SSH_KEY:-$HOME/.ssh/pi}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
APP="$ROOT/dist/slot-machine"
LOCAL_CONFIG="$ROOT/config/slot-machine.env"
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
    if [[ -f "$LOCAL_CONFIG" ]]; then
        push_config
    elif ssh_pi 'test -e ~/.config/slot-machine/slot-machine.env'; then
        echo "settings: ~/.config/slot-machine/slot-machine.env (kept as is; no local config/slot-machine.env)"
    else
        ssh_pi '(umask 077 && cat > ~/.config/slot-machine/slot-machine.env)' < "$ROOT/config/slot-machine.env.example"
        echo "settings: ~/.config/slot-machine/slot-machine.env (template installed; run 'make config' to keep yours locally)"
    fi
    if ssh_pi "crontab -l 2>/dev/null | grep -qF '$MARK'"; then
        echo "cron:     on"
    else
        echo "cron:     off (make cron-on when the settings and cookie are in place)"
    fi
}

# Your local, git-ignored settings are the source of truth: refresh them from the
# template, refuse to upload invalid values, then replace the Pi's copy atomically.
push_config() {
    [[ -f "$LOCAL_CONFIG" ]] || { echo "No $LOCAL_CONFIG - run 'make config' first." >&2; exit 1; }
    python3 "$ROOT/deploy/sync_config.py" "$ROOT/config/slot-machine.env.example" "$LOCAL_CONFIG" \
        || { echo "Not uploaded: fix the settings above first." >&2; exit 1; }
    ssh_pi 'mkdir -p -m 700 ~/.config/slot-machine && cd ~/.config/slot-machine &&
            (umask 077 && cat > slot-machine.env.new) && mv -f slot-machine.env.new slot-machine.env' < "$LOCAL_CONFIG"
    echo "settings: ~/.config/slot-machine/slot-machine.env (uploaded from config/slot-machine.env)"
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
    --push-config) push_config ;;
    --cron-on) cron_on ;;
    --cron-off) cron_off ;;
    *) echo "usage: $0 [--push-config | --cron-on | --cron-off]" >&2; exit 2 ;;
esac
