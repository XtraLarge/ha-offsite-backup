#!/usr/bin/env bash
# Listet Offsite Storage Box Snapshots und Verzeichnisstruktur.
set -euo pipefail

OPTIONS_FILE="/data/options.json"
SECRETS_DIR="/data/secrets"

TOKEN=$(cat "$SECRETS_DIR/offsite_token" 2>/dev/null) \
  || { echo "FEHLER: $SECRETS_DIR/offsite_token fehlt"; exit 1; }

BOX_ID=$(jq -r '.offsite_box_id' "$OPTIONS_FILE")
OFFSITE_HOST=$(jq -r '.offsite_host' "$OPTIONS_FILE")
OFFSITE_USER=$(jq -r '.offsite_user' "$OPTIONS_FILE")
OFFSITE_PORT=$(jq -r '.offsite_port // 23' "$OPTIONS_FILE")

echo "=== Offsite Storage Box Snapshots (Box ID: $BOX_ID) ==="
curl -sS -H "Authorization: Bearer $TOKEN" \
  "https://api.hetzner.com/v1/storage_boxes/$BOX_ID/snapshots" \
  | python3 -c "
import json, sys
data = json.load(sys.stdin)
snaps = data.get('snapshots', [])
if not snaps:
    print('  (keine Snapshots)')
else:
    for s in snaps:
        print(f\"  {s.get('name','-'):<20} {s.get('created','')[:19]}  {s.get('description','')}\")"

echo ""
echo "=== Backup-Verzeichnisse Offsite ==="
chmod 600 "$SECRETS_DIR/id_ed25519_offsite"
ssh -p "$OFFSITE_PORT" \
    -i "$SECRETS_DIR/id_ed25519_offsite" \
    -o StrictHostKeyChecking=no \
    -o BatchMode=yes \
    -o ConnectTimeout=15 \
    "${OFFSITE_USER}@${OFFSITE_HOST}" \
    'du -sh /home/ZPool/BackupPC /home/ZPool/Docker/backuppc /home/ZPool/Docker/_DockerCreate 2>/dev/null; echo "---"; find /home/ZPool -maxdepth 2 -type d 2>/dev/null | sort' \
  2>/dev/null || echo "(SSH-Verbindung zu Offsite-Host fehlgeschlagen)"
