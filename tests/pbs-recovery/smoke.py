#!/usr/bin/env python3
"""PBS Offsite Recovery Smoke-Test (3/3).

3 Checks müssen grün sein, damit DoD-4 gilt:
  1) SNAPSHOTS  – PBS-Datastore enthält mind. 1 lesbaren Snapshot
  2) FRESHNESS  – jüngster Snapshot nicht älter als SMOKE_MAX_AGE_DAYS
  3) RESTORE    – proxmox-backup-client restore einer echten Datei

Direkte Dateisystem-Analyse für Checks 1+2 (kein PBS-Server nötig).
Check 3 ruft proxmox-backup-client gegen einen laufenden PBS-Server auf.

Env:
  PBS_DATASTORE       Pfad zum (lokalen) Datastore-Verzeichnis
  PBS_REPOSITORY      user@host[:port]:store  (für proxmox-backup-client)
  PBS_PASSWORD        Passwort für PBS-Client
  PBS_FINGERPRINT     Server-TLS-Fingerprint
  SMOKE_MAX_AGE_DAYS  max. Alter jüngster Snapshot (default: 30)
  SMOKE_SKIP_RESTORE  1 → Check 3 überspringen
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DATASTORE   = os.environ.get("PBS_DATASTORE", "")
REPOSITORY  = os.environ.get("PBS_REPOSITORY", "")
PASSWORD    = os.environ.get("PBS_PASSWORD", "")
FINGERPRINT = os.environ.get("PBS_FINGERPRINT", "")
MAX_AGE_DAYS = float(os.environ.get("SMOKE_MAX_AGE_DAYS", "30"))
SKIP_RESTORE = os.environ.get("SMOKE_SKIP_RESTORE", "0") == "1"

# ── Hilfsfunktionen ────────────────────────────────────────────────────────

TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _parse_ts(ts_str: str) -> float | None:
    """ISO-8601 UTC-Timestamp → Unix-Sekunden."""
    try:
        dt = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def scan_datastore(store_path: str) -> list[dict]:
    """Liest alle Snapshots direkt aus der Datastore-Verzeichnisstruktur.
    Struktur: <store>/<type>/<id>/<timestamp>/
    Gibt Liste von {group, snapshot, ts_str, ts_epoch, archives} zurück.
    """
    snaps: list[dict] = []
    root = Path(store_path)
    if not root.is_dir():
        return snaps
    for typ_dir in sorted(root.iterdir()):
        if not typ_dir.is_dir() or typ_dir.name.startswith("."):
            continue
        btype = typ_dir.name                           # vm / ct / host / ...
        for id_dir in sorted(typ_dir.iterdir()):
            if not id_dir.is_dir():
                continue
            bid = id_dir.name                          # 100 / pve / ...
            for snap_dir in sorted(id_dir.iterdir()):
                if not snap_dir.is_dir():
                    continue
                ts_str = snap_dir.name
                if not TS_RE.match(ts_str):
                    continue
                archives = [f.name for f in snap_dir.iterdir()
                            if f.is_file() and not f.name.startswith(".")]
                ts_epoch = _parse_ts(ts_str)
                snaps.append({
                    "group":    f"{btype}/{bid}",
                    "snapshot": f"{btype}/{bid}/{ts_str}",
                    "ts_str":   ts_str,
                    "ts_epoch": ts_epoch,
                    "archives": archives,
                })
    return snaps


# ── Check 1: Snapshots vorhanden ───────────────────────────────────────────

def check_snapshots(snaps: list[dict]) -> dict:
    if not snaps:
        return {"ok": False, "detail": "Datastore leer / kein Snapshot lesbar",
                "count": 0}
    groups = sorted({s["group"] for s in snaps})
    return {"ok": True,
            "detail": f"{len(snaps)} Snapshot(s) in {len(groups)} Gruppe(n): {', '.join(groups)}",
            "count": len(snaps), "groups": groups}


# ── Check 2: Frische ───────────────────────────────────────────────────────

def check_freshness(snaps: list[dict], now: float) -> dict:
    with_ts = [s for s in snaps if s["ts_epoch"] is not None]
    if not with_ts:
        return {"ok": False, "detail": "Kein Snapshot mit lesbarem Timestamp"}
    newest = max(with_ts, key=lambda s: s["ts_epoch"])
    age_days = (now - newest["ts_epoch"]) / 86400.0
    future_skew = newest["ts_epoch"] - now
    if future_skew > 6 * 3600:
        return {"ok": False,
                "detail": f"Jüngster Snapshot liegt in der Zukunft: {newest['snapshot']}",
                "age_days": round(-age_days, 2)}
    if MAX_AGE_DAYS > 0 and age_days > MAX_AGE_DAYS:
        return {"ok": False,
                "detail": f"Jüngster Snapshot zu alt: {age_days:.1f}d > {MAX_AGE_DAYS}d "
                           f"({newest['snapshot']})",
                "age_days": round(age_days, 2)}
    return {"ok": True,
            "detail": f"Jüngster Snapshot: {newest['snapshot']} ({age_days:.1f}d alt)",
            "age_days": round(age_days, 2), "snapshot": newest["snapshot"]}


# ── Check 3: Echter Restore ────────────────────────────────────────────────

def _pick_restore_target(snaps: list[dict]) -> dict | None:
    """Snapshot mit dem jüngsten Timestamp + mind. 1 Archiv."""
    with_ts = [s for s in snaps if s["ts_epoch"] is not None and s["archives"]]
    if not with_ts:
        return None
    return max(with_ts, key=lambda s: s["ts_epoch"])


def check_restore(snaps: list[dict]) -> dict:
    """Führt proxmox-backup-client restore durch (eine Archiv-Datei)."""
    if SKIP_RESTORE:
        return {"ok": True, "detail": "SMOKE_SKIP_RESTORE=1 → übersprungen", "skipped": True}
    if not REPOSITORY:
        return {"ok": False, "detail": "PBS_REPOSITORY nicht gesetzt"}
    target = _pick_restore_target(snaps)
    if target is None:
        return {"ok": False, "detail": "Kein geeigneter Snapshot für Restore"}

    # Erstes Archiv im Snapshot
    archive = target["archives"][0]
    snapshot = target["snapshot"]
    restore_out = "/tmp/pbs-smoke-restore-out"

    env = dict(os.environ)
    if PASSWORD:
        env["PBS_PASSWORD"] = PASSWORD

    cmd = ["proxmox-backup-client", "restore", snapshot, archive, restore_out,
           "--repository", REPOSITORY]
    if FINGERPRINT:
        cmd += ["--fingerprint", FINGERPRINT]
    # Überschreiben erlauben (idempotent)
    cmd += ["--overwrite", "true"]

    try:
        r = subprocess.run(cmd, capture_output=True, timeout=120, env=env)
        rc = r.returncode
        stderr = r.stderr.decode("utf-8", "replace")[:500]
        if rc == 0:
            # Größe des Restore-Ergebnisses
            try:
                size = Path(restore_out).stat().st_size
            except OSError:
                size = -1
            return {"ok": True,
                    "detail": f"Restore ok: {snapshot}::{archive} → {restore_out} ({size} Bytes)",
                    "snapshot": snapshot, "archive": archive, "bytes": size}
        return {"ok": False,
                "detail": f"proxmox-backup-client restore rc={rc}: {stderr}",
                "snapshot": snapshot, "archive": archive}
    except subprocess.TimeoutExpired:
        return {"ok": False, "detail": "proxmox-backup-client restore Timeout (120s)"}
    except FileNotFoundError:
        return {"ok": False, "detail": "proxmox-backup-client nicht gefunden"}


# ── Aggregation ────────────────────────────────────────────────────────────

def run_smoke() -> dict:
    now = time.time()
    result: dict = {"ok": False, "ts": None, "checks": {}, "reason": ""}

    if not DATASTORE:
        result["reason"] = "PBS_DATASTORE nicht gesetzt"
        result["checks"]["snapshots"] = {"ok": False, "detail": result["reason"]}
        return result

    snaps = scan_datastore(DATASTORE)

    c1 = check_snapshots(snaps)
    c2 = check_freshness(snaps, now) if c1["ok"] else \
         {"ok": False, "detail": "übersprungen (Check 1 fehlgeschlagen)"}
    c3 = check_restore(snaps) if c1["ok"] else \
         {"ok": False, "detail": "übersprungen (Check 1 fehlgeschlagen)"}

    result["checks"] = {"snapshots": c1, "freshness": c2, "restore": c3}
    result["ok"] = bool(c1["ok"] and c2["ok"] and c3["ok"])
    if not result["ok"]:
        bad = [f"{k}: {v.get('detail','')}" for k, v in result["checks"].items()
               if not v.get("ok")]
        result["reason"] = " | ".join(bad)
    return result


if __name__ == "__main__":
    res = run_smoke()
    res["ts"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(json.dumps(res, ensure_ascii=False, indent=2))
    sys.exit(0 if res["ok"] else 1)
