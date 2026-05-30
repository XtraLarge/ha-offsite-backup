#!/usr/bin/env bash
set -euo pipefail

CONFIG=/data/options.json
SSHFS_MOUNT=/mnt/hetzner
BACKUPPC_CONF=/etc/backuppc
OFFSITE_KEY=/data/secrets/id_ed25519_offsite

# ── Adferrand-Umgebung ────────────────────────────────────────────────────────
export BACKUPPC_USERNAME=backuppc
export BACKUPPC_GROUPNAME=backuppc
export BACKUPPC_UUID=1000
export BACKUPPC_GUID=1000

# User/Gruppe anlegen (shadow-Paket, da adferrand 'shadow' installiert)
groupadd -g 1000 backuppc 2>/dev/null || true
useradd -r -d /home/backuppc -g backuppc -u 1000 -s /bin/sh backuppc 2>/dev/null || true

# Verzeichnisse anlegen
mkdir -p /home/backuppc /data/backuppc/log /data/logs /data/secrets \
         /var/log/lighttpd /mnt/hetzner /etc/backuppc
chown -R 1000:1000 /home/backuppc /data/backuppc /var/log/lighttpd 2>/dev/null || true

# ── BackupPC Ersteinrichtung via configure.pl ─────────────────────────────────
# /firstrun wird im Docker-Build gesetzt (adferrand-Mechanismus).
# configure.pl installiert BackupPC nach /usr/local/BackupPC.
if [[ -f /firstrun ]]; then
  echo "Führe BackupPC configure.pl durch (Ersteinrichtung — läuft nur einmal)..."
  BACKUPPC_TAR=$(ls /root/BackupPC-*.tar.gz 2>/dev/null | head -1)
  if [[ -z "$BACKUPPC_TAR" ]]; then
    echo "FEHLER: BackupPC-Quellpaket /root/BackupPC-*.tar.gz nicht gefunden" >&2
    exit 1
  fi
  BACKUPPC_DIR="/root/${BACKUPPC_TAR##*/}"
  BACKUPPC_DIR="${BACKUPPC_DIR%.tar.gz}"
  tar xzf "$BACKUPPC_TAR" -C /root/
  (
    cd "$BACKUPPC_DIR"
    perl configure.pl --batch \
      --config-dir    /etc/backuppc \
      --cgi-dir       /var/www/cgi-bin/BackupPC \
      --data-dir      /data/backuppc \
      --log-dir       /data/backuppc/log \
      --hostname      "$(hostname)" \
      --html-dir      /var/www/html/BackupPC \
      --html-dir-url  /BackupPC \
      --install-dir   /usr/local/BackupPC \
      --backuppc-user backuppc \
      --config-override "CgiAdminUsers=backuppc"
  )
  rm -rf "$BACKUPPC_DIR" "$BACKUPPC_TAR"
  rm -f /firstrun
  chown -R 1000:1000 /usr/local/BackupPC /var/www/cgi-bin/BackupPC \
    /var/www/html/BackupPC /etc/backuppc 2>/dev/null || true
  echo "BackupPC Ersteinrichtung abgeschlossen."
fi

# ── Optionen lesen ────────────────────────────────────────────────────────────
OFFSITE_USER=$(jq -r '.offsite_user' "$CONFIG")
OFFSITE_HOST=$(jq -r '.offsite_host' "$CONFIG")
OFFSITE_PORT=$(jq -r '.offsite_port // 23' "$CONFIG")
SNAPSHOT_NAME=$(jq -r '.snapshot_name // ""' "$CONFIG")
OFFSITE_PATH=$(jq -r '.offsite_path // "/home"' "$CONFIG")

# Quell-Mapping (dieselbe Liste wie das Backup-Add-on). Recovery nutzt davon nur
# dest + recovery-Rolle + container_mount, um sich 1:1 über das Hetzner-Mapping
# zu bedienen. Fehlt die Liste, greift der eingebaute Default (Parität).
SOURCES_JSON=$(jq -c '.backup_sources // []' "$CONFIG")
if ! jq -e 'type=="array" and length>0' >/dev/null 2>&1 <<<"$SOURCES_JSON"; then
  SOURCES_JSON='[
    {"dest":"ZPool/BackupPC","recovery":"topdir","container_mount":"/data/backuppc","recovery_clean":false},
    {"dest":"ZPool/Docker/backuppc/config","recovery":"import","container_mount":"/etc/backuppc","recovery_clean":true},
    {"dest":"ZPool/Docker/backuppc/home","recovery":"import","container_mount":"/home/backuppc","recovery_clean":false},
    {"dest":"ZPool/Docker/backuppc/ssh_config","recovery":"import","container_mount":"/etc/ssh/ssh_config","recovery_clean":false}
  ]'
fi

# Mount-Wurzel = offsite_path (read-only); im Snapshot-Modus zusätzlich
# .snapshots/<name> als Pfad-Präfix. Absolut identisch zum bisherigen Verhalten.
OFFSITE_SOURCE="$OFFSITE_PATH"
if [[ -n "$SNAPSHOT_NAME" ]]; then
  REL_PREFIX=".snapshots/${SNAPSHOT_NAME}"
  echo "BackupPC Umgebung startet (Snapshot-Modus)"
  echo "  Offsite:  ${OFFSITE_USER}@${OFFSITE_HOST}:${OFFSITE_PORT}"
  echo "  Snapshot: ${SNAPSHOT_NAME}"
else
  REL_PREFIX=""
  echo "BackupPC Umgebung startet (Live-Modus)"
  echo "  Offsite: ${OFFSITE_USER}@${OFFSITE_HOST}:${OFFSITE_PORT}"
fi
# Basis innerhalb des SSHFS-Mounts, aus der alle dest-Pfade aufgelöst werden.
BASE="${SSHFS_MOUNT}${REL_PREFIX:+/$REL_PREFIX}"
# TopDir-Quelle aus dem Mapping (Rolle "topdir").
TOPDIR_DEST="$(jq -r '[.[] | select(.recovery=="topdir")][0].dest // "ZPool/BackupPC"' <<<"$SOURCES_JSON")"
TOPDIR_PATH="${BASE}/${TOPDIR_DEST}"

# ── SSH-Key schreiben ─────────────────────────────────────────────────────────
if [[ "$(jq -r '.ssh_key_offsite // empty' "$CONFIG")" == "" ]]; then
  echo "FEHLER: ssh_key_offsite nicht konfiguriert" >&2
  exit 1
fi
# printf '%b\n' konvertiert \n-Literale in echte Newlines (HA-UI speichert Keys einzeilig)
printf '%b\n' "$(jq -r '.ssh_key_offsite' "$CONFIG")" > "$OFFSITE_KEY"
chmod 600 "$OFFSITE_KEY"
if ! head -1 "$OFFSITE_KEY" | grep -q 'BEGIN'; then
  echo "FEHLER: SSH-Key-Datei ungültig (kein PEM-Header)" >&2
  exit 1
fi
echo "Offsite SSH-Key geschrieben ($(wc -l < "$OFFSITE_KEY") Zeilen)."

# ── SSHFS mounten ─────────────────────────────────────────────────────────────
grep -q '^user_allow_other' /etc/fuse.conf 2>/dev/null \
    || echo 'user_allow_other' >> /etc/fuse.conf

SSH_OPTS="IdentityFile=${OFFSITE_KEY},StrictHostKeyChecking=no,UserKnownHostsFile=/dev/null,GlobalKnownHostsFile=/dev/null,ConnectTimeout=15"

if ! mountpoint -q "$SSHFS_MOUNT"; then
  echo "Mounte SSHFS: ${OFFSITE_USER}@${OFFSITE_HOST}:${OFFSITE_SOURCE} → $SSHFS_MOUNT"
  set +e
  SSHFS_OUT=$(sshfs -p "$OFFSITE_PORT" \
    -o "${SSH_OPTS},allow_other,ro" \
    "${OFFSITE_USER}@${OFFSITE_HOST}:${OFFSITE_SOURCE}" "$SSHFS_MOUNT" 2>&1)
  SSHFS_RC=$?
  set -e
  if [[ $SSHFS_RC -ne 0 ]] || ! mountpoint -q "$SSHFS_MOUNT"; then
    echo "FEHLER: SSHFS-Mount fehlgeschlagen (rc=$SSHFS_RC):"
    echo "$SSHFS_OUT"
    exit 1
  fi
  echo "Offsite gemountet: $SSHFS_MOUNT (${OFFSITE_SOURCE})"
fi

# ── Datenstand ────────────────────────────────────────────────────────────────
DATASTAND="unbekannt"
PC_DIR="${TOPDIR_PATH}/pc"
if [[ -d "$PC_DIR" ]]; then
  NEWEST_HOST=$(ls -dt "$PC_DIR"/*/ 2>/dev/null | head -1)
  if [[ -n "$NEWEST_HOST" ]]; then
    DATASTAND=$(date -r "$NEWEST_HOST" '+%d.%m.%Y %H:%M' 2>/dev/null || echo "unbekannt")
  fi
fi
echo "Datenstand: Letztes Host-Backup: $DATASTAND"
echo "$DATASTAND" > /data/datastand

# ── Import-Quellen einspielen (bei jedem Start frisch von SSHFS) ─────────────
# Jeder recovery=="import"-Eintrag wird über sein dest aus dem Hetzner-Mapping
# nach container_mount kopiert. Verzeichnisse rekursiv (mit Inhalt), Dateien
# einzeln (z.B. ssh_config). recovery_clean leert das Ziel vorher (nur Dirs).
echo "Importiere Recovery-Quellen von Offsite..."
NUM_SRC="$(jq 'length' <<<"$SOURCES_JSON")"
for ((i=0; i<NUM_SRC; i++)); do
  ENTRY="$(jq -c ".[$i]" <<<"$SOURCES_JSON")"
  I_ROLE="$(jq -r '.recovery // ""' <<<"$ENTRY")"
  [[ "$I_ROLE" == "import" ]] || continue
  I_DEST="$(jq -r '.dest // ""' <<<"$ENTRY")"
  I_TARGET="$(jq -r '.container_mount // ""' <<<"$ENTRY")"
  I_CLEAN="$(jq -r '.recovery_clean // false' <<<"$ENTRY")"
  [[ -n "$I_DEST" && -n "$I_TARGET" ]] || continue
  SRC_PATH="${BASE}/${I_DEST#/}"
  if [[ -d "$SRC_PATH" ]]; then
    mkdir -p "$I_TARGET"
    if [[ "$I_CLEAN" == "true" ]]; then
      rm -rf "${I_TARGET:?}/"*
      echo "  import (dir):  ${I_DEST} → ${I_TARGET} (clean)"
    else
      echo "  import (dir):  ${I_DEST} → ${I_TARGET}"
    fi
    cp -a "${SRC_PATH}/." "${I_TARGET}/" 2>/dev/null || true
  elif [[ -e "$SRC_PATH" ]]; then
    mkdir -p "$(dirname "$I_TARGET")"
    cp -a "$SRC_PATH" "$I_TARGET" 2>/dev/null || true
    echo "  import (file): ${I_DEST} → ${I_TARGET}"
  else
    echo "  import übersprungen (Quelle fehlt): ${SRC_PATH}"
  fi
done
echo "Recovery-Quellen importiert."

# TopDir setzen (bestehende Einträge entfernen, dann anhängen — wie BackupsDisable)
perl -i -pe "s/^\s*\\\$Conf\{TopDir\}\s*=.*//" \
  "${BACKUPPC_CONF}/config.pl" 2>/dev/null || true
printf '\n$Conf{TopDir} = "%s";\n' "$TOPDIR_PATH" >> "${BACKUPPC_CONF}/config.pl"
echo "TopDir gesetzt auf: ${TOPDIR_PATH}"

# Neue Sicherungen immer deaktivieren (vorhandene Einträge entfernen, dann anhängen)
perl -i -pe "s/^\\\$Conf\{BackupsDisable\}.*$//" \
  "${BACKUPPC_CONF}/config.pl" 2>/dev/null || true
printf '\n$Conf{BackupsDisable} = 2;\n' >> "${BACKUPPC_CONF}/config.pl"

# CgiAdminUsers sicherstellen
perl -i -pe "s/^\\\$Conf\{CgiAdminUsers\}.*$//" \
  "${BACKUPPC_CONF}/config.pl" 2>/dev/null || true
printf '\n$Conf{CgiAdminUsers} = "backuppc";\n' >> "${BACKUPPC_CONF}/config.pl"

# LogDir lokal setzen — verhindert dass Recovery-Logs auf Hetzner (SSHFS) landen
mkdir -p /data/backuppc/log
perl -i -pe "s/^\s*\\\$Conf\{LogDir\}\s*=.*//" \
  "${BACKUPPC_CONF}/config.pl" 2>/dev/null || true
printf '\n$Conf{LogDir} = "/data/backuppc/log";\n' >> "${BACKUPPC_CONF}/config.pl"
echo "LogDir gesetzt auf: /data/backuppc/log (lokal, nicht auf Hetzner)"

# Berechtigungen
chown -R 1000:1000 "$BACKUPPC_CONF" /home/backuppc /data/backuppc 2>/dev/null || true

# ── Lighttpd konfigurieren ────────────────────────────────────────────────────
# mod_setenv laden + Auth deaktivieren + REMOTE_USER direkt setzen
if ! grep -q 'mod_setenv' /etc/lighttpd/lighttpd.conf 2>/dev/null; then
  sed -i 's/"mod_redirect" )/"mod_redirect", "mod_setenv" )/' \
    /etc/lighttpd/lighttpd.conf 2>/dev/null || true
fi
cat > /etc/lighttpd/auth.conf << 'LHEOF'
# Recovery-Modus: Kein Login nötig (nur lokales Netz, Port 8080)
setenv.add-environment = ("REMOTE_USER" => "backuppc")
LHEOF

# msmtp-Logdatei anlegen damit watchmails nicht crasht
touch /var/log/msmtp.log

# ── Dienste starten ───────────────────────────────────────────────────────────
python3 /state.py &

echo "Starte BackupPC+lighttpd via supervisord..."
echo "  Web-UI: http://<HA-IP>:8080/BackupPC_Admin"
exec /usr/bin/supervisord -c /etc/supervisord.conf
