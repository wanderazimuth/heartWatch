#!/usr/bin/env bash
# Install or update HeartWatch on the Pi. Run from the copy at /opt/heartwatch:
#   sudo bash /opt/heartwatch/deploy/install.sh
# (deploy/push.sh does that from Windows.)
set -euo pipefail

APP_DIR=/opt/heartwatch
CONF_DIR=/etc/heartwatch
STATE_DIR=/var/lib/heartwatch

if [[ $EUID -ne 0 ]]; then
    echo "run with sudo" >&2
    exit 1
fi

id heartwatch &>/dev/null || useradd --system --home-dir "$STATE_DIR" --no-create-home --shell /usr/sbin/nologin heartwatch

# rsync + ssh: the nightly copy to the NAS.
apt-get install -y --no-install-recommends python3-venv rsync openssh-client

[[ -d $APP_DIR/.venv ]] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade "$APP_DIR"

# Health data and GPS tracks: only the service user can read them.
install -d -m 750 -o heartwatch -g heartwatch "$STATE_DIR" "$STATE_DIR/.ssh"
install -d -m 750 -o root -g heartwatch "$CONF_DIR"
if [[ ! -f $CONF_DIR/config.toml ]]; then
    install -m 640 -o root -g heartwatch "$APP_DIR/config.example.toml" "$CONF_DIR/config.toml"
    echo "created $CONF_DIR/config.toml -- review it"
fi

# The key the nightly backup logs in to the NAS with.
if [[ ! -f $STATE_DIR/.ssh/id_ed25519 ]]; then
    sudo -u heartwatch ssh-keygen -q -t ed25519 -N "" -C "heartwatch@$(hostname)" -f "$STATE_DIR/.ssh/id_ed25519"
fi

for unit in heartwatch-web.service heartwatch-poll.service heartwatch-poll.timer \
            heartwatch-backup.service heartwatch-backup.timer; do
    install -m 644 "$APP_DIR/deploy/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable heartwatch-web
systemctl restart heartwatch-web
# The poll skips itself until dropbox-login has saved a token.
systemctl enable --now heartwatch-poll.timer
if grep -Eq '^backup_target *= *"[^"]+"' "$CONF_DIR/config.toml"; then
    systemctl enable --now heartwatch-backup.timer
else
    systemctl disable --now heartwatch-backup.timer 2>/dev/null || true
    echo "backup_target is not set: no nightly NAS copy (see README)"
fi
systemctl --no-pager status heartwatch-web
systemctl --no-pager list-timers 'heartwatch-*'
echo
echo "NAS backup key (add to the NAS account's ~/.ssh/authorized_keys):"
cat "$STATE_DIR/.ssh/id_ed25519.pub"
