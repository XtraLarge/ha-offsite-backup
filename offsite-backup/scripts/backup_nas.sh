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
  -aHAX -W --numeric-ids --delete-delay --max-alloc=4G
  --timeout="$RSYNC_IO_TIMEOUT" --info=none --stats
  --log-file="$RSYNC_LOG" --log-file-format="%t %o %i %n%L"
)
RSYNC_PARALLEL_JOBS="${RSYNC_PARALLEL_JOBS:-6}"

SSH_CTL="/tmp/ctl-rsync-offline-%C"
SSH_CMD="ssh -p $OFFSITE_PORT -T -o Compression=no -c aes128-gcm@openssh.com \
  -o ConnectTimeout=$SSH_CONNECT_TIMEOUT \
  -o ServerAliveInterval=60 -o ServerAliveCountMax=10 \
  -o ControlMaster=auto -o ControlPersist=15m -o ControlPath=$SSH_CTL"

# Eigene SSH-Verbindung pro Parallel-Shard (kein gemeinsamer Master): jede
# Verbindung bekommt ihr eigenes TCP-/Congestion-Window und eine eigene CPU für
# die Verschlüsselung – das erhöht den aggregierten Durchsatz bei vielen Streams.
SSH_CMD_NOCTL="ssh -p $OFFSITE_PORT -T -o Compression=no -c aes128-gcm@openssh.com \
  -o ConnectTimeout=$SSH_CONNECT_TIMEOUT \
  -o ServerAliveInterval=60 -o ServerAliveCountMax=10"

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

# Auth (ssh-agent + OFFSITE_API_TOKEN) wird vom nas_bootstrap.sh in der
# screen-Session bereitgestellt und an diesen Prozess vererbt. Hier nur prüfen.
ensure_packages

if [[ -z "${OFFSITE_API_TOKEN:-}" ]]; then
  echo "FEHLER: OFFSITE_API_TOKEN nicht gesetzt"; exit 1
fi
check_offsite_token "$OFFSITE_API_TOKEN" "$OFFSITE_BOX_ID"

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
    run_root rsync "${RSYNC_OPTS[@]}" -e "${RSYNC_SSH:-$SSH_CMD}" "$src" "$dst"
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

# Großen Baum (BackupPC-Pool) parallel übertragen. Aufteilung in Shards =
# Verzeichnisse auf Tiefe 2 (z. B. pc/<host>, cpool/<hex>). Bei BackupPC v4 ist
# der Pool inhaltsadressiert (jede Datei liegt an genau einem Pfad, kein
# Hardlink zwischen Pool-Dateien) und pc/-Bäume referenzieren den Pool über
# Digests in attrib-Dateien statt über FS-Hardlinks – daher ist Sharding
# verlustfrei: jede Datei landet in genau einem Shard, --delete je Shard ist
# korrekt, und es brechen keine für die Wiederherstellung nötigen Hardlinks.
# $1 = lokaler Quell-Root (mit/ohne /), $2 = entfernter Zielpfad (ohne user@host)
run_rsync_parallel() {
  local src_root="${1%/}" dst_path="${2%/}"
  local jobs="$RSYNC_PARALLEL_JOBS"
  local remote="${OFFSITE_USER}@${OFFSITE_HOST}"

  echo "$(date '+%F %T'): Parallel-Sync (${jobs} Streams): $src_root → ${remote}:${dst_path}/"

  # Struktur-Pass: alles bis Tiefe 2 (Top-Level-Dateien + leere Shard-Dirs),
  # Inhalte ab Tiefe 3 ausgeschlossen (übernehmen die Shards). --delete entfernt
  # hier verwaiste Top-Level-Einträge; ausgeschlossene (= Shard-)Inhalte sind
  # vor Löschung geschützt.
  echo "$(date '+%F %T'): Struktur-Pass (Tiefe ≤2) …"
  local skel_opts=(
    -aHAX -W --numeric-ids --max-alloc=4G
    --timeout="$RSYNC_IO_TIMEOUT" --info=none --stats
    --delete -f '- /*/*/**'
  )
  run_root rsync "${skel_opts[@]}" -e "$SSH_CMD" "$src_root/" "${remote}:${dst_path}/"

  local shards=()
  mapfile -t shards < <(run_root find "$src_root" -mindepth 2 -maxdepth 2 -type d -printf '%P\n' 2>/dev/null | sort)
  local total="${#shards[@]}"
  echo "$(date '+%F %T'): $total Shards zu übertragen"
  if [[ "$total" -eq 0 ]]; then
    echo "$(date '+%F %T'): Keine Tiefe-2-Verzeichnisse – Struktur-Pass deckt alles ab."
    return 0
  fi

  local rc_dir; rc_dir="$(mktemp -d)"
  local shard
  for shard in "${shards[@]}"; do
    while (( $(jobs -rp | wc -l) >= jobs )); do wait -n 2>/dev/null || wait; done
    (
      if ! RSYNC_SSH="$SSH_CMD_NOCTL" run_rsync "$src_root/$shard/" "${remote}:${dst_path}/$shard/"; then
        : > "$rc_dir/fail.$BASHPID"
      fi
    ) &
  done
  wait

  local fails; fails="$(find "$rc_dir" -type f -name 'fail.*' 2>/dev/null | wc -l)"
  rm -rf "$rc_dir"
  if (( fails > 0 )); then
    echo "$(date '+%F %T'): FEHLER: $fails von $total Shards fehlgeschlagen"
    return 1
  fi
  echo "$(date '+%F %T'): Alle $total Shards erfolgreich übertragen"
}

zfs_destroy_retry() {
  local snap="$1" i
  for i in 1 2 3; do
    if run_root zfs destroy "$snap" 2>/dev/null; then
      echo "$(date '+%F %T'): Snapshot gelöscht: $snap"
      return 0
    fi
    echo "$(date '+%F %T'): Snapshot busy, Versuch $i/3 – warte 30s: $snap"
    sleep 30
  done
  echo "$(date '+%F %T'): Snapshot noch busy – deferred destroy: $snap"
  run_root zfs destroy -d "$snap"
}

# Verwaiste rsync/ssh-Prozesse früherer Läufe beenden. Wenn die SSH-Pipe vom
# HA-Add-on abbricht, läuft der rsync hier weiter (reparented auf init) und hält
# den pre_rsync-Snapshot-Mount offen → jeder neue Lauf scheitert am "dataset is
# busy". Wir killen sie vor dem Snapshot-Cleanup. Zu diesem Zeitpunkt existiert
# noch kein rsync des aktuellen Laufs, daher ist jeder Treffer ein Altlauf.
kill_stale_backup_procs() {
  local self=$$ parent
  parent="$(awk '{print $4}' "/proc/$self/stat" 2>/dev/null || echo 0)"
  local targets=() pid ppid
  for pid in $(pgrep -f 'ctl-rsync-offline|\.zfs/snapshot/pre_rsync' 2>/dev/null || true); do
    [[ "$pid" == "$self" || "$pid" == "$parent" ]] && continue
    targets+=("$pid")
    ppid="$(awk '{print $4}' "/proc/$pid/stat" 2>/dev/null || echo 0)"
    if [[ "$ppid" -gt 1 && "$ppid" != "$self" && "$ppid" != "$parent" ]]; then
      targets+=("$ppid")
    fi
  done
  [[ "${#targets[@]}" -eq 0 ]] && return 0

  local uniq; uniq="$(printf '%s\n' "${targets[@]}" | sort -un)"
  echo "$(date '+%F %T'): Verwaiste Backup-Prozesse gefunden – beende: $(echo "$uniq" | tr '\n' ' ')"
  # Hinweis: Schleifenkörper bzw. Pipeline müssen unter set -e/pipefail mit 0
  # enden – sonst bricht ein bereits beendeter Prozess (Erfolgsfall!) das Skript ab.
  echo "$uniq" | while IFS= read -r p; do
    [[ -n "$p" ]] && run_root kill -TERM "$p" 2>/dev/null || true
  done || true
  sleep 5
  echo "$uniq" | while IFS= read -r p; do
    if [[ -n "$p" && -d "/proc/$p" ]]; then
      echo "$(date '+%F %T'): PID $p lebt noch – SIGKILL"
      run_root kill -KILL "$p" 2>/dev/null || true
    fi
  done || true
  sleep 2
  run_root rm -f /tmp/ctl-rsync-offline-* 2>/dev/null || true
  return 0
}

kill_stale_backup_procs

# Vorhandene pre_rsync-Snapshots im Log anzeigen (Diagnose)
existing="$(run_root zfs list -H -t snapshot -o name,defer_destroy -s creation -r "$DATASET" \
  | grep -E "^${DATASET}@pre_rsync" || true)"
if [[ -n "$existing" ]]; then
  echo "$(date '+%F %T'): Gefundene pre_rsync-Snapshots vor Cleanup:"
  echo "$existing" | while IFS= read -r line; do echo "  $line"; done
fi

# Alte pre_rsync Snapshots löschen
{ run_root zfs list -H -t snapshot -o name -s creation -r "$DATASET" \
    | grep -E "^${DATASET}@pre_rsync" || true; } \
| while IFS= read -r snap; do
    [[ -z "$snap" ]] && continue
    zfs_destroy_retry "$snap"
  done

SNAP="pre_rsync_$(date +%F_%H-%M-%S)"
MP="$(zfs get -H -o value mountpoint "$DATASET")"
echo "$(date '+%F %T'): Snapshot erstellen: ${DATASET}@${SNAP}"
run_root zfs snapshot "${DATASET}@${SNAP}"

run_rsync_parallel "$MP/.zfs/snapshot/$SNAP/" "${OFFSITE_PATH}/ZPool/BackupPC"
echo "$(date '+%F %T'): Snapshot löschen: ${DATASET}@${SNAP}"
zfs_destroy_retry "${DATASET}@${SNAP}"

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
