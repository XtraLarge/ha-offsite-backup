#!/usr/bin/with-contenv bash
set -euo pipefail

CONFIG=/data/options.json

# Optionen lesen
ZFS_HOST=$(jq -r '.zfs_storage_host' "$CONFIG")
ZFS_USER=$(jq -r '.zfs_storage_user // "root"' "$CONFIG")
BACKUP_SCHEDULE=$(jq -r '.backup_schedule' "$CONFIG")
LOKI_URL=$(jq -r '.loki_url // ""' "$CONFIG")

echo "Offsite Backup Add-on startet"
echo "  ZFS:       ${ZFS_USER}@${ZFS_HOST}"
echo "  Zeitplan:  $BACKUP_SCHEDULE"
echo "  Loki-URL:  ${LOKI_URL:-deaktiviert}"

mkdir -p /data/logs /data/secrets /data/backuppc-recovery

# SSH-Keys + Token aus Config in Dateien schreiben (falls gesetzt)
_write_secret() {
  local key="$1" file="$2"
  local val; val=$(jq -r ".$key // empty" "$CONFIG")
  [[ -z "$val" ]] && return
  # \n-Literale in echte Newlines umwandeln (für einzeilige Eingabe in HA-UI)
  printf '%b\n' "$val" > "$file"
  chmod 600 "$file"
  echo "Secret $key → $file geschrieben"
}
_write_secret ssh_key_storage  /data/secrets/id_ed25519_storage
_write_secret ssh_key_offsite  /data/secrets/id_ed25519_offsite
_write_secret offsite_token    /data/secrets/offsite_token

# Cron einrichten
echo "$BACKUP_SCHEDULE root /scripts/backup.sh >> /data/logs/backup.log 2>&1" \
    > /etc/cron.d/offsite-backup
chmod 0644 /etc/cron.d/offsite-backup

# Cron als Daemon starten (kein init-System nötig)
cron
echo "Cron gestartet: $BACKUP_SCHEDULE"

# user_allow_other für SSHFS (Recovery)
grep -q '^user_allow_other' /etc/fuse.conf 2>/dev/null \
    || echo 'user_allow_other' >> /etc/fuse.conf

# API starten
exec python3 /api.py
