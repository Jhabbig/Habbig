#!/usr/bin/env bash
# install_backup_cron.sh — idempotent crontab wiring for the backup scripts.
#
# Writes /etc/cron.d/narve-backup (requires root). Designed to be run
# once on the production host during initial deploy or after a
# schedule change. Safe to re-run — overwrites the existing file.

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "must run as root (writes /etc/cron.d/narve-backup)" >&2
    exit 1
fi

# Default user is the canonical prod account (julianhabbig on the
# Tailscale-facing Ubuntu box). Override via env for staging chroots,
# CI runs, or a future rename.
USER_ACCOUNT="${USER_ACCOUNT:-${SUDO_USER:-julianhabbig}}"
REPO="${REPO:-/home/${USER_ACCOUNT}/Habbig}"
CRON_FILE=/etc/cron.d/narve-backup

cat > "$CRON_FILE" <<EOF
# Managed by gateway/scripts/install_backup_cron.sh — do not hand-edit.
# 3-2-1 backup for narve.ai auth.db.

SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
MAILTO=${BACKUP_MAILTO:-ops@narve.ai}

# Hourly at :07 to avoid the top-of-hour thundering herd.
7 * * * * $USER_ACCOUNT $REPO/gateway/scripts/backup_hourly.sh >> /var/log/narve-backup-hourly.log 2>&1

# Daily at 03:14 (off-peak, post-log-rotate).
14 3 * * * $USER_ACCOUNT $REPO/gateway/scripts/backup_daily.sh >> /var/log/narve-backup-daily.log 2>&1

# Weekly offsite on Sun 03:42 — env vars live in /etc/default/narve-backup.
42 3 * * 0 $USER_ACCOUNT . /etc/default/narve-backup; $REPO/gateway/scripts/backup_offsite.sh >> /var/log/narve-backup-offsite.log 2>&1

# Weekly verification on Mon 04:07 — writes PASS/FAIL to its own log
# so stale verification alerts show up in /admin/backups.
7 4 * * 1 $USER_ACCOUNT $REPO/gateway/scripts/backup_verify.sh >> /var/log/narve-backup-verify.log 2>&1
EOF

chmod 0644 "$CRON_FILE"
chown root:root "$CRON_FILE"

mkdir -p /var/log
touch /var/log/narve-backup-{hourly,daily,offsite,verify}.log
chown "$USER_ACCOUNT":"$USER_ACCOUNT" /var/log/narve-backup-*.log

echo "installed $CRON_FILE"
echo "logs under /var/log/narve-backup-*.log"
echo ""
echo "Remember to:"
echo "  1. populate /etc/default/narve-backup with BACKUP_GPG_RECIPIENT"
echo "     and BACKUP_OFFSITE_RSYNC_TARGET for the weekly offsite push"
echo "  2. trust the GPG recipient key on this host"
echo "  3. put the rsync target's SSH key in \$HOME/.ssh/known_hosts"
