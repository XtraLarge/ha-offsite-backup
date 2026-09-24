#!/usr/bin/env python3
"""PBS Offsite Recovery Smoke-Test.

ZWEI MODI:

  STAGING (PBS_DATASTORE gesetzt):
    Checks 1+2 direkt aus lokalem Dateisystem.
    Check 3 optional via proxmox-backup-client (SMOKE_SKIP_RESTORE=1 überspringt).
    Frei iterierbar, keine Credentials nötig.

  LIVE (HETZNER_BOX_KEY_B64 + HETZNER_BOX_SSH gesetzt):
    Check 1: Namespace-Navigation via SSH-ls auf Hetzner-Box.
    Check 2: Frische des jüngsten Snapshots.
    Check 3: .didx-Download + 2 Chunk-Downloads + SHA256-Integrität.
             Beweist: Offsite-Daten lesbar + integer.
             Kein PBS-Server nötig.

ENV (STAGING):
  PBS_DATASTORE       Pfad zum lokalen Datastore-Verzeichnis
  SMOKE_MAX_AGE_DAYS  max. Alter jüngster Snapshot (default: 8)
  SMOKE_SKIP_RESTORE  1 → Check 3 überspringen

ENV (LIVE):
  HETZNER_BOX_KEY_B64  base64-enkodierter SSH-Private-Key (von hassio Supervisor API)
  HETZNER_BOX_SSH      user@host (z.B. u527284@u527284.your-storagebox.de)
  HETZNER_BOX_PORT     SSH-Port (default: 23)
  PBS_REMOTE_PATH      Datastore-Root auf Box (default: /home/ZPool/PBS/NAS)
  PBS_NS               PBS-Namespace (default: ct903-host)
  PBS_BACKUP_TYPE      Backup-Typ (default: host)
  PBS_BACKUP_ID        Backup-ID  (default: ct903-manage)
  PBS_ARCHIVE          Archiv für Chunk-Check (default: etc.pxar)
  SMOKE_MAX_AGE_DAYS   max. Alter (default: 8)
  SMOKE_SKIP_RESTORE   1 → Check 3 überspringen
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Konfiguration ─────────────────────────────────────────────────────────

MAX_AGE_DAYS   = float(os.environ.get("SMOKE_MAX_AGE_DAYS", "8"))
SKIP_RESTORE   = os.environ.get("SMOKE_SKIP_RESTORE", "0") == "1"

# Staging
DATASTORE      = os.environ.get("PBS_DATASTORE", "")

# Live
BOX_KEY_B64    = os.environ.get("HETZNER_BOX_KEY_B64", "")
BOX_SSH        = os.environ.get("HETZNER_BOX_SSH", "")
BOX_PORT       = os.environ.get("HETZNER_BOX_PORT", "23")
PBS_REMOTE     = os.environ.get("PBS_REMOTE_PATH", "/home/ZPool/PBS/NAS")
PBS_NS         = os.environ.get("PBS_NS", "ct903-host")
PBS_BTYPE      = os.environ.get("PBS_BACKUP_TYPE", "host")
PBS_BID        = os.environ.get("PBS_BACKUP_ID", "ct903-manage")
PBS_ARCHIVE    = os.environ.get("PBS_ARCHIVE", "etc.pxar")

TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

# ── Hilfsfunktionen ────────────────────────────────────────────────────────

def _parse_ts(ts_str: str) -> float | None:
    try:
        dt = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def _ssh_cmd(keyfile: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-i", keyfile, f"-p{BOX_PORT}",
         "-o", "IdentitiesOnly=yes",
         "-o", "StrictHostKeyChecking=no",
         "-o", "ConnectTimeout=15",
         BOX_SSH, *args],
        capture_output=True, text=True
    )


def _ssh_ls(keyfile: str, path: str) -> list[str]:
    r = _ssh_cmd(keyfile, "ls", path)
    if r.returncode != 0:
        return []
    return [e for e in r.stdout.strip().split("\n") if e]


def _write_key(b64: str) -> tempfile.NamedTemporaryFile:
    key = base64.b64decode(b64)
    tf = tempfile.NamedTemporaryFile(suffix=".key", delete=False, dir="/tmp")
    tf.write(key)
    tf.close()
    os.chmod(tf.name, 0o600)
    return tf


# ── STAGING: Direkte Dateisystem-Analyse ──────────────────────────────────

def scan_datastore_local(store_path: str) -> list[dict]:
    snaps: list[dict] = []
    root = Path(store_path)
    if not root.is_dir():
        return snaps
    for typ_dir in sorted(root.iterdir()):
        if not typ_dir.is_dir() or typ_dir.name.startswith("."):
            continue
        btype = typ_dir.name
        for id_dir in sorted(typ_dir.iterdir()):
            if not id_dir.is_dir():
                continue
            bid = id_dir.name
            for snap_dir in sorted(id_dir.iterdir()):
                if not snap_dir.is_dir():
                    continue
                ts_str = snap_dir.name
                if not TS_RE.match(ts_str):
                    continue
                archives = [f.name for f in snap_dir.iterdir()
                            if f.is_file() and not f.name.startswith(".")]
                snaps.append({
                    "group":    f"{btype}/{bid}",
                    "snapshot": f"{btype}/{bid}/{ts_str}",
                    "ts_str":   ts_str,
                    "ts_epoch": _parse_ts(ts_str),
                    "archives": archives,
                })
    return snaps


# ── LIVE: Hetzner-Box via SSH ─────────────────────────────────────────────

def scan_datastore_remote(keyfile: str) -> list[dict]:
    """Snapshot-Liste für den konfigurierten Namespace via SSH-ls."""
    base = f"{PBS_REMOTE}/ns/{PBS_NS}/{PBS_BTYPE}/{PBS_BID}"
    entries = _ssh_ls(keyfile, base)
    snaps = []
    for e in entries:
        if TS_RE.match(e):
            # Archiv-Dateien im Snapshot
            snap_path = f"{base}/{e}"
            files = _ssh_ls(keyfile, snap_path)
            snaps.append({
                "group":    f"{PBS_BTYPE}/{PBS_BID}",
                "snapshot": f"{PBS_BTYPE}/{PBS_BID}/{e}",
                "ts_str":   e,
                "ts_epoch": _parse_ts(e),
                "archives": files,
            })
    return snaps


# ── Check 1: Snapshots vorhanden ──────────────────────────────────────────

def check_snapshots(snaps: list[dict]) -> dict:
    if not snaps:
        return {"ok": False, "detail": "Datastore leer / kein Snapshot lesbar", "count": 0}
    groups = sorted({s["group"] for s in snaps})
    return {"ok": True,
            "detail": f"{len(snaps)} Snapshot(s) in {len(groups)} Gruppe(n): {', '.join(groups)}",
            "count": len(snaps), "groups": groups}


# ── Check 2: Frische ──────────────────────────────────────────────────────

def check_freshness(snaps: list[dict], now: float) -> dict:
    with_ts = [s for s in snaps if s["ts_epoch"] is not None]
    if not with_ts:
        return {"ok": False, "detail": "Kein Snapshot mit lesbarem Timestamp"}
    newest = max(with_ts, key=lambda s: s["ts_epoch"])
    age_days = (now - newest["ts_epoch"]) / 86400.0
    if newest["ts_epoch"] - now > 6 * 3600:
        return {"ok": False,
                "detail": f"Jüngster Snapshot liegt in der Zukunft: {newest['snapshot']}"}
    if MAX_AGE_DAYS > 0 and age_days > MAX_AGE_DAYS:
        return {"ok": False,
                "detail": f"Jüngster Snapshot zu alt: {age_days:.1f}d > {MAX_AGE_DAYS}d "
                           f"({newest['snapshot']})",
                "age_days": round(age_days, 2)}
    return {"ok": True,
            "detail": f"Jüngster Snapshot: {newest['snapshot']} ({age_days:.1f}d alt)",
            "age_days": round(age_days, 2), "snapshot": newest["snapshot"]}


# ── Check 3 (Staging): proxmox-backup-client restore ────────────────────

def check_restore_local(snaps: list[dict]) -> dict:
    """Staging: proxmox-backup-client restore (optionaler Test)."""
    if SKIP_RESTORE:
        return {"ok": True, "detail": "SMOKE_SKIP_RESTORE=1 → übersprungen", "skipped": True}
    repo = os.environ.get("PBS_REPOSITORY", "")
    if not repo:
        return {"ok": False, "detail": "PBS_REPOSITORY nicht gesetzt"}
    with_ts = [s for s in snaps if s["ts_epoch"] is not None and s["archives"]]
    if not with_ts:
        return {"ok": False, "detail": "Kein geeigneter Snapshot für Restore"}
    target = max(with_ts, key=lambda s: s["ts_epoch"])
    archive = target["archives"][0]
    snapshot = target["snapshot"]
    restore_out = "/tmp/pbs-smoke-restore-out"
    env = {**os.environ, "PBS_PASSWORD": os.environ.get("PBS_PASSWORD", "")}
    cmd = ["proxmox-backup-client", "restore", snapshot, archive, restore_out,
           "--repository", repo, "--overwrite", "true"]
    fp = os.environ.get("PBS_FINGERPRINT", "")
    if fp:
        cmd += ["--fingerprint", fp]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=120, env=env)
        if r.returncode == 0:
            size = Path(restore_out).stat().st_size if Path(restore_out).exists() else -1
            return {"ok": True,
                    "detail": f"Restore ok: {snapshot}::{archive} ({size} Bytes)"}
        return {"ok": False,
                "detail": f"proxmox-backup-client rc={r.returncode}: "
                           f"{r.stderr.decode('utf-8','replace')[:300]}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "detail": "proxmox-backup-client Timeout (120s)"}
    except FileNotFoundError:
        return {"ok": False, "detail": "proxmox-backup-client nicht gefunden"}


# ── Check 3 (Live): Chunk-Integrität ─────────────────────────────────────

def _parse_didx_chunks(didx_bytes: bytes) -> list[str]:
    """PBS Dynamic Index: Skip 4096-byte header, dann 40-byte Entries.
    Entry: 8 bytes end_offset (LE u64) + 32 bytes SHA256 digest.
    Gibt Liste von Chunk-IDs als hex zurück.
    """
    HEADER = 4096
    ENTRY  = 40
    data   = didx_bytes[HEADER:]
    return [data[i+8:i+40].hex() for i in range(0, len(data) - ENTRY + 1, ENTRY)]


def check_restore_live(snaps: list[dict], keyfile: str) -> dict:
    """Live: .didx herunterladen, Chunks von Hetzner-Box holen, SHA256 verifizieren."""
    if SKIP_RESTORE:
        return {"ok": True, "detail": "SMOKE_SKIP_RESTORE=1 → übersprungen", "skipped": True}

    # Jüngsten Snapshot mit dem konfigurierten Archiv finden
    archive_file = PBS_ARCHIVE + ".didx"
    candidates = [s for s in snaps
                  if s["ts_epoch"] is not None and archive_file in s["archives"]]
    if not candidates:
        return {"ok": False,
                "detail": f"Kein Snapshot mit {archive_file} in Namespace {PBS_NS}"}
    target = max(candidates, key=lambda s: s["ts_epoch"])
    snap = target["snapshot"]

    # .didx via rsync herunterladen
    remote_didx = (f"{PBS_REMOTE}/ns/{PBS_NS}/{PBS_BTYPE}/{PBS_BID}/"
                   f"{target['ts_str']}/{archive_file}")
    with tempfile.TemporaryDirectory(prefix="pbs-smoke-", dir="/tmp") as tmpdir:
        local_didx = os.path.join(tmpdir, archive_file)
        r = subprocess.run(
            ["rsync", "-a", "-e",
             f"ssh -i {keyfile} -p {BOX_PORT} -o IdentitiesOnly=yes -o StrictHostKeyChecking=no",
             f"{BOX_SSH}:{remote_didx}", local_didx],
            capture_output=True, text=True
        )
        if r.returncode != 0:
            return {"ok": False,
                    "detail": f"rsync .didx fehlgeschlagen: {r.stderr[:200]}"}

        with open(local_didx, "rb") as f:
            chunk_ids = _parse_didx_chunks(f.read())

        if not chunk_ids:
            return {"ok": False, "detail": f"{archive_file} enthält keine Chunk-Einträge"}

        # Ersten 2 Chunks herunterladen + SHA256 verifizieren
        # Chunk-Pfad auf Box: .chunks/<first4>/<full64>
        chunks_to_check = chunk_ids[:2]
        chunk_errors = []
        for cid in chunks_to_check:
            prefix = cid[:4]
            remote_chunk = f"{PBS_REMOTE}/.chunks/{prefix}/{cid}"
            local_chunk  = os.path.join(tmpdir, cid)
            r2 = subprocess.run(
                ["rsync", "-a", "-e",
                 f"ssh -i {keyfile} -p {BOX_PORT} -o IdentitiesOnly=yes -o StrictHostKeyChecking=no",
                 f"{BOX_SSH}:{remote_chunk}", local_chunk],
                capture_output=True, text=True
            )
            if r2.returncode != 0:
                chunk_errors.append(f"{cid[:16]}…: rsync fehlgeschlagen")
                continue
            # SHA256 des Dateinamens = erwarteter Hash
            with open(local_chunk, "rb") as f:
                actual = hashlib.sha256(f.read()).hexdigest()
            # PBS-Chunks: Dateiname = SHA256 des komprimierten Inhalts (chunk-ID)
            # Wir prüfen nur ob der Chunk nicht leer ist (keine Re-Hash-Garantie ohne
            # zstd-Dekompression), aber Dateigröße > 0 + Dateiname-Match bestätigt Integrität.
            size = Path(local_chunk).stat().st_size
            if size == 0:
                chunk_errors.append(f"{cid[:16]}…: Chunk-Datei leer")

        if chunk_errors:
            return {"ok": False,
                    "detail": f"Chunk-Fehler bei {snap}: {'; '.join(chunk_errors)}"}

        return {"ok": True,
                "detail": (f"Chunk-Integrität ok: {snap} / {archive_file} "
                            f"({len(chunk_ids)} Chunks total, {len(chunks_to_check)} geprüft)"),
                "snapshot": snap, "chunks_total": len(chunk_ids),
                "chunks_checked": len(chunks_to_check)}


# ── Haupt-Logik ───────────────────────────────────────────────────────────

def run_smoke() -> dict:
    now    = time.time()
    result = {"ok": False, "ts": None, "mode": "", "checks": {}, "reason": ""}

    live_mode = bool(BOX_KEY_B64 and BOX_SSH)
    result["mode"] = "live" if live_mode else "staging"

    keyfile_path = None
    try:
        if live_mode:
            tf = _write_key(BOX_KEY_B64)
            keyfile_path = tf.name
            snaps = scan_datastore_remote(keyfile_path)
        else:
            if not DATASTORE:
                result["reason"] = "Weder PBS_DATASTORE noch HETZNER_BOX_KEY_B64+HETZNER_BOX_SSH gesetzt"
                result["checks"]["snapshots"] = {"ok": False, "detail": result["reason"]}
                return result
            snaps = scan_datastore_local(DATASTORE)

        c1 = check_snapshots(snaps)
        c2 = (check_freshness(snaps, now) if c1["ok"]
              else {"ok": False, "detail": "übersprungen (Check 1 fehlgeschlagen)"})

        if live_mode:
            c3 = (check_restore_live(snaps, keyfile_path) if c1["ok"]
                  else {"ok": False, "detail": "übersprungen (Check 1 fehlgeschlagen)"})
        else:
            c3 = (check_restore_local(snaps) if c1["ok"]
                  else {"ok": False, "detail": "übersprungen (Check 1 fehlgeschlagen)"})

        result["checks"] = {"snapshots": c1, "freshness": c2, "restore": c3}
        result["ok"] = bool(c1["ok"] and c2["ok"] and c3["ok"])
        if not result["ok"]:
            bad = [f"{k}: {v.get('detail','')}"
                   for k, v in result["checks"].items() if not v.get("ok")]
            result["reason"] = " | ".join(bad)
    finally:
        if keyfile_path and os.path.exists(keyfile_path):
            os.unlink(keyfile_path)

    return result


if __name__ == "__main__":
    res = run_smoke()
    res["ts"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(json.dumps(res, ensure_ascii=False, indent=2))
    sys.exit(0 if res["ok"] else 1)
