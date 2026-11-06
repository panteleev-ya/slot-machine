#!/bin/sh
# Virtual screen, then the probe as an ordinary user. Python becomes pid 1 so Ctrl+C stops it.
set -eu

mkdir -p /home/probe/chrome-profile /home/probe/shots /tmp/runtime-probe /tmp/.X11-unix
chown probe:probe /home/probe/chrome-profile /home/probe/shots /tmp/runtime-probe
chmod 700 /tmp/runtime-probe
chmod 1777 /tmp/.X11-unix

setpriv --reuid=probe --regid=probe --init-groups --inh-caps=-all -- \
    Xvfb :99 -screen 0 1280x800x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &

i=0
while [ ! -S /tmp/.X11-unix/X99 ]; do
    i=$((i + 1))
    if [ "$i" -gt 50 ]; then
        echo "Xvfb did not start" >&2
        cat /tmp/xvfb.log >&2 || true
        exit 1
    fi
    sleep 0.1
done

setpriv --reuid=probe --regid=probe --init-groups --inh-caps=-all -- \
    env DISPLAY=:99 HOME=/home/probe XDG_RUNTIME_DIR=/tmp/runtime-probe \
    openbox >/tmp/openbox.log 2>&1 &

exec setpriv --reuid=probe --regid=probe --init-groups --inh-caps=-all -- \
    env DISPLAY=:99 HOME=/home/probe XDG_RUNTIME_DIR=/tmp/runtime-probe \
        SHOTS_DIR=/home/probe/shots CHROME_PROFILE=/home/probe/chrome-profile \
        PYTHONPATH=/opt/slot-machine/src \
    python3 -m signin "$@"
