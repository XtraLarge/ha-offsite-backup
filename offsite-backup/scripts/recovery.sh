#!/usr/bin/env bash
# BackupPC4 Notfall-Recovery.
# Modus "local":  Docker-Socket auf HA-RPi (kein extra Host nötig).
# Modus "remote": SSH zu einem beliebigen Docker-Host.
set -euo pipefail

OPTIONS_FILE="/data/options.json"
SECRETS_DIR="/data/secrets"
RECOVERY_DIR="/data/backuppc-recovery"

HETZNER_USER=$(jq -r '.hetzner_user' "$OPTIONS_FILE")
HETZNER_HOST=$(jq -r '.hetzner_host' "$OPTIONS_FILE")
HETZNER_PORT=$(jq -r '.hetzner_port // 23' "$OPTIONS_FILE")
RECOVERY_TARGET=$(jq -r '.recovery_target // "local"' "$OPTIONS_FILE")

HETZNER_BASE="/home/ZPool"
HETZNER_KEY="$SECRETS_DIR/id_ed25519_hetzner"
RECOVERY_SSH_KEY="$SECRETS_DIR/id_ed25519_recovery"

CONTAINER_NAME="backuppc-recovery"
CONTAINER_IMAGE="adferrand/backuppc:4.4.0-9"
CONTAINER_PORT="8900"
CONTAINER_INT_PORT="8080"
TZ="Europe/Berlin"
SMTP_HOST="srv-smtp.fritz.box"

# Pfade auf Remote-Host (nur im remote-Modus verwendet)
REMOTE_SSHFS_MOUNT="/mnt/hetzner_backuppc"
REMOTE_CONFIG_DIR="/opt/backuppc-recovery"
REMOTE_KEY_PATH="/root/.ssh/id_ed25519_hetzner_recovery"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()   { echo -e "${GREEN}[OK]${NC}    $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()  { echo -e "${RED}[FEHLER]${NC} $*" >&2; exit 1; }

# ---------- Remote-Modus Helpers ----------
rssh() { ssh -i "$RECOVERY_SSH_KEY" -o BatchMode=yes -o ConnectTimeout=10 "root@${TARGET}" "$@"; }

remote_check_target() {
  log "Prüfe SSH-Verbindung zu $TARGET..."
  rssh "echo ok" >/dev/null || err "Kein SSH-Zugang zu $TARGET als root."
  ok "SSH: OK  |  Docker: $(rssh "docker --version")"
}

remote_install_sshfs() {
  rssh "command -v sshfs >/dev/null || (apt-get update -qq && apt-get install -y sshfs fuse3 2>&1 | tail -3)"
  rssh "grep -q '^user_allow_other' /etc/fuse.conf || echo 'user_allow_other' >> /etc/fuse.conf"
  ok "sshfs bereit."
}

remote_copy_hetzner_key() {
  log "Übertrage Hetzner SSH-Key auf $TARGET (temporär)..."
  scp -i "$RECOVERY_SSH_KEY" -q "$HETZNER_KEY" "root@${TARGET}:${REMOTE_KEY_PATH}"
  rssh "chmod 600 $REMOTE_KEY_PATH"
  warn "Nach der Recovery: ssh root@$TARGET 'rm -f $REMOTE_KEY_PATH'"
}

remote_mount_hetzner() {
  rssh "mountpoint -q $REMOTE_SSHFS_MOUNT 2>/dev/null" && { warn "Bereits gemountet."; return; }
  rssh "mkdir -p $REMOTE_SSHFS_MOUNT"
  rssh "sshfs -p $HETZNER_PORT \
    -o IdentityFile=$REMOTE_KEY_PATH,allow_other,reconnect,uid=0,gid=0,StrictHostKeyChecking=no \
    ${HETZNER_USER}@${HETZNER_HOST}:${HETZNER_BASE} $REMOTE_SSHFS_MOUNT"
  rssh "mountpoint -q $REMOTE_SSHFS_MOUNT" || err "SSHFS-Mount fehlgeschlagen."
  ok "SSHFS gemountet."
}

remote_copy_config() {
  rssh "mkdir -p ${REMOTE_CONFIG_DIR}/config ${REMOTE_CONFIG_DIR}/home"
  local cnt; cnt=$(rssh "ls ${REMOTE_CONFIG_DIR}/config/ 2>/dev/null | wc -l")
  if [[ "$cnt" -eq 0 ]]; then
    rssh "cp -a ${REMOTE_SSHFS_MOUNT}/Docker/backuppc/config/. ${REMOTE_CONFIG_DIR}/config/"
    rssh "cp -a ${REMOTE_SSHFS_MOUNT}/Docker/backuppc/home/.   ${REMOTE_CONFIG_DIR}/home/ 2>/dev/null || true"
    rssh "cp    ${REMOTE_SSHFS_MOUNT}/Docker/backuppc/ssh_config ${REMOTE_CONFIG_DIR}/ssh_config"
    rssh "chown -R 1000:1000 ${REMOTE_CONFIG_DIR}/"
    ok "Config-Daten kopiert."
  else
    warn "Config bereits vorhanden ($cnt Einträge) – überspringe."
  fi
}

remote_start_container() {
  local prepared="${CONTAINER_NAME}-prepared"
  if ! rssh "docker images -q $prepared 2>/dev/null" | grep -q .; then
    log "Phase 1: configure.pl auf lok. Verzeichnis (SSHFS unterstützt kein chown)..."
    local init="${CONTAINER_NAME}-init" init_data="/tmp/backuppc-init-$$"
    rssh "mkdir -p $init_data; docker rm -f $init 2>/dev/null || true"
    rssh "docker run -d --name $init \
      -e 'TZ=$TZ' \
      -v '${REMOTE_CONFIG_DIR}/config:/etc/backuppc' \
      -v '${REMOTE_CONFIG_DIR}/home:/home/backuppc' \
      -v '${REMOTE_CONFIG_DIR}/ssh_config:/etc/ssh/ssh_config' \
      -v '${init_data}:/data/backuppc' $CONTAINER_IMAGE"
    for i in $(seq 1 24); do
      sleep 5
      rssh "docker exec $init test -f /firstrun" 2>/dev/null || { ok "configure.pl fertig."; break; }
      [[ "$i" -eq 24 ]] && { rssh "docker logs --tail 20 $init; docker stop $init; docker rm $init" || true; err "configure.pl Timeout."; }
    done
    rssh "docker commit $init $prepared && docker stop $init && docker rm $init && rm -rf $init_data"
    ok "Vorbereitetes Image: $prepared"
  fi
  log "Phase 2: Recovery-Container starten..."
  rssh "docker run -d --name $CONTAINER_NAME -h $CONTAINER_NAME --restart=unless-stopped \
    -p ${CONTAINER_PORT}:${CONTAINER_INT_PORT} -e 'TZ=$TZ' \
    -v '${REMOTE_CONFIG_DIR}/config:/etc/backuppc' \
    -v '${REMOTE_CONFIG_DIR}/home:/home/backuppc' \
    -v '${REMOTE_CONFIG_DIR}/ssh_config:/etc/ssh/ssh_config' \
    -v '${REMOTE_SSHFS_MOUNT}/BackupPC:/data/backuppc' $prepared"
  for i in $(seq 1 12); do
    sleep 5
    rssh "docker ps --filter name=^${CONTAINER_NAME}$ --format '{{.Status}}'" | grep -q "Up" && { ok "Container läuft."; return; }
  done
  err "Container startet nicht."
}

remote_stop() {
  log "Beende Recovery auf $TARGET..."
  rssh "docker stop $CONTAINER_NAME 2>/dev/null; docker rm $CONTAINER_NAME 2>/dev/null" || true
  rssh "docker rmi ${CONTAINER_NAME}-prepared $CONTAINER_IMAGE 2>/dev/null" || true
  if rssh "mountpoint -q $REMOTE_SSHFS_MOUNT 2>/dev/null"; then
    rssh "umount $REMOTE_SSHFS_MOUNT || fusermount3 -u $REMOTE_SSHFS_MOUNT || fusermount -u $REMOTE_SSHFS_MOUNT" || warn "SSHFS-Unmount fehlgeschlagen"
    rssh "rmdir $REMOTE_SSHFS_MOUNT 2>/dev/null" || true
  fi
  rssh "rm -f $REMOTE_KEY_PATH && rm -rf $REMOTE_CONFIG_DIR" || true
  rssh "sed -i '/^user_allow_other$/d' /etc/fuse.conf" || true
  ok "Remote aufgeräumt."
}

# ---------- Local-Modus Helpers ----------
# Docker läuft auf dem HA-Host (HAOS hat keinen dockerd in Add-ons erreichbar).
# Das Add-on SSHt zum Host (172.30.32.1:22222) und führt Docker dort aus.
# Add-on-Daten liegen auf dem Host unter HOST_DATA_DIR.
LOCAL_SSHFS_MOUNT="/data/backuppc-recovery/hetzner"
LOCAL_CONFIG_DIR="/data/backuppc-recovery"
HOST_DATA_DIR="/mnt/data/supervisor/addons/data/3e98a749_offsite_backup"
HOST_SSH_KEY="/data/secrets/id_recovery_host"
HOST_IP="172.30.32.1"
HOST_PORT="22222"

_hssh() {
  ssh -p "$HOST_PORT" -i "$HOST_SSH_KEY" \
    -o BatchMode=yes -o StrictHostKeyChecking=no \
    "root@${HOST_IP}" "$@"
}

local_ensure_host_key() {
  if [[ ! -f "$HOST_SSH_KEY" ]]; then
    ssh-keygen -t ed25519 -f "$HOST_SSH_KEY" -N "" -C "recovery-host" >/dev/null
    chmod 600 "$HOST_SSH_KEY"
    local pub; pub=$(cat "${HOST_SSH_KEY}.pub")
    _hssh "grep -qF 'recovery-host' /root/.ssh/authorized_keys 2>/dev/null || echo '$pub' >> /root/.ssh/authorized_keys" 2>/dev/null \
      || warn "Host-Key-Eintrag fehlgeschlagen – bitte manuell: echo '$pub' >> /root/.ssh/authorized_keys (Port 22222)"
    ok "Host SSH-Key eingerichtet."
  fi
  _hssh "echo ok" >/dev/null || err "Kein SSH-Zugang zum Host (${HOST_IP}:${HOST_PORT})."
}

local_mount_hetzner() {
  mkdir -p "$LOCAL_SSHFS_MOUNT"
  if mountpoint -q "$LOCAL_SSHFS_MOUNT" 2>/dev/null; then
    warn "Hetzner bereits gemountet."; return
  fi
  chmod 600 "$HETZNER_KEY"
  sshfs -p "$HETZNER_PORT" \
    -o "IdentityFile=$HETZNER_KEY,allow_other,reconnect,uid=0,gid=0,StrictHostKeyChecking=no" \
    "${HETZNER_USER}@${HETZNER_HOST}:${HETZNER_BASE}" "$LOCAL_SSHFS_MOUNT"
  mountpoint -q "$LOCAL_SSHFS_MOUNT" || err "Lokaler SSHFS-Mount fehlgeschlagen."
  ok "Hetzner gemountet unter $LOCAL_SSHFS_MOUNT"
}

local_copy_config() {
  mkdir -p "${LOCAL_CONFIG_DIR}/config" "${LOCAL_CONFIG_DIR}/home"
  local cnt; cnt=$(ls "${LOCAL_CONFIG_DIR}/config/" 2>/dev/null | wc -l)
  if [[ "$cnt" -eq 0 ]]; then
    cp -a "${LOCAL_SSHFS_MOUNT}/Docker/backuppc/config/." "${LOCAL_CONFIG_DIR}/config/"
    cp -a "${LOCAL_SSHFS_MOUNT}/Docker/backuppc/home/."   "${LOCAL_CONFIG_DIR}/home/" 2>/dev/null || true
    cp    "${LOCAL_SSHFS_MOUNT}/Docker/backuppc/ssh_config" "${LOCAL_CONFIG_DIR}/ssh_config" 2>/dev/null || true
    chown -R 1000:1000 "${LOCAL_CONFIG_DIR}/config" "${LOCAL_CONFIG_DIR}/home" 2>/dev/null || true
    ok "Config lokal kopiert."
  else
    warn "Config bereits vorhanden – überspringe."
  fi
}

local_disable_backups() {
  local config="${LOCAL_CONFIG_DIR}/config/config.pl"
  grep -qF 'Recovery-Modus' "$config" 2>/dev/null || {
    printf '\n$Conf{BackupsDisable} = 2;  # Recovery-Modus\n' >> "$config"
    ok "BackupsDisable = 2 gesetzt."
  }
}

local_start_container() {
  local prepared="${CONTAINER_NAME}-prepared"
  local h_config="${HOST_DATA_DIR}/backuppc-recovery/config"
  local h_home="${HOST_DATA_DIR}/backuppc-recovery/home"
  local h_ssh="${HOST_DATA_DIR}/backuppc-recovery/ssh_config"
  local h_hetzner="${HOST_DATA_DIR}/backuppc-recovery/hetzner/BackupPC"
  local h_key="${HOST_DATA_DIR}/secrets/id_ed25519_hetzner"

  if ! _hssh "docker images -q $prepared 2>/dev/null" | grep -q .; then
    log "Phase 1: configure.pl (Host-Docker, tmpfs)..."
    local init="${CONTAINER_NAME}-init"
    local init_data; init_data=$(_hssh "mktemp -d")
    _hssh "docker rm -f $init 2>/dev/null || true"
    _hssh "docker run -d --name $init \
      -e 'TZ=$TZ' \
      -v '${h_config}:/etc/backuppc' \
      -v '${h_home}:/home/backuppc' \
      -v '${init_data}:/data/backuppc' \
      $CONTAINER_IMAGE"
    for i in $(seq 1 24); do
      sleep 5
      _hssh "docker exec $init test -f /firstrun 2>/dev/null" || { ok "configure.pl fertig."; break; }
      [[ "$i" -eq 24 ]] && { _hssh "docker logs --tail 20 $init; docker stop $init; docker rm $init; rm -rf $init_data"; err "configure.pl Timeout."; }
    done
    _hssh "docker commit $init $prepared && docker stop $init && docker rm $init && rm -rf $init_data"
    ok "Vorbereitetes Image: $prepared"
  fi

  log "Phase 2: Recovery-Container auf Host starten (Port $CONTAINER_PORT)..."
  _hssh "docker run -d --name $CONTAINER_NAME -h $CONTAINER_NAME --restart=unless-stopped \
    -p ${CONTAINER_PORT}:${CONTAINER_INT_PORT} \
    -e 'TZ=$TZ' \
    --device /dev/fuse --cap-add SYS_ADMIN --security-opt apparmor=unconfined \
    -v '${h_config}:/etc/backuppc' \
    -v '${h_home}:/home/backuppc' \
    -v '${h_hetzner}:/data/backuppc' \
    $prepared"
  for i in $(seq 1 12); do
    sleep 5
    _hssh "docker ps --filter name=^${CONTAINER_NAME}$ --format '{{.Status}}'" | grep -q "Up" && { ok "Container läuft."; return; }
  done
  err "Container startet nicht nach 60s."
}

local_stop() {
  log "Beende Recovery-Container auf Host..."
  _hssh "docker stop $CONTAINER_NAME 2>/dev/null; docker rm $CONTAINER_NAME 2>/dev/null" || true
  _hssh "docker rmi ${CONTAINER_NAME}-prepared $CONTAINER_IMAGE 2>/dev/null" || true
  if mountpoint -q "$LOCAL_SSHFS_MOUNT" 2>/dev/null; then
    fusermount3 -u "$LOCAL_SSHFS_MOUNT" 2>/dev/null \
      || fusermount -u "$LOCAL_SSHFS_MOUNT" 2>/dev/null \
      || umount "$LOCAL_SSHFS_MOUNT" || warn "SSHFS-Unmount fehlgeschlagen"
  fi
  ok "Recovery aufgeräumt."
}

local_print_summary() {
  echo ""
  echo "============================================================"
  ok "Recovery-Container läuft lokal auf HA-RPi!"
  echo ""
  echo "  BackupPC-UI: http://<HA-IP>:${CONTAINER_PORT}"
  echo "  Daten:       Hetzner SSHFS unter $LOCAL_SSHFS_MOUNT"
  echo ""
  echo "  Recovery beenden:"
  echo "    bash /scripts/recovery.sh --stop"
  echo "============================================================"
}

remote_print_summary() {
  local ip; ip=$(rssh "ip -4 addr show | grep 'inet ' | grep -v '127.0.0.1' | awk '{print \$2}' | cut -d/ -f1 | head -1" 2>/dev/null || echo "$TARGET")
  echo ""
  echo "============================================================"
  ok "Recovery-Container läuft auf $TARGET"
  echo "  BackupPC-UI: http://${ip}:${CONTAINER_PORT}"
  echo "  Recovery beenden: bash /scripts/recovery.sh --stop $TARGET"
  echo "============================================================"
}

# ---------- Hauptprogramm ----------
MODE=""
case "${1:-}" in
  --start) MODE="start"; shift ;;
  --stop)  MODE="stop";  shift ;;
  --*)     err "Unbekannte Option: $1. Gültig: --start | --stop" ;;
  *)
    echo "1) --start  Recovery starten"
    echo "2) --stop   Recovery beenden"
    read -r -p "Auswahl [1/2]: " CHOICE
    case "$CHOICE" in 1|start) MODE="start" ;; 2|stop) MODE="stop" ;; *) err "Ungültig." ;; esac ;;
esac

TARGET="${1:-$RECOVERY_TARGET}"

echo "Modus: $MODE  |  Ziel: $TARGET"
echo ""

if [[ "$TARGET" == "local" ]]; then
  case "$MODE" in
    start)
      [[ ! -f "$HETZNER_KEY" ]] && err "$HETZNER_KEY fehlt – Secrets einrichten (siehe DOCS.md)"
      local_ensure_host_key
      local_mount_hetzner
      local_copy_config
      local_disable_backups
      local_start_container
      local_print_summary
      ;;
    stop)
      local_stop
      ;;
  esac
else
  [[ ! -f "$RECOVERY_SSH_KEY" ]] && err "$RECOVERY_SSH_KEY fehlt – Secrets einrichten (siehe DOCS.md)"
  case "$MODE" in
    start)
      remote_check_target
      remote_install_sshfs
      remote_copy_hetzner_key
      remote_mount_hetzner
      remote_copy_config
      remote_start_container
      remote_print_summary
      ;;
    stop)
      remote_check_target
      remote_stop
      ;;
  esac
fi
