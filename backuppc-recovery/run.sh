#!/usr/bin/with-contenv bash
set -euo pipefail

CONFIG=/data/options.json
SSHFS_MOUNT=/mnt/hetzner
BACKUPPC_CONF=/etc/backuppc
BACKUPPC_HOME=/var/lib/backuppc
HETZNER_KEY=/data/secrets/id_ed25519_hetzner

HETZNER_USER=$(jq -r '.hetzner_user' "$CONFIG")
HETZNER_HOST=$(jq -r '.hetzner_host' "$CONFIG")
HETZNER_PORT=$(jq -r '.hetzner_port // 23' "$CONFIG")
SNAPSHOT_NAME=$(jq -r '.snapshot_name // ""' "$CONFIG")

# Quellpfad auf der Storage Box bestimmen
if [[ -n "$SNAPSHOT_NAME" ]]; then
  HETZNER_SOURCE="/home/.snapshots/${SNAPSHOT_NAME}/ZPool"
  IMPORT_FLAG="/data/config-imported-$(echo "$SNAPSHOT_NAME" | tr -cd '[:alnum:].-')"
  echo "BackupPC Umgebung startet (Snapshot-Modus)"
  echo "  Hetzner:  ${HETZNER_USER}@${HETZNER_HOST}:${HETZNER_PORT}"
  echo "  Snapshot: ${SNAPSHOT_NAME}"
else
  HETZNER_SOURCE="/home/ZPool"
  IMPORT_FLAG="/data/config-imported"
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
  echo "FUSE-Check: /dev/fuse = $(ls -la /dev/fuse 2>&1)"
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
  # Container-Defaults entfernen (können Typ-Konflikte mit Hetzner-Verzeichnisstruktur verursachen)
  rm -rf "${BACKUPPC_CONF:?}/"*
  cp -a "${SSHFS_MOUNT}/Docker/backuppc/config/." "$BACKUPPC_CONF/"
  cp -a "${SSHFS_MOUNT}/Docker/backuppc/home/." "$BACKUPPC_HOME/" 2>/dev/null || true
  # TopDir auf den SSHFS-Mount setzen
  perl -i -pe "s|^\\\$Conf\{TopDir\}.*|\\\$Conf{TopDir} = '${SSHFS_MOUNT}/BackupPC';|" \
    "${BACKUPPC_CONF}/config.pl" 2>/dev/null || \
    echo "\$Conf{TopDir} = '${SSHFS_MOUNT}/BackupPC';" >> "${BACKUPPC_CONF}/config.pl"
  # Neue Sicherungen deaktivieren
  grep -qF 'BackupsDisable' "${BACKUPPC_CONF}/config.pl" \
    || printf '\n$Conf{BackupsDisable} = 2;\n' >> "${BACKUPPC_CONF}/config.pl"
  touch "$IMPORT_FLAG"
  echo "Config importiert."
fi

# Berechtigungen
chown -R backuppc:backuppc "$BACKUPPC_CONF" "$BACKUPPC_HOME" 2>/dev/null || true

# CgiAdminUsers sicherstellen (Hetzner-Config könnte anderen User haben)
perl -i -pe "s/^\\\$Conf\{CgiAdminUsers\}.*$//" \
  "${BACKUPPC_CONF}/config.pl" 2>/dev/null || true
printf '\n$Conf{CgiAdminUsers} = "backuppc";\n' >> "${BACKUPPC_CONF}/config.pl"

# Apache BackupPC-Route konfigurieren (a2enconf backuppc schlägt im Build fehl)
# Kein Passwort-Schutz: Port 8900 ist nur im lokalen Netz erreichbar.
# SetEnv REMOTE_USER=backuppc setzt den Admin-User direkt im CGI-Environment.
cat > /etc/apache2/conf-available/backuppc-recovery.conf << 'APACHEEOF'
ScriptAlias /BackupPC /usr/share/backuppc/cgi-bin/BackupPC_Admin

<Directory /usr/share/backuppc/cgi-bin>
    Options ExecCGI FollowSymlinks
    AllowOverride None
    Require all granted
</Directory>

Alias /BackupPC-static /usr/share/backuppc/html
<Directory /usr/share/backuppc/html>
    Options None
    AllowOverride None
    Require all granted
</Directory>

SetEnv REMOTE_USER backuppc
APACHEEOF

a2enconf backuppc-recovery 2>/dev/null || true
a2enmod cgi 2>/dev/null || true

# Apache starten
echo "Starte Apache (Port 8900)..."
apache2ctl start 2>&1 || true

# BackupPC-Daemon starten
echo "Starte BackupPC-Daemon..."
sudo -u backuppc /usr/share/backuppc/bin/BackupPC -d >> /data/logs/backuppc.log 2>&1 &

echo "BackupPC Umgebung läuft."
echo "  Web-UI: http://<HA-IP>:8900/BackupPC/"

# MQTT-State publizieren
python3 /state.py &

# Prozesse am Leben halten
wait
