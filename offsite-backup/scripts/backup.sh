#!/usr/bin/env bash
# Läuft auf HA-RPi. Startet das eigentliche Backup in einer detached screen-
# Session AUF DER NAS und verfolgt nur deren Log. Dadurch läuft das Backup
# unabhängig von dieser SSH-Pipe weiter, falls das Add-on neu startet oder das
# Netzwerk zwischen RPi und NAS wackelt. Die Offsite-Auth (Key + Token) wird per
# stdin (nie als Argument -> nicht in ps sichtbar) übertragen und auf der NAS
# ausschließlich im RAM gehalten (ssh-agent + tmpfs), siehe nas_bootstrap.sh.
set -euo pipefail

SCRIPT_DIR="/scripts"
SECRETS_DIR="/data/secrets"
LOG_FILE="/data/logs/backup.log"
STATUS_FILE="/data/logs/status.json"
OPTIONS_FILE="/data/options.json"

SCREEN_NAME="offsite-backup"
REMOTE_RUNDIR="/dev/shm/offsite-backup"

exec >> "$LOG_FILE" 2>&1

echo ""
echo "$(date '+%F %T'): ============================================"
echo "$(date '+%F %T'): Offsite Backup gestartet (screen-on-NAS)"
echo "$(date '+%F %T'): ============================================"

_finalize() {
  local rc=$?
  local status="success"
  [[ $rc -ne 0 ]] && status="failed"
  LOKI_URL=$(jq -r '.loki_url // ""' "$OPTIONS_FILE")
  [[ -n "$LOKI_URL" ]] && LOKI_URL="$LOKI_URL" bash "$SCRIPT_DIR/loki_ship.sh" "$LOG_FILE" "$status" || true
  printf '{"status":"%s","last_run":"%s"}\n' "$status" "$(date -Iseconds)" > "$STATUS_FILE"
}
trap '_finalize' EXIT

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

if [[ ! -s "$SECRETS_DIR/offsite_token" ]]; then
  echo "$(date '+%F %T'): FEHLER: offsite_token ist leer"
  exit 1
fi

SSH_OPTS=(
  -o StrictHostKeyChecking=no
  -o BatchMode=yes
  -o ConnectTimeout=30
  -o ServerAliveInterval=30
  -o ServerAliveCountMax=10
  -i "$SECRETS_DIR/id_ed25519_storage"
)
NAS_TARGET="${ZFS_USER}@${ZFS_HOST}"

# base64 vermeidet jegliche Quoting-Probleme im Launcher-Heredoc und enthält
# selbst keine Sonderzeichen (nur [A-Za-z0-9+/=]).
NAS_B64=$(base64 -w0 < "$SCRIPT_DIR/backup_nas.sh")
BOOT_B64=$(base64 -w0 < "$SCRIPT_DIR/nas_bootstrap.sh")
KEY_B64=$(base64 -w0 < "$SECRETS_DIR/id_ed25519_offsite")
TOKEN_B64=$(base64 -w0 < "$SECRETS_DIR/offsite_token")

echo "$(date '+%F %T'): Starte Backup in screen-Session '$SCREEN_NAME' auf $NAS_TARGET"

# --- Launcher auf der NAS: legt /dev/shm-RunDir an, dekodiert Skripte/Secrets,
#     stellt screen sicher und startet die detached Session. ---
# Achtung: lokale ${VARS} werden hier expandiert, remote-$ als \$ geschützt.
launch_out=$(ssh "${SSH_OPTS[@]}" "$NAS_TARGET" "bash -s" <<LAUNCHER || true
set -euo pipefail
umask 077
SCREEN_NAME='${SCREEN_NAME}'
RUNDIR='${REMOTE_RUNDIR}'

if ! command -v screen >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y >/dev/null 2>&1 || true
  apt-get install -y screen >/dev/null 2>&1 || true
fi

sessions="\$(screen -ls 2>/dev/null || true)"
if printf '%s' "\$sessions" | grep -q "\$SCREEN_NAME"; then
  echo "__ALREADY_RUNNING__"
  exit 0
fi

rm -rf "\$RUNDIR"
mkdir -p "\$RUNDIR"
chmod 700 "\$RUNDIR"

base64 -d > "\$RUNDIR/backup_nas.sh" <<'B_NAS'
${NAS_B64}
B_NAS
base64 -d > "\$RUNDIR/nas_bootstrap.sh" <<'B_BOOT'
${BOOT_B64}
B_BOOT
base64 -d > "\$RUNDIR/offsite_key" <<'B_KEY'
${KEY_B64}
B_KEY
chmod 600 "\$RUNDIR/offsite_key"
printf '%s' '${TOKEN_B64}' > "\$RUNDIR/token"
: > "\$RUNDIR/run.log"

export RUNDIR
export OFFSITE_USER='${OFFSITE_USER}'
export OFFSITE_HOST='${OFFSITE_HOST}'
export OFFSITE_PORT='${OFFSITE_PORT}'
export OFFSITE_BOX_ID='${OFFSITE_BOX_ID}'
screen -dmS "\$SCREEN_NAME" bash "\$RUNDIR/nas_bootstrap.sh"
sleep 1
sessions="\$(screen -ls 2>/dev/null || true)"
if printf '%s' "\$sessions" | grep -q "\$SCREEN_NAME"; then
  echo "__STARTED__"
else
  echo "__START_FAILED__"
fi
LAUNCHER
)

echo "$launch_out"
case "$launch_out" in
  *__ALREADY_RUNNING__*)
    echo "$(date '+%F %T'): Es läuft bereits ein Backup auf der NAS – breche ab."
    exit 1
    ;;
  *__STARTED__*)
    echo "$(date '+%F %T'): screen-Session gestartet – verfolge Log."
    ;;
  *)
    echo "$(date '+%F %T'): FEHLER: screen-Session konnte nicht gestartet werden."
    exit 1
    ;;
esac

# Log der laufenden Session in das lokale LOG_FILE spiegeln, solange die Session
# lebt. Endet die SSH-Verbindung (Netzwerk/Container-Neustart), läuft das Backup
# auf der NAS einfach weiter; dieses Skript endet dann nur das Mitschreiben.
ssh "${SSH_OPTS[@]}" "$NAS_TARGET" "bash -s" <<TAILER || true
tail -n +1 -F '${REMOTE_RUNDIR}/run.log' 2>/dev/null &
tpid=\$!
while screen -ls 2>/dev/null | grep -q '${SCREEN_NAME}'; do sleep 5; done
sleep 2
kill \$tpid 2>/dev/null || true
TAILER

# Exit-Code der NAS-Session abholen (von nas_bootstrap.sh geschrieben).
# Hinweis: Der Storage-Key ist auf `command="bash -s"` festgenagelt, daher
# müssen Befehle über stdin kommen (Argument-Befehle würden ignoriert).
RC=$(echo "cat '${REMOTE_RUNDIR}/exit_code' 2>/dev/null" | ssh "${SSH_OPTS[@]}" "$NAS_TARGET" || true)
RC="${RC//[^0-9]/}"

# RunDir NICHT hier löschen: das vollständige run.log wird nach Abschluss vom
# Add-on (api.py _finalize_from_nas) von der NAS geholt und persistent nach
# /data/logs/runs/ archiviert; erst danach räumt der Finalizer das tmpfs-RunDir
# auf. So bleibt das Log auch dann erhalten, wenn dieser Launcher mitten im Lauf
# durch einen Container-Neustart stirbt.

if [[ "$RC" == "0" ]]; then
  echo "$(date '+%F %T'): ============================================"
  echo "$(date '+%F %T'): Offsite Backup erfolgreich abgeschlossen"
  echo "$(date '+%F %T'): ============================================"
  exit 0
else
  echo "$(date '+%F %T'): ============================================"
  echo "$(date '+%F %T'): Offsite Backup fehlgeschlagen oder abgebrochen (rc=${RC:-unbekannt})"
  echo "$(date '+%F %T'): ============================================"
  exit 1
fi
