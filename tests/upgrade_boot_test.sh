#!/usr/bin/env bash
# Upgrade-/Boot-Test (Gate 2) für das Offsite-Backup-Add-on.
#
# Bootet zuerst das vorherige Release-Image, befüllt ein persistentes /data,
# startet dann das Kandidaten-Image auf demselben /data und prüft, dass:
#   * der Container hochkommt und die API auf :8099 antwortet
#   * gespeicherter /data-State den Versionswechsel überlebt
#   * /api/options KEINE sensiblen Felder ausliefert (Secret-Hiding-Invariante)
#
# Boot macht bewusst kein SSH (run.sh richtet nur Cron ein + startet api.py),
# daher braucht der Test kein echtes NAS/Offsite-Ziel.
#
# Aufruf: upgrade_boot_test.sh <old_image> <new_image>
set -euo pipefail

OLD_IMAGE="${1:?old image fehlt}"
NEW_IMAGE="${2:?new image fehlt}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIXTURE="$HERE/fixtures/options.offsite.json"
DATADIR="$(mktemp -d)"
PORT=8099
CONTAINER="offsite_boottest"

SECRET_MARKERS=(
  SECRET_USER_SHOULD_NOT_APPEAR
  SECRET_HOST_SHOULD_NOT_APPEAR
  SECRET_MQTT_SHOULD_NOT_APPEAR
  SECRET_KEY_SHOULD_NOT_APPEAR
  SECRET_TOKEN_SHOULD_NOT_APPEAR
)

cleanup() { docker rm -f "$CONTAINER" >/dev/null 2>&1 || true; rm -rf "$DATADIR"; }
trap cleanup EXIT

fail() { echo "FEHLGESCHLAGEN: $*" >&2; docker logs "$CONTAINER" 2>&1 | tail -40 >&2 || true; exit 1; }

boot_and_check() {
  local image="$1" phase="$2"
  echo "=== [$phase] boote $image ==="
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  # bash als Entrypoint umgeht die with-contenv/s6-Abhängigkeit des Shebangs.
  docker run -d --name "$CONTAINER" -v "$DATADIR:/data" -p "$PORT:8099" \
    --entrypoint bash "$image" /run.sh >/dev/null

  local ok="" body=""
  for _ in $(seq 1 60); do
    if body="$(curl -fsS "http://localhost:$PORT/api/options" 2>/dev/null)"; then
      ok=1; break
    fi
    # Container vorzeitig gestorben?
    docker ps -q --filter "name=$CONTAINER" | grep -q . || fail "[$phase] Container beendet vor API-Bereitschaft"
    sleep 1
  done
  [ -n "$ok" ] || fail "[$phase] API auf :$PORT nicht erreichbar (Timeout)"

  echo "$body" | python3 -c 'import json,sys; json.load(sys.stdin)' \
    || fail "[$phase] /api/options liefert kein gültiges JSON"

  for marker in "${SECRET_MARKERS[@]}"; do
    if echo "$body" | grep -q "$marker"; then
      fail "[$phase] Secret-Leak: '$marker' in /api/options-Antwort!"
    fi
  done

  curl -fsS "http://localhost:$PORT/api/status" >/dev/null \
    || fail "[$phase] /api/status nicht erreichbar"

  echo "[$phase] OK — API bereit, /data persistiert, keine Secrets in /api/options"
}

echo "Datenverzeichnis: $DATADIR"
cp "$FIXTURE" "$DATADIR/options.json"

boot_and_check "$OLD_IMAGE" "ALT (vorheriges Release)"

# Markerdatei in /data: muss den Upgrade überleben
echo "upgrade-marker-$$" > "$DATADIR/logs/upgrade_marker.txt" 2>/dev/null \
  || { mkdir -p "$DATADIR/logs"; echo "upgrade-marker-$$" > "$DATADIR/logs/upgrade_marker.txt"; }

boot_and_check "$NEW_IMAGE" "NEU (Kandidat)"

[ -f "$DATADIR/logs/upgrade_marker.txt" ] \
  || fail "Markerdatei in /data nach Upgrade verschwunden"

echo
echo "OK: Upgrade alt→neu erfolgreich — Boot, /data-Persistenz und Secret-Hiding bestätigt."
