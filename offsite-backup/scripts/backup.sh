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
NAS_HOST=$(jq -r '.nas_host' "$OPTIONS_FILE")
NAS_USER=$(jq -r '.nas_user // "root"' "$OPTIONS_FILE")
TARGET_USER=$(jq -r '.hetzner_user' "$OPTIONS_FILE")
TARGET_HOST=$(jq -r '.hetzner_host' "$OPTIONS_FILE")
SSH_PORT=$(jq -r '.hetzner_port // 23' "$OPTIONS_FILE")
STORAGE_BOX_ID=$(jq -r '.hetzner_box_id' "$OPTIONS_FILE")

# Secrets prüfen
for secret in hetzner_token id_ed25519_nas id_ed25519_hetzner; do
  if [[ ! -f "$SECRETS_DIR/$secret" ]]; then
    echo "$(date '+%F %T'): FEHLER: $SECRETS_DIR/$secret fehlt – Setup nötig (siehe DOCS.md)"
    exit 1
  fi
done
chmod 600 "$SECRETS_DIR"/id_ed25519_*

HETZNER_API_TOKEN="$(cat "$SECRETS_DIR/hetzner_token")"
if [[ -z "$HETZNER_API_TOKEN" ]]; then
  echo "$(date '+%F %T'): FEHLER: hetzner_token ist leer"
  exit 1
fi

# SSH-Agent mit Hetzner-Key starten (wird per Agent-Forwarding zur NAS weitergeleitet)
eval "$(ssh-agent -s)"
ssh-add "$SECRETS_DIR/id_ed25519_hetzner"
echo "$(date '+%F %T'): Hetzner SSH-Key in Agent geladen"

echo "$(date '+%F %T'): Starte Backup auf ${NAS_USER}@${NAS_HOST} (Script per Pipe)"

{
  printf 'export HETZNER_API_TOKEN=%q\n'  "$HETZNER_API_TOKEN"
  printf 'export TARGET_USER=%q\n'        "$TARGET_USER"
  printf 'export TARGET_HOST=%q\n'        "$TARGET_HOST"
  printf 'export SSH_PORT=%q\n'           "$SSH_PORT"
  printf 'export STORAGE_BOX_ID=%q\n'     "$STORAGE_BOX_ID"
  printf 'export USE_SSH_PASSWORD=0\n'
  printf 'export RUNNING_IN_SCREEN=1\n'
  printf 'export RSYNC_LOG=/dev/null\n'
  cat "$SCRIPT_DIR/backup_nas.sh"
} | ssh -A \
        -o StrictHostKeyChecking=no \
        -o BatchMode=yes \
        -i "$SECRETS_DIR/id_ed25519_nas" \
        "${NAS_USER}@${NAS_HOST}" \
        "bash -s"

echo "$(date '+%F %T'): ============================================"
echo "$(date '+%F %T'): Offsite Backup erfolgreich abgeschlossen"
echo "$(date '+%F %T'): ============================================"
