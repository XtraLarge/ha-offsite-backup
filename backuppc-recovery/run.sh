#!/usr/bin/env bash
set -euo pipefail

CONFIG=/data/options.json
SSHFS_MOUNT=/mnt/hetzner
BACKUPPC_CONF=/etc/backuppc
BACKUPPC_HOME=/var/lib/backuppc
HETZNER_KEY=/data/secrets/id_ed25519_hetzner

HETZNER_USER=$(jq -r '.hetzner_user' "$CONFIG")
HETZNER_HOST=$(jq -r '.hetzner_host' "$CONFIG")
HETZNER_PORT=$(jq -r '.hetzner_port // 23' "$CONFIG")

echo "BackupPC Recovery startet"
echo "  Hetzner: ${HETZNER_USER}@${HETZNER_HOST}:${HETZNER_PORT}"

# SSH-Key schreiben
mkdir -p /data/secrets
val=$(jq -r '.ssh_key_hetzner // empty' "$CONFIG")
if [[ -z "$val" ]]; then
  echo "FEHLER: ssh_key_hetzner nicht konfiguriert" >&2
  exit 1
fi
printf '%b' "$val" > "$HETZNER_KEY"
chmod 600 "$HETZNER_KEY"
echo "Hetzner SSH-Key geschrieben."

# user_allow_other für SSHFS
grep -q '^user_allow_other' /etc/fuse.conf 2>/dev/null \
    || echo 'user_allow_other' >> /etc/fuse.conf

# Hetzner SSHFS mounten
mkdir -p "$SSHFS_MOUNT"
if ! mountpoint -q "$SSHFS_MOUNT"; then
  sshfs -p "$HETZNER_PORT" \
    -o "IdentityFile=${HETZNER_KEY},allow_other,reconnect,uid=0,gid=0,StrictHostKeyChecking=no" \
    "${HETZNER_USER}@${HETZNER_HOST}:/home/ZPool" "$SSHFS_MOUNT"
  mountpoint -q "$SSHFS_MOUNT" || { echo "FEHLER: SSHFS-Mount fehlgeschlagen" >&2; exit 1; }
  echo "Hetzner gemountet: $SSHFS_MOUNT"
fi

# BackupPC-Config von Hetzner übernehmen (einmalig)
if [[ ! -f /data/config-imported ]]; then
  echo "Importiere BackupPC-Config von Hetzner..."
  cp -a "${SSHFS_MOUNT}/Docker/backuppc/config/." "$BACKUPPC_CONF/"
  cp -a "${SSHFS_MOUNT}/Docker/backuppc/home/." "$BACKUPPC_HOME/" 2>/dev/null || true
  # TopDir auf den SSHFS-Mount setzen
  perl -i -pe "s|^\\\$Conf\{TopDir\}.*|\\\$Conf{TopDir} = '${SSHFS_MOUNT}/BackupPC';|" \
    "${BACKUPPC_CONF}/config.pl" 2>/dev/null || \
    echo "\$Conf{TopDir} = '${SSHFS_MOUNT}/BackupPC';" >> "${BACKUPPC_CONF}/config.pl"
  # Neue Sicherungen deaktivieren (Recovery-Modus)
  grep -qF 'BackupsDisable' "${BACKUPPC_CONF}/config.pl" \
    || printf '\n$Conf{BackupsDisable} = 2;  # Recovery-Modus\n' >> "${BACKUPPC_CONF}/config.pl"
  touch /data/config-imported
  echo "Config importiert."
fi

# Berechtigungen
chown -R backuppc:backuppc "$BACKUPPC_CONF" "$BACKUPPC_HOME" 2>/dev/null || true

# Apache starten
echo "Starte Apache (Port 8900)..."
apache2ctl start 2>&1 || true

# BackupPC-Daemon starten
echo "Starte BackupPC-Daemon..."
sudo -u backuppc /usr/share/backuppc/bin/BackupPC -d >> /data/logs/backuppc.log 2>&1 &

echo "BackupPC Recovery läuft."
echo "  Web-UI: http://<HA-IP>:8900/BackupPC/"

# MQTT-State publizieren
python3 /state.py &

# Prozesse am Leben halten
wait
