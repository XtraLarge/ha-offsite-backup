#!/usr/bin/env bash
set -euo pipefail

CONFIG=/data/options.json

# Optionen lesen
NAS_HOST=$(jq -r '.nas_host' "$CONFIG")
NAS_USER=$(jq -r '.nas_user // "root"' "$CONFIG")
BACKUP_SCHEDULE=$(jq -r '.backup_schedule' "$CONFIG")
LOKI_URL=$(jq -r '.loki_url // ""' "$CONFIG")

echo "Offsite Backup Add-on startet"
echo "  NAS:       ${NAS_USER}@${NAS_HOST}"
echo "  Zeitplan:  $BACKUP_SCHEDULE"
echo "  Loki-URL:  ${LOKI_URL:-deaktiviert}"

mkdir -p /data/logs /data/secrets /data/backuppc-recovery

# Key-Berechtigungen korrigieren
find /data/secrets -name 'id_ed25519_*' -exec chmod 600 {} \; 2>/dev/null || true

# Cron einrichten
echo "$BACKUP_SCHEDULE root /scripts/backup.sh >> /data/logs/backup.log 2>&1" \
    > /etc/cron.d/offsite-backup
chmod 0644 /etc/cron.d/offsite-backup
crontab /etc/cron.d/offsite-backup

# Cron als Daemon starten (kein init-System nötig)
cron
echo "Cron gestartet: $BACKUP_SCHEDULE"

# user_allow_other für SSHFS (Recovery)
grep -q '^user_allow_other' /etc/fuse.conf 2>/dev/null \
    || echo 'user_allow_other' >> /etc/fuse.conf

# API starten
exec python3 /api.py
