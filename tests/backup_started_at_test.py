#!/usr/bin/env python3
"""Regressionstest fuer backup_started_at (Offsite-Backup-Addon, #1627/#840).

Sichert den Fix ab: backup_started_at muss fuer die GESAMTE Laufdauer und ueber
ALLE Startpfade (geplant NAS-seitig / manuell / Auto-Resume) verfuegbar sein,
also aus dem authoritativen NAS-Run-State (is_backup_running / _classify_state)
abgeleitet werden -- NICHT aus dem _run_backup-Lebenszyklus. Sonst meldet das
Icinga-Plugin waehrend eines geplanten Laufs faelschlich STALLED (started=?).
"""
import os
import sys
import tempfile

os.environ.pop("SUPERVISOR_TOKEN", None)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "offsite-backup"))
import api  # noqa: E402

# Marker auf temporaeren Pfad umbiegen (statt /data)
tmp = tempfile.mkdtemp()
api.BACKUP_STARTED_MARKER = os.path.join(tmp, "backup-started-at")

fails = 0
def check(name, cond):
    global fails
    print(f"[{'OK ' if cond else 'FAIL'}] {name}")
    if not cond:
        fails += 1

# Ausgangslage: idle -> None
api._sync_backup_started_marker("idle")
check("idle -> backup_started_at None", api.get_backup_started_at() is None)

# NAS-Lauf erkannt (running) -> gesetzt, nicht-null
api._sync_backup_started_marker("running")
first = api.get_backup_started_at()
check("running -> backup_started_at gesetzt (nicht None)", first is not None)

# Wiederholtes running -> Start bleibt STABIL (nicht ueberschrieben)
api._sync_backup_started_marker("running")
check("running x2 -> Start unveraendert (nicht ueberschrieben)", api.get_backup_started_at() == first)

# Transienter SSH-Aussetzer (unknown) mitten im Lauf -> Marker bleibt erhalten
api._sync_backup_started_marker("unknown")
check("unknown (SSH-Blip) -> Start bleibt erhalten", api.get_backup_started_at() == first)

# stalled zaehlt als laufend -> Start bleibt (echte Stall-Erkennung nutzt run_age)
api._sync_backup_started_marker("stalled")
check("stalled -> Start bleibt (echter Stall via run_age erkennbar)", api.get_backup_started_at() == first)

# finished (Finalisierung laeuft) -> Start bleibt noch
api._sync_backup_started_marker("finished")
check("finished -> Start bleibt bis Finalisierung", api.get_backup_started_at() == first)

# idle nach Finalisierung -> geloescht
api._sync_backup_started_marker("idle")
check("idle nach Lauf -> backup_started_at wieder None", api.get_backup_started_at() is None)

# crashed -> ebenfalls geloescht (kein Auto-Resume-Slot blockieren)
api._sync_backup_started_marker("running")
api._sync_backup_started_marker("crashed")
check("crashed -> backup_started_at None", api.get_backup_started_at() is None)

if fails:
    print(f"\n{fails} FEHLER")
    sys.exit(1)
print("\nAlle backup_started_at-Faelle bestanden.")
