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

# SSH-Keys + Token aus Config in Dateien schreiben (falls gesetzt)
_write_secret() {
  local key="$1" file="$2"
  local val; val=$(jq -r ".$key // empty" "$CONFIG")
  [[ -z "$val" ]] && return
  # \n-Literale in echte Newlines umwandeln (für einzeilige Eingabe in HA-UI)
  printf '%b' "$val" > "$file"
  chmod 600 "$file"
  echo "Secret $key → $file geschrieben"
}
_write_secret ssh_key_nas       /data/secrets/id_ed25519_nas
_write_secret ssh_key_hetzner   /data/secrets/id_ed25519_hetzner
_write_secret ssh_key_recovery  /data/secrets/id_ed25519_recovery
_write_secret hetzner_token     /data/secrets/hetzner_token

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

# Supervisor-Token prüfen
if [ -n "${SUPERVISOR_TOKEN:-}" ]; then
  echo "SUPERVISOR_TOKEN: gesetzt (${#SUPERVISOR_TOKEN} Zeichen)"
elif [ -n "${HASSIO_TOKEN:-}" ]; then
  echo "HASSIO_TOKEN: gesetzt — verwende als SUPERVISOR_TOKEN"
  export SUPERVISOR_TOKEN="$HASSIO_TOKEN"
else
  echo "WARNUNG: Kein Supervisor-Token verfügbar — BackupPC-Steuerung deaktiviert"
fi

# API starten
exec python3 /api.py
