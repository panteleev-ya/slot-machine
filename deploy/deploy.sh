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
LOCAL_CONFIG=${LOCAL_CONFIG:-$ROOT/config/slot-machine.env}
MARK="# slot-machine (managed by deploy.sh)"
BOT_MARK="# slot-machine telegram (managed by deploy.sh)"  # the /check poller
SERVE_MARK="# slot-machine renew (managed by deploy.sh)"  # the renewal page; exits if already running

ssh_pi() { ssh -i "$SSH_KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o LogLevel=ERROR "$PI" "$@"; }

deploy() {
    [[ -f "$APP" ]] || { echo "No $APP - run 'make build' first." >&2; exit 1; }
    echo "==> $PI"
    ssh_pi 'mkdir -p ~/.local/bin && mkdir -p -m 700 ~/.config/slot-machine ~/.local/state/slot-machine'
    # upload next to the target, then rename: a cron run never sees a half-written file
    ssh_pi 'cat > ~/.local/bin/slot-machine.new && chmod 755 ~/.local/bin/slot-machine.new &&
            mv -f ~/.local/bin/slot-machine.new ~/.local/bin/slot-machine' < "$APP"
    echo "app:      ~/.local/bin/slot-machine ($(ssh_pi '~/.local/bin/slot-machine --version'))"
    restart_page
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
    restart_page
    # a changed CRON_SCHEDULE takes effect right away, but only if cron is already on
    if cron_is_on; then cron_on; fi
}

# The renewal page runs until killed; stop it so cron restarts it with the new code/settings.
restart_page() { ssh_pi "pkill -u \$(id -u) -f 'slot-machin[e] --serve' || true"; }

cron_is_on() { ssh_pi "crontab -l 2>/dev/null | grep -qF '$MARK'"; }

cron_on() {
    # CRON_SCHEDULE from the local settings, validated by the app's own parser (five plain
    # crontab fields, nothing a shell would interpret); the default when there are none
    local schedule
    schedule=$(python3 "$ROOT/deploy/sync_config.py" --cron-schedule "$LOCAL_CONFIG") \
        || { echo "Cron not changed: fix CRON_SCHEDULE first." >&2; exit 1; }
    ssh_pi bash -s <<EOF
set -e
app=\$HOME/.local/bin/slot-machine
log=\$HOME/.local/state/slot-machine/watch.log
check="$schedule \$app --via cron >> \$log 2>&1 $MARK"
poll="* * * * * \$app --poll-telegram >> \$log 2>&1 $BOT_MARK"
serve="* * * * * \$app --serve >> \$log 2>&1 $SERVE_MARK"
(crontab -l 2>/dev/null | grep -vF -e '$MARK' -e '$BOT_MARK' -e '$SERVE_MARK' || true
 echo "\$check"; echo "\$poll"; echo "\$serve") | crontab -
echo "cron:     on (checks on CRON_SCHEDULE; Telegram commands and the renewal page every minute)"
crontab -l | grep -F -e '$MARK' -e '$BOT_MARK' -e '$SERVE_MARK'
EOF
}

cron_off() {
    ssh_pi bash -s <<EOF
set -e
(crontab -l 2>/dev/null | grep -vF -e '$MARK' -e '$BOT_MARK' -e '$SERVE_MARK' || true) | crontab -
pkill -u \$(id -u) -f 'slot-machin[e] --serve' || true
echo "cron:     off (checks, Telegram commands, renewal page)"
EOF
}

case "${1:-}" in
    "") deploy ;;
    --push-config) push_config ;;
    --cron-on) cron_on ;;
    --cron-off) cron_off ;;
    *) echo "usage: $0 [--push-config | --cron-on | --cron-off]" >&2; exit 2 ;;
esac
