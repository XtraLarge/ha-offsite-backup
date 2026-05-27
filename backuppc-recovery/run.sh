#!/usr/bin/env bash
set -euo pipefail

CONFIG=/data/options.json
SSHFS_MOUNT=/mnt/hetzner
BACKUPPC_CONF=/etc/backuppc
BACKUPPC_HOME=/home/backuppc
HETZNER_KEY=/data/secrets/id_ed25519_hetzner

HETZNER_USER=$(jq -r '.hetzner_user' "$CONFIG")
HETZNER_HOST=$(jq -r '.hetzner_host' "$CONFIG")
HETZNER_PORT=$(jq -r '.hetzner_port // 23' "$CONFIG")
SNAPSHOT_NAME=$(jq -r '.snapshot_name // ""' "$CONFIG")

# Quellpfad auf der Storage Box bestimmen
if [[ -n "$SNAPSHOT_NAME" ]]; then
  HETZNER_SOURCE="/home/.snapshots/${SNAPSHOT_NAME}/ZPool"
  IMPORT_FLAG="/data/config-imported-v2-$(echo "$SNAPSHOT_NAME" | tr -cd '[:alnum:].-')"
  echo "BackupPC Umgebung startet (Snapshot-Modus)"
  echo "  Hetzner:  ${HETZNER_USER}@${HETZNER_HOST}:${HETZNER_PORT}"
  echo "  Snapshot: ${SNAPSHOT_NAME}"
else
  HETZNER_SOURCE="/home/ZPool"
  IMPORT_FLAG="/data/config-imported-v2"
  echo "BackupPC Umgebung startet (Live-Modus)"
  echo "  Hetzner: ${HETZNER_USER}@${HETZNER_HOST}:${HETZNER_PORT}"
fi

# SSH-Key schreiben
mkdir -p /data/secrets /data/logs
if [[ "$(jq -r '.ssh_key_hetzner // empty' "$CONFIG")" == "" ]]; then
  echo "FEHLER: ssh_key_hetzner nicht konfiguriert" >&2
  exit 1
fi
jq -r '.ssh_key_hetzner' "$CONFIG" > "$HETZNER_KEY"
chmod 600 "$HETZNER_KEY"
if ! head -1 "$HETZNER_KEY" | grep -q 'BEGIN'; then
  echo "FEHLER: SSH-Key-Datei ungültig (kein PEM-Header)" >&2
  exit 1
fi
echo "Hetzner SSH-Key geschrieben ($(wc -l < "$HETZNER_KEY") Zeilen)."

# user_allow_other für SSHFS
grep -q '^user_allow_other' /etc/fuse.conf 2>/dev/null \
    || echo 'user_allow_other' >> /etc/fuse.conf

SSH_OPTS="IdentityFile=${HETZNER_KEY},StrictHostKeyChecking=no,UserKnownHostsFile=/dev/null,GlobalKnownHostsFile=/dev/null,ConnectTimeout=15"

# Hetzner SSHFS mounten
mkdir -p "$SSHFS_MOUNT"
if ! mountpoint -q "$SSHFS_MOUNT"; then
  echo "Mounte SSHFS: ${HETZNER_USER}@${HETZNER_HOST}:${HETZNER_SOURCE} → $SSHFS_MOUNT"
  set +e
  SSHFS_OUT=$(sshfs -p "$HETZNER_PORT" \
    -o "${SSH_OPTS},allow_other" \
    "${HETZNER_USER}@${HETZNER_HOST}:${HETZNER_SOURCE}" "$SSHFS_MOUNT" 2>&1)
  SSHFS_RC=$?
  set -e
  if [[ $SSHFS_RC -ne 0 ]] || ! mountpoint -q "$SSHFS_MOUNT"; then
    echo "FEHLER: SSHFS-Mount fehlgeschlagen (rc=$SSHFS_RC):"
    echo "$SSHFS_OUT"
    exit 1
  fi
  echo "Hetzner gemountet: $SSHFS_MOUNT (${HETZNER_SOURCE})"
fi

# BackupPC-Config übernehmen (einmalig pro Snapshot/Live-Kombination)
if [[ ! -f "$IMPORT_FLAG" ]]; then
  echo "Importiere BackupPC-Config..."
  rm -rf "${BACKUPPC_CONF:?}/"*
  cp -a "${SSHFS_MOUNT}/Docker/backuppc/config/." "$BACKUPPC_CONF/"
  cp -a "${SSHFS_MOUNT}/Docker/backuppc/home/." "$BACKUPPC_HOME/" 2>/dev/null || true
  touch "$IMPORT_FLAG"
  echo "Config importiert."
fi

# TopDir auf den SSHFS-Mount setzen
perl -i -pe "s|^\\\$Conf\{TopDir\}.*|\\\$Conf{TopDir} = '${SSHFS_MOUNT}/BackupPC';|" \
  "${BACKUPPC_CONF}/config.pl" 2>/dev/null || \
  echo "\$Conf{TopDir} = '${SSHFS_MOUNT}/BackupPC';" >> "${BACKUPPC_CONF}/config.pl"

# Neue Sicherungen deaktivieren (letzter Eintrag gewinnt in Perl)
grep -qF 'BackupsDisable' "${BACKUPPC_CONF}/config.pl" \
  || printf '\n$Conf{BackupsDisable} = 2;\n' >> "${BACKUPPC_CONF}/config.pl"

# CgiAdminUsers sicherstellen (Hetzner-Config könnte anderen User enthalten)
perl -i -pe "s/^\\\$Conf\{CgiAdminUsers\}.*$//" \
  "${BACKUPPC_CONF}/config.pl" 2>/dev/null || true
printf '\n$Conf{CgiAdminUsers} = "backuppc";\n' >> "${BACKUPPC_CONF}/config.pl"

# Berechtigungen setzen (UID 1000 = backuppc im adferrand/backuppc Image)
chown -R 1000:1000 "$BACKUPPC_CONF" "$BACKUPPC_HOME" 2>/dev/null || true

# Lighttpd: mod_setenv laden + Auth deaktivieren, REMOTE_USER direkt setzen
# Port 8080 ist nur im lokalen Netz erreichbar — kein Passwort-Schutz nötig.
if ! grep -q 'mod_setenv' /etc/lighttpd/lighttpd.conf 2>/dev/null; then
  sed -i 's/"mod_redirect" )/"mod_redirect", "mod_setenv" )/' \
    /etc/lighttpd/lighttpd.conf 2>/dev/null || true
fi

cat > /etc/lighttpd/auth.conf << 'LHEOF'
# Recovery-Modus: Kein Login erforderlich (lokales Netz, Port 8080)
setenv.add-environment = ("REMOTE_USER" => "backuppc")
LHEOF

# MQTT-State-Publisher starten
python3 /state.py &

echo "Starte BackupPC+lighttpd via supervisord..."
echo "  Web-UI: http://<HA-IP>:8080/BackupPC_Admin"
exec /usr/bin/supervisord -c /etc/supervisord.conf
