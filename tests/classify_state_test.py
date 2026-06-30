#!/usr/bin/env python3
"""Regressionstest für _classify_state (Offsite-Backup-Addon).

Sichert den Fix gegen die Post-Finalize-False-Positive-„stalled"-Schleife ab:
ein nach dem Finalisieren (RunDir gelöscht) noch nicht gereapter screen/proc darf
NICHT als „stalled" gelten (sonst Endlos-Auto-Resume), aber ein ECHTER Hänger
(RunDir vorhanden, run.log stale) MUSS weiterhin als „stalled" erkannt werden.
"""
import os
import sys

# Kein SUPERVISOR_TOKEN -> der Import-Thread (_update_recovery_slug) macht keinen
# Netzwerk-Call und kehrt sofort zurück.
os.environ.pop("SUPERVISOR_TOKEN", None)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "offsite-backup"))
import api  # noqa: E402

S = api.STALL_SECS


def st(screen=False, proc=False, rundir=False, exit=None, age=-1):
    return {"screen": screen, "proc": proc, "rundir": rundir, "exit": exit, "age": age}


CASES = [
    ("running (frisch)",            st(screen=True, rundir=True, age=10),            "running"),
    ("echter Haenger (stale log)",  st(screen=True, rundir=True, age=S + 200),       "stalled"),
    ("echter Haenger (proc only)",  st(proc=True, rundir=True, age=S + 1),           "stalled"),
    ("POST-FINALIZE-ZOMBIE screen", st(screen=True, rundir=False, exit=None),        "idle"),
    ("POST-FINALIZE-ZOMBIE proc",   st(proc=True, rundir=False, exit=None),          "idle"),
    ("finished (exit gesetzt)",     st(screen=True, rundir=False, exit="0"),         "finished"),
    ("finished trotz rundir",       st(rundir=True, exit="0"),                       "finished"),
    ("crashed (proc tot, rundir)",  st(rundir=True, exit=None),                      "crashed"),
    ("idle (alles aus)",            st(),                                            "idle"),
]

fails = 0
for name, state, want in CASES:
    got = api._classify_state(state)
    ok = got == want
    print(f"[{'OK ' if ok else 'FAIL'}] {name:32s} erwartet={want:9s} got={got}")
    if not ok:
        fails += 1

if fails:
    print(f"\n{fails} FEHLER")
    sys.exit(1)
print("\nAlle _classify_state-Faelle bestanden.")
