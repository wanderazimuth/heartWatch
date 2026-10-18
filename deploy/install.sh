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

# rsync + ssh or cifs-utils: the nightly copy to the NAS.
apt-get install -y --no-install-recommends python3-venv rsync openssh-client cifs-utils

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

# backup_share: mount the NAS folder for the backup only (StopWhenUnneeded), so a NAS that's down
# fails the backup instead of it writing onto the SD card, and the NAS isn't touched the rest of the day.
setting() {
    "$APP_DIR/.venv/bin/python" -c 'import sys, tomllib; print(tomllib.load(open(sys.argv[1], "rb")).get(sys.argv[2], ""))' \
        "$CONF_DIR/config.toml" "$1"
}
SHARE=$(setting backup_share)
TARGET=$(setting backup_target)
CREDS=$CONF_DIR/nas-credentials
DROPIN=/etc/systemd/system/heartwatch-backup.service.d
if [[ -n $SHARE ]]; then
    if [[ ! -f $CREDS ]]; then
        (umask 077; printf 'username=heartwatch\npassword=\n' > "$CREDS")
        echo "created $CREDS -- put the NAS account's password in it"
    fi
    chown root:root "$CREDS"
    chmod 600 "$CREDS"
    install -d -m 755 /mnt/heartwatch
    cat > /etc/systemd/system/mnt-heartwatch.mount <<UNIT
[Unit]
Description=NAS folder for HeartWatch backups
After=network-online.target
Wants=network-online.target
StopWhenUnneeded=yes

[Mount]
What=$SHARE
Where=/mnt/heartwatch
Type=cifs
Options=credentials=$CREDS,uid=heartwatch,gid=heartwatch,file_mode=0640,dir_mode=0750,_netdev
TimeoutSec=60
UNIT
    install -d -m 755 "$DROPIN"
    printf '[Unit]\nRequiresMountsFor=/mnt/heartwatch\n\n[Service]\nReadWritePaths=/mnt/heartwatch\n' > "$DROPIN/nas.conf"
else
    rm -f /etc/systemd/system/mnt-heartwatch.mount "$DROPIN/nas.conf"
fi

systemctl daemon-reload
systemctl enable heartwatch-web
systemctl restart heartwatch-web
# The poll skips itself until dropbox-login has saved a token.
systemctl enable --now heartwatch-poll.timer
if [[ -n $SHARE || -n $TARGET ]]; then
    systemctl enable --now heartwatch-backup.timer
else
    systemctl disable --now heartwatch-backup.timer 2>/dev/null || true
    echo "backup_share and backup_target are not set: no nightly NAS copy (see README)"
fi
systemctl --no-pager status heartwatch-web
systemctl --no-pager list-timers 'heartwatch-*'
if [[ -z $SHARE ]]; then
    echo
    echo "NAS backup key, for a backup_target over ssh (add to the NAS account's ~/.ssh/authorized_keys):"
    cat "$STATE_DIR/.ssh/id_ed25519.pub"
fi
