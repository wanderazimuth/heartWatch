#!/usr/bin/env bash
# Deploy the committed HEAD to the Pi and run the installer there. From Git Bash:
#   bash deploy/push.sh pi@<pi address>   (or set HEARTWATCH_PI)
# Only committed code goes: config.toml, data/ and *.fit stay on this machine.
set -euo pipefail

HOST=${1:-${HEARTWATCH_PI:-pi@raspberrypi.local}}
cd "$(dirname "$0")/.."
git diff --quiet HEAD || echo "note: uncommitted changes are not deployed" >&2

git archive --format=tar HEAD | ssh "$HOST" '
    set -e
    rm -rf /tmp/heartwatch-src && mkdir /tmp/heartwatch-src
    tar -x -C /tmp/heartwatch-src
    sudo mkdir -p /opt/heartwatch
    sudo rsync -a --delete --exclude=/.venv --chown=root:root /tmp/heartwatch-src/ /opt/heartwatch/
    rm -rf /tmp/heartwatch-src
    sudo bash /opt/heartwatch/deploy/install.sh'
