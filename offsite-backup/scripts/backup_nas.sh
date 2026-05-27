#!/usr/bin/env bash
# Läuft auf der NAS via SSH-Pipe. Alle konfigurierbaren Werte kommen per
# Umgebungsvariable vom HA-Add-on (backup.sh). Defaults sichern Standalone-Betrieb.
set -euo pipefail

DATASET="${DATASET:-ZPool/BackupPC}"
OFFSITE_USER="${OFFSITE_USER:?Offsite-User nicht gesetzt – wird von backup.sh übergeben}"
OFFSITE_HOST="${OFFSITE_HOST:?Offsite-Host nicht gesetzt – wird von backup.sh übergeben}"
OFFSITE_PATH="${OFFSITE_PATH:-/home}"
OFFSITE_PORT="${OFFSITE_PORT:-23}"
OFFSITE_BOX_ID="${OFFSITE_BOX_ID:?Offsite-Box-ID nicht gesetzt – wird von backup.sh übergeben}"
USE_SSH_PASSWORD="${USE_SSH_PASSWORD:-0}"
STATUS_INTERVAL=60
RSYNC_MAX_RETRIES=5
RSYNC_RETRY_SLEEP=120
RSYNC_RETRY_BACKOFF=1
RSYNC_IO_TIMEOUT=600
SSH_CONNECT_TIMEOUT=30
RSYNC_LOG="${RSYNC_LOG:-/tmp/rsync_itemized.log}"
SCREEN_LOG="${SCREEN_LOG:-/tmp/rsync_screen.log}"

is_root() { [[ "${EUID:-$(id -u)}" -eq 0 ]]; }
run_root() { if is_root; then "$@"; else sudo "$@"; fi; }

: > "$RSYNC_LOG"
RSYNC_OPTS=(
  -aHAX --numeric-ids --no-inc-recursive --delete-delay --max-alloc=4G
  --timeout="$RSYNC_IO_TIMEOUT" --info=none --stats
  --log-file="$RSYNC_LOG" --log-file-format="%t %o %i %n%L"
)

SSH_CTL="/tmp/ctl-rsync-offline-%C"
SSH_CMD="ssh -p $OFFSITE_PORT -T -o Compression=no -c aes128-gcm@openssh.com \
  -o ConnectTimeout=$SSH_CONNECT_TIMEOUT \
  -o ServerAliveInterval=60 -o ServerAliveCountMax=10 \
  -o ControlMaster=auto -o ControlPersist=15m -o ControlPath=$SSH_CTL"

reset_ssh_master() {
  ssh -p "$OFFSITE_PORT" -o ControlPath="$SSH_CTL" -O exit "$OFFSITE_USER@$OFFSITE_HOST" >/dev/null 2>&1 || true
}

ensure_packages() {
  local pkgs=(rsync openssh-client curl ca-certificates jq zfsutils-linux)
  local missing=()
  for p in "${pkgs[@]}"; do dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p"); done
  if [[ "${#missing[@]}" -gt 0 ]]; then
    echo "Installiere fehlende Pakete: ${missing[*]}"
    run_root env DEBIAN_FRONTEND=noninteractive apt-get update -y
    run_root env DEBIAN_FRONTEND=noninteractive apt-get install -y "${missing[@]}"
  fi
}

check_offsite_token() {
  local token="$1" box_id="$2"
  local out http body
  out="$(curl -sS -H "Authorization: Bearer $token" \
        "https://api.hetzner.com/v1/storage_boxes/${box_id}" \
        -w $'\n%{http_code}' || true)"
  http="${out##*$'\n'}"; body="${out%$'\n'*}"
  case "$http" in
    200) echo "Offsite Token-Check: OK"; return 0 ;;
    401|403) echo "FEHLER: Offsite API Token ungültig (HTTP $http)"; return 1 ;;
    404) echo "FEHLER: Storage Box ID $box_id nicht gefunden"; return 1 ;;
    *) echo "FEHLER: Token-Check HTTP $http"; return 1 ;;
  esac
}

# Direkt im Script-Kontext starten (RUNNING_IN_SCREEN=1 gesetzt vom HA-Add-on)
if [[ -z "${STY:-}" && -z "${RUNNING_IN_SCREEN:-}" ]]; then
  ensure_packages
  if [[ -z "${OFFSITE_API_TOKEN:-}" ]]; then
    echo "FEHLER: OFFSITE_API_TOKEN nicht gesetzt"; exit 1
  fi
  check_offsite_token "$OFFSITE_API_TOKEN" "$OFFSITE_BOX_ID"
  RUNNING_IN_SCREEN=1 OFFSITE_API_TOKEN="$OFFSITE_API_TOKEN" exec bash "$0" "$@"
fi

ensure_packages

OFFSITE_TOKEN_LOCAL="${OFFSITE_API_TOKEN:-}"
unset OFFSITE_API_TOKEN
readonly OFFSITE_TOKEN_LOCAL

if [[ -z "$OFFSITE_TOKEN_LOCAL" ]]; then
  echo "FEHLER: Offsite Token fehlt"; exit 1
fi

run_rsync() {
  local src="$1" dst="$2"
  local retryable_codes=(10 11 12 30 35 255)
  local attempt=1 max_attempts=$((RSYNC_MAX_RETRIES + 1)) sleep_s="$RSYNC_RETRY_SLEEP"

  while (( attempt <= max_attempts )); do
    echo "$(date '+%F %T'): rsync ${attempt}/${max_attempts}: $src → $dst"
    (while true; do echo "$(date '+%F %T'): läuft: $src"; sleep "$STATUS_INTERVAL"; done) &
    local status_pid=$!
    set +e
    run_root rsync "${RSYNC_OPTS[@]}" -e "$SSH_CMD" "$src" "$dst"
    local rc=$?
    set -e
    kill "$status_pid" >/dev/null 2>&1 || true
    wait "$status_pid" >/dev/null 2>&1 || true

    if [[ "$rc" -eq 0 || "$rc" -eq 23 || "$rc" -eq 24 ]]; then return 0; fi

    local is_retryable=0
    for c in "${retryable_codes[@]}"; do [[ "$rc" -eq "$c" ]] && is_retryable=1; done

    if (( is_retryable == 1 )) && (( attempt < max_attempts )); then
      echo "$(date '+%F %T'): rsync Fehler rc=$rc – Retry in ${sleep_s}s"
      reset_ssh_master; sleep "$sleep_s"
      [[ "$RSYNC_RETRY_BACKOFF" -gt 1 ]] && sleep_s=$(( sleep_s * RSYNC_RETRY_BACKOFF ))
      ((attempt++)); continue
    fi
    echo "$(date '+%F %T'): rsync endgültig fehlgeschlagen rc=$rc"
    return "$rc"
  done
}

# Alte pre_rsync Snapshots löschen
{ run_root zfs list -H -t snapshot -o name -s creation -r "$DATASET" \
    | grep -E "^${DATASET}@pre_rsync" || true; } \
| while IFS= read -r snap; do
    [[ -z "$snap" ]] && continue
    echo "Lösche Snapshot: $snap"; run_root zfs destroy "$snap"
  done

SNAP="pre_rsync_$(date +%F_%H-%M-%S)"
MP="$(zfs get -H -o value mountpoint "$DATASET")"
echo "$(date '+%F %T'): Snapshot erstellen: ${DATASET}@${SNAP}"
run_root zfs snapshot "${DATASET}@${SNAP}"

run_rsync "$MP/.zfs/snapshot/$SNAP/" "${OFFSITE_USER}@${OFFSITE_HOST}:${OFFSITE_PATH}/ZPool/BackupPC/"
echo "$(date '+%F %T'): Snapshot löschen: ${DATASET}@${SNAP}"
run_root zfs destroy "${DATASET}@${SNAP}"

run_rsync "/ZPool/Docker/backuppc/"     "${OFFSITE_USER}@${OFFSITE_HOST}:${OFFSITE_PATH}/ZPool/Docker/backuppc/"
run_rsync "/ZPool/Docker/_DockerCreate/" "${OFFSITE_USER}@${OFFSITE_HOST}:${OFFSITE_PATH}/ZPool/Docker/_DockerCreate/"

create_storagebox_snapshot() {
  local desc="Snap_$(date +%F)"
  echo "$(date '+%F %T'): Erstelle Offsite Snapshot: $desc"
  local resp
  resp="$(curl -sS -X POST \
    -H "Authorization: Bearer $OFFSITE_TOKEN_LOCAL" \
    -H "Content-Type: application/json" \
    -d "{\"description\":\"$desc\"}" \
    "https://api.hetzner.com/v1/storage_boxes/$OFFSITE_BOX_ID/snapshots")"
  local action_id
  action_id="$(jq -r '.action.id // empty' <<<"$resp")"
  if [[ -n "$action_id" ]]; then
    while true; do
      local a status prog
      a="$(curl -sS -H "Authorization: Bearer $OFFSITE_TOKEN_LOCAL" \
          "https://api.hetzner.com/v1/storage_boxes/actions/$action_id")"
      status="$(jq -r '.action.status' <<<"$a")"
      prog="$(jq -r '.action.progress // 0' <<<"$a")"
      echo "$(date '+%F %T'): Snapshot-Status: $status (${prog}%)"
      [[ "$status" == "success" ]] && { echo "$(date '+%F %T'): Offsite Snapshot erstellt."; break; }
      [[ "$status" == "error" ]] && { echo "$(date '+%F %T'): Snapshot fehlgeschlagen"; return 1; }
      sleep 5
    done
  else
    echo "$(date '+%F %T'): Snapshot-Antwort: $resp"
  fi
}
create_storagebox_snapshot
reset_ssh_master
unset OFFSITE_TOKEN_LOCAL
echo "$(date '+%F %T'): Fertig."
