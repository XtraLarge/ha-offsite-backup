#!/usr/bin/env bash
# PBS Offsite Recovery Smoke-Harness
#
# MODI:
#   --staging   Checks 1+2 gegen eine lokale Fixture (kein PBS-Server nötig).
#               Frei iterierbar, kein Gate.
#               Schlägt fehl wenn Fixture-Struktur ungültig oder Checks rot.
#
#   --live      Vollständiger E2E-Restore gegen die echte Hetzner-Box-Kopie.
#               Rsync + Restore-Smoke (3/3) auf einer Recovery-LXC auf gvmhp.
#               NUR einmal, braucht Freigabe (request_approval durch rufenden Agent).
#
# Voraussetzungen --staging:
#   python3 auf dem Ausführungshost
#
# Voraussetzungen --live:
#   SSH-Zugang zur Hetzner-Box (HETZNER_BOX_SSH env)
#   SSH-Zugang zu gvmhp (pvesh-CLI)
#   proxmox-backup-client installiert
#   HETZNER_BOX_SSH, PBS_REMOTE_PATH, LXC_TEMPLATE envs gesetzt
#
# Exit-Code: 0 = grün, 1 = rot

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SMOKE_PY="$SCRIPT_DIR/smoke.py"

MODE="${1:-}"
if [[ "$MODE" != "--staging" && "$MODE" != "--live" ]]; then
    echo "Verwendung: $0 --staging | --live" >&2
    exit 1
fi

# ════════════════════════════════════════════════════════════════
# STAGING: Checks 1+2 gegen Fixture (kein PBS-Server)
# ════════════════════════════════════════════════════════════════
if [[ "$MODE" == "--staging" ]]; then
    echo "=== PBS Smoke-Harness: STAGING (Checks 1+2, kein Restore) ==="

    FIXTURE="/tmp/pbs-smoke-fixture-$$"
    trap "rm -rf '$FIXTURE'" EXIT

    # Fixture mit zwei Snapshot-Gruppen und frischem Timestamp
    TS_NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    mkdir -p "$FIXTURE/vm/100/${TS_NOW}"
    mkdir -p "$FIXTURE/ct/200/${TS_NOW}"
    mkdir -p "$FIXTURE/.chunks/ab"

    echo '{"size":4096}' > "$FIXTURE/vm/100/${TS_NOW}/disk-0.img.fidx"
    echo '{"size":1024}' > "$FIXTURE/ct/200/${TS_NOW}/rootfs.pxar.didx"
    echo '{"size":512}'  > "$FIXTURE/.chunks/ab/abcdef1234"

    echo "[staging] Fixture unter $FIXTURE:"
    find "$FIXTURE" -type f | sort

    echo "[staging] Führe smoke.py aus (SMOKE_SKIP_RESTORE=1) ..."
    PBS_DATASTORE="$FIXTURE" SMOKE_SKIP_RESTORE=1 python3 "$SMOKE_PY"
    RC=$?
    echo "=== Staging-Smoke exit $RC ==="
    exit $RC
fi

# ════════════════════════════════════════════════════════════════
# LIVE: E2E Restore gegen Hetzner-Box-Kopie (Gate-Lauf)
# ════════════════════════════════════════════════════════════════
if [[ "$MODE" == "--live" ]]; then
    echo "=== PBS Smoke-Harness: LIVE (E2E, Gate-Lauf) ===" >&2
    : "${HETZNER_BOX_SSH:?Env HETZNER_BOX_SSH nicht gesetzt}"
    : "${PBS_REMOTE_PATH:?Env PBS_REMOTE_PATH nicht gesetzt}"
    HETZNER_BOX_PORT="${HETZNER_BOX_PORT:-23}"
    SYNC_DIR="/tmp/pbs-live-sync-$$"
    LXC_CTID="${LXC_CTID:-}"         # optional; falls leer: kein LXC-Teardown

    trap "echo '[teardown] Räume Sync-Dir auf'; rm -rf '$SYNC_DIR'; \
          [[ -n '$LXC_CTID' ]] && ssh gvmhp pvesh create /nodes/gvmhp/lxc/${LXC_CTID}/status/stop 2>/dev/null; \
          true" EXIT

    mkdir -p "$SYNC_DIR"

    # Schritt 1: Jüngste Snapshot-Gruppe ermitteln
    echo "[01] Ermittle Snapshot-Gruppen auf Hetzner-Box ..."
    LATEST=$(ssh -p "$HETZNER_BOX_PORT" -o StrictHostKeyChecking=no "$HETZNER_BOX_SSH" \
        "find '${PBS_REMOTE_PATH}' -mindepth 3 -maxdepth 3 -type d 2>/dev/null | sort | tail -3")
    if [[ -z "$LATEST" ]]; then
        echo "Kein Snapshot auf Hetzner-Box gefunden" >&2; exit 1
    fi
    echo "$LATEST"

    # Schritt 2: Subset lokal syncen (jüngster Snapshot + Chunks)
    SNAP_REL=$(echo "$LATEST" | tail -1 | sed "s|${PBS_REMOTE_PATH}/||")
    SNAP_GROUP=$(dirname "$SNAP_REL")
    echo "[02] Synce Snapshot $SNAP_REL ..."
    rsync -az -e "ssh -p $HETZNER_BOX_PORT -o StrictHostKeyChecking=no" \
        "${HETZNER_BOX_SSH}:${PBS_REMOTE_PATH}/${SNAP_GROUP}/" \
        "$SYNC_DIR/${SNAP_GROUP}/"
    rsync -az -e "ssh -p $HETZNER_BOX_PORT -o StrictHostKeyChecking=no" \
        "${HETZNER_BOX_SSH}:${PBS_REMOTE_PATH}/.chunks/" \
        "$SYNC_DIR/.chunks/"
    echo "Sync fertig: $(du -sh "$SYNC_DIR" | cut -f1)"

    # Schritt 3: Checks 1+2 (ohne Server)
    echo "[03] Checks 1+2 (Dateisystem) ..."
    PBS_DATASTORE="$SYNC_DIR" SMOKE_SKIP_RESTORE=1 python3 "$SMOKE_PY"
    RC12=$?
    [[ $RC12 -ne 0 ]] && { echo "Checks 1+2 FEHLGESCHLAGEN — Abbruch" >&2; exit 1; }

    # Schritt 4: PBS-Server für Restore starten (auf Recovery-LXC oder lokal)
    # Setzt voraus: PBS_REPOSITORY + PBS_PASSWORD + PBS_FINGERPRINT gesetzt
    if [[ -n "${PBS_REPOSITORY:-}" ]]; then
        echo "[04] Check 3: Restore-Smoke ..."
        PBS_DATASTORE="$SYNC_DIR" \
        PBS_REPOSITORY="${PBS_REPOSITORY}" \
        PBS_PASSWORD="${PBS_PASSWORD:-}" \
        PBS_FINGERPRINT="${PBS_FINGERPRINT:-}" \
            python3 "$SMOKE_PY"
        RC=$?
    else
        echo "[04] PBS_REPOSITORY nicht gesetzt — Restore übersprungen (nur Checks 1+2)"
        RC=$RC12
    fi

    echo "=== Live-Smoke exit $RC ==="
    exit $RC
fi
