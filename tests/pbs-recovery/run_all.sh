#!/usr/bin/env bash
# PBS Offsite Recovery Smoke-Harness
#
# MODI:
#   --staging   Checks 1+2 gegen lokale Fixture (kein SSH, keine Credentials).
#               Frei iterierbar; prüft das Harness selbst.
#               Exit 0 = grün.
#
#   --live      Vollständiger Live-Check gegen echte Hetzner-Offsite-Kopie.
#               Holt SSH-Key direkt aus hassio Supervisor API.
#               Check 1: Snapshots vorhanden
#               Check 2: Frische (< SMOKE_MAX_AGE_DAYS Tage, default 8)
#               Check 3: .didx-Download + 2 Chunks + Integrität
#               Exit 0 = grün, 1 = rot.
#
# Voraussetzungen --live:
#   SSH-Zugang von Manage zu hassio (10.10.6.43, Port 22)
#   hassio hat SUPERVISOR_TOKEN im SSH-Addon-Container
#   Netzwerk-Zugang von Manage zu u527284.your-storagebox.de:23
#
# Env-Overrides (--live):
#   HASSIO_HOST          hassio-IP (default: 10.10.6.43)
#   HETZNER_BOX_SSH      user@host (default: aus Supervisor API)
#   HETZNER_BOX_PORT     SSH-Port  (default: 23)
#   PBS_REMOTE_PATH      Datastore-Root (default: /home/ZPool/PBS/NAS)
#   PBS_NS               Namespace (default: ct903-host)
#   PBS_BACKUP_TYPE      Backup-Typ (default: host)
#   PBS_BACKUP_ID        Backup-ID  (default: ct903-manage)
#   PBS_ARCHIVE          Archiv für Chunk-Check (default: etc.pxar)
#   SMOKE_MAX_AGE_DAYS   max. Alter Snapshot (default: 8)
#   SMOKE_SKIP_RESTORE   1 → Check 3 überspringen

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SMOKE_PY="$SCRIPT_DIR/smoke.py"
HASSIO_HOST="${HASSIO_HOST:-10.10.6.43}"

MODE="${1:-}"
if [[ "$MODE" != "--staging" && "$MODE" != "--live" ]]; then
    echo "Verwendung: $0 --staging | --live" >&2
    exit 1
fi

# ════════════════════════════════════════════════════════════════
# STAGING
# ════════════════════════════════════════════════════════════════
if [[ "$MODE" == "--staging" ]]; then
    echo "=== PBS Smoke-Harness: STAGING ==="

    FIXTURE="/tmp/pbs-smoke-fixture-$$"
    trap "rm -rf '$FIXTURE'" EXIT

    TS_NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    mkdir -p "$FIXTURE/host/pve/${TS_NOW}"
    mkdir -p "$FIXTURE/ct/200/${TS_NOW}"
    mkdir -p "$FIXTURE/.chunks/ab12"

    echo '{"size":4096}' > "$FIXTURE/host/pve/${TS_NOW}/pxar.didx"
    echo '{"size":1024}' > "$FIXTURE/ct/200/${TS_NOW}/rootfs.pxar.didx"
    echo '{"size":512}'  > "$FIXTURE/.chunks/ab12/ab12cdef"

    echo "[staging] Fixture: $(find "$FIXTURE" -type f | sort | tr '\n' ' ')"
    echo "[staging] Starte smoke.py ..."
    PBS_DATASTORE="$FIXTURE" SMOKE_SKIP_RESTORE=1 python3 "$SMOKE_PY"
    RC=$?
    echo "=== Staging exit $RC ==="
    exit $RC
fi

# ════════════════════════════════════════════════════════════════
# LIVE
# ════════════════════════════════════════════════════════════════
echo "=== PBS Smoke-Harness: LIVE ==="

# 1. SSH-Key + Box-Credentials aus hassio Supervisor API holen
echo "[01] Hole Credentials von hassio ($HASSIO_HOST) ..."
ADDON_INFO=$(ssh -o ConnectTimeout=15 -o StrictHostKeyChecking=no \
    "root@${HASSIO_HOST}" \
    'TOKEN=$(printenv SUPERVISOR_TOKEN); curl -s -H "Authorization: Bearer $TOKEN" http://supervisor/addons/3e98a749_offsite_backup/info')

# Key extrahieren + base64-enkodieren (Python auf Manage verfügbar)
BOX_KEY_B64=$(echo "$ADDON_INFO" | python3 -c "
import sys, json, re, base64
d = json.load(sys.stdin)
opts = d['data']['options']
raw = opts['ssh_key_offsite']
key_text = raw.replace(chr(92)+'n', chr(10))
b64 = re.sub(r'-----[A-Z ]+-----', '', key_text)
b64 = re.sub(r'\s', '', b64)
import struct
from pathlib import Path
wrapped = chr(10).join(b64[i:i+70] for i in range(0, len(b64), 70))
key = '-----BEGIN OPENSSH PRIVATE KEY-----' + chr(10) + wrapped + chr(10) + '-----END OPENSSH PRIVATE KEY-----' + chr(10)
print(base64.b64encode(key.encode()).decode())
")

# Box-Adresse aus Addon-Config (Fallback: aus ENV)
if [[ -z "${HETZNER_BOX_SSH:-}" ]]; then
    HETZNER_BOX_USER=$(echo "$ADDON_INFO" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['data']['options']['offsite_user'])")
    HETZNER_BOX_HOST=$(echo "$ADDON_INFO" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['data']['options']['offsite_host'])")
    export HETZNER_BOX_SSH="${HETZNER_BOX_USER}@${HETZNER_BOX_HOST}"
fi

echo "[01] Box: $HETZNER_BOX_SSH, Key: $(echo "$BOX_KEY_B64" | wc -c) Bytes b64"

# 2. Smoke-Test starten
echo "[02] Starte smoke.py (Live-Modus) ..."
HETZNER_BOX_KEY_B64="$BOX_KEY_B64" \
HETZNER_BOX_SSH="$HETZNER_BOX_SSH" \
HETZNER_BOX_PORT="${HETZNER_BOX_PORT:-23}" \
PBS_REMOTE_PATH="${PBS_REMOTE_PATH:-/home/ZPool/PBS/NAS}" \
PBS_NS="${PBS_NS:-ct903-host}" \
PBS_BACKUP_TYPE="${PBS_BACKUP_TYPE:-host}" \
PBS_BACKUP_ID="${PBS_BACKUP_ID:-ct903-manage}" \
PBS_ARCHIVE="${PBS_ARCHIVE:-etc.pxar}" \
SMOKE_MAX_AGE_DAYS="${SMOKE_MAX_AGE_DAYS:-8}" \
SMOKE_SKIP_RESTORE="${SMOKE_SKIP_RESTORE:-0}" \
    python3 "$SMOKE_PY"
RC=$?

echo "=== Live exit $RC ==="
exit $RC
