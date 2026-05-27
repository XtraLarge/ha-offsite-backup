#!/usr/bin/env bash
# Läuft auf HA-RPi: lädt Secrets, startet SSH-Agent und streamt backup_nas.sh zur NAS.
set -euo pipefail

SCRIPT_DIR="/scripts"
SECRETS_DIR="/data/secrets"
LOG_FILE="/data/logs/backup.log"
STATUS_FILE="/data/logs/status.json"
OPTIONS_FILE="/data/options.json"

exec >> "$LOG_FILE" 2>&1

echo ""
echo "$(date '+%F %T'): ============================================"
echo "$(date '+%F %T'): Offsite Backup gestartet"
echo "$(date '+%F %T'): ============================================"

_loki_ship() {
  local rc=$?
  local status="success"
  [[ $rc -ne 0 ]] && status="failed"
  ssh-agent -k >/dev/null 2>&1 || true
  LOKI_URL=$(jq -r '.loki_url // ""' "$OPTIONS_FILE")
  [[ -n "$LOKI_URL" ]] && LOKI_URL="$LOKI_URL" bash "$SCRIPT_DIR/loki_ship.sh" "$LOG_FILE" "$status" || true
  printf '{"status":"%s","last_run":"%s"}\n' "$status" "$(date -Iseconds)" > "$STATUS_FILE"
}
trap '_loki_ship' EXIT

# Optionen lesen
ZFS_HOST=$(jq -r '.zfs_storage_host' "$OPTIONS_FILE")
ZFS_USER=$(jq -r '.zfs_storage_user // "root"' "$OPTIONS_FILE")
OFFSITE_USER=$(jq -r '.offsite_user' "$OPTIONS_FILE")
OFFSITE_HOST=$(jq -r '.offsite_host' "$OPTIONS_FILE")
OFFSITE_PORT=$(jq -r '.offsite_port // 23' "$OPTIONS_FILE")
OFFSITE_BOX_ID=$(jq -r '.offsite_box_id' "$OPTIONS_FILE")

# Secrets prüfen
for secret in offsite_token id_ed25519_storage id_ed25519_offsite; do
  if [[ ! -f "$SECRETS_DIR/$secret" ]]; then
    echo "$(date '+%F %T'): FEHLER: $SECRETS_DIR/$secret fehlt – Setup nötig (siehe DOCS.md)"
    exit 1
  fi
done
chmod 600 "$SECRETS_DIR"/id_ed25519_*

OFFSITE_API_TOKEN="$(cat "$SECRETS_DIR/offsite_token")"
if [[ -z "$OFFSITE_API_TOKEN" ]]; then
  echo "$(date '+%F %T'): FEHLER: offsite_token ist leer"
  exit 1
fi

# SSH-Agent mit Offsite-Key starten (wird per Agent-Forwarding zur ZFS-Storage weitergeleitet)
eval "$(ssh-agent -s)"
ssh-add "$SECRETS_DIR/id_ed25519_offsite"
echo "$(date '+%F %T'): Offsite SSH-Key in Agent geladen"

echo "$(date '+%F %T'): Starte Backup auf ${ZFS_USER}@${ZFS_HOST} (Script per Pipe)"

{
  printf 'export OFFSITE_API_TOKEN=%q\n'  "$OFFSITE_API_TOKEN"
  printf 'export OFFSITE_USER=%q\n'       "$OFFSITE_USER"
  printf 'export OFFSITE_HOST=%q\n'       "$OFFSITE_HOST"
  printf 'export OFFSITE_PORT=%q\n'       "$OFFSITE_PORT"
  printf 'export OFFSITE_BOX_ID=%q\n'     "$OFFSITE_BOX_ID"
  printf 'export USE_SSH_PASSWORD=0\n'
  printf 'export RUNNING_IN_SCREEN=1\n'
  printf 'export RSYNC_LOG=/dev/null\n'
  cat "$SCRIPT_DIR/backup_nas.sh"
} | ssh -A \
        -o StrictHostKeyChecking=no \
        -o BatchMode=yes \
        -i "$SECRETS_DIR/id_ed25519_storage" \
        "${ZFS_USER}@${ZFS_HOST}" \
        "bash -s"

echo "$(date '+%F %T'): ============================================"
echo "$(date '+%F %T'): Offsite Backup erfolgreich abgeschlossen"
echo "$(date '+%F %T'): ============================================"
