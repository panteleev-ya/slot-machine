#!/bin/sh
# Build and run the Linux Chrome probe. Default (no arguments) only checks that
# Chrome is a normal window. It does not open the visa site.
#
#   src/signin/docker/run.sh
#   src/signin/docker/run.sh --once
#   src/signin/docker/run.sh --every 50 --jitter 3
set -eu

cd "$(dirname "$0")/../../.."
tag=slot-machine-signin-probe
platform=linux/amd64

need_secrets=0
if [ "$#" -gt 0 ]; then
    need_secrets=1
    for arg in "$@"; do
        if [ "$arg" = "--check" ]; then
            need_secrets=0
        fi
    done
fi

creds="${PWD}/config/ais-login.env"
settings="${PWD}/config/slot-machine.env"
if [ "$need_secrets" -eq 1 ]; then
    if [ ! -f "$creds" ] || [ ! -f "$settings" ]; then
        echo "Sign-in needs config/ais-login.env and config/slot-machine.env" >&2
        exit 1
    fi
fi

docker build --platform "$platform" -f src/signin/docker/Dockerfile -t "$tag" .

pi_ip="$(python3 -c 'import socket; print(socket.gethostbyname("yarikos.local"))' 2>/dev/null || true)"
add_host=""
case "$pi_ip" in
    *.*.*.*)
        add_host="--add-host=yarikos.local:${pi_ip}"
        echo "Container will reach yarikos.local at ${pi_ip}."
        ;;
    *)
        echo "Could not resolve yarikos.local from the Mac. A sign-in run may not reach the Pi."
        ;;
esac

mounts=""
if [ "$need_secrets" -eq 1 ]; then
    mounts="$mounts -v ${creds}:/opt/slot-machine/config/ais-login.env:ro"
    mounts="$mounts -v ${settings}:/opt/slot-machine/config/slot-machine.env:ro"
    echo "If a cycle fails, a picture of the window is written to src/signin/shots/last.png"
fi

mkdir -p src/signin/shots
tty_flags="-i"
if [ -t 0 ]; then
    tty_flags="-it"
fi

# Docker blocks the user-namespace move Chrome's sandbox makes. These two flags
# let that sandbox start. A real mini-PC does not need them, and Chrome is still
# sandboxed either way. --no-sandbox is not used.
# shellcheck disable=SC2086
exec docker run --rm $tty_flags \
    --platform "$platform" \
    --shm-size=1g \
    --cap-add SYS_ADMIN \
    --security-opt seccomp=unconfined \
    $add_host \
    $mounts \
    -v "${PWD}/src/signin/shots:/home/probe/shots" \
    -v signin-chrome-profile:/home/probe/chrome-profile \
    "$tag" \
    "$@"
