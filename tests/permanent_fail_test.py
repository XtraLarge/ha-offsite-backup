#!/usr/bin/env python3
"""Regressionstest fuer den OOM-Fix (#1497/#1507):
1) read_log/read_finished_log liefern einen BOUNDED Tail (deque, maxlen) statt
   die ganze Datei via readlines() in den RAM zu laden (Host-OOM-Ursache).
2) Persistenter Permanent-Fehler-Marker stoppt Auto-Resume bei permanentem
   Fehlerbild (Offsite-Quota voll) dauerhaft – auch ueber OOM-Neustarts hinweg
   (der In-Memory-_resume-Zaehler resettete sonst und machte den 3er-Deckel
   unwirksam -> getaktete Volllast-/OOM-Laeufe).
"""
import os
import sys
import tempfile

os.environ.pop("SUPERVISOR_TOKEN", None)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "offsite-backup"))
import api  # noqa: E402

fails = []

def check(cond, msg):
    print(("[OK ] " if cond else "[FAIL] ") + msg)
    if not cond:
        fails.append(msg)


# --- 1) bounded Tail -------------------------------------------------------
tmp = tempfile.mkdtemp()
big = os.path.join(tmp, "backup.log")
with open(big, "w") as f:
    for i in range(100000):
        f.write(f"zeile {i}\n")
api.LOG_FILE = big
res = api.read_log(50)
check(len(res) == 50, f"read_log liefert genau 50 Zeilen (got {len(res)})")
check(res[-1] == "zeile 99999\n", "read_log liefert die LETZTEN Zeilen (Tail)")
check(res[0] == "zeile 99950\n", "read_log Tail korrekt am Anfang")
# read_finished_log faellt ohne RUNS_DIR-Datei auf read_log zurueck -> ebenfalls bounded
api.RUNS_DIR = os.path.join(tmp, "runs_empty")
res2 = api.read_finished_log(10)
check(len(res2) == 10, f"read_finished_log liefert bounded Tail (got {len(res2)})")

# Sicherstellen: kein readlines()-Vollload mehr im Quelltext
src = open(os.path.join(os.path.dirname(__file__), "..", "offsite-backup", "api.py")).read()
check("readlines()" not in src, "api.py enthaelt kein readlines() mehr")
check("from collections import deque" in src, "api.py importiert deque")


# --- 2) Permanent-Fehler-Erkennung ----------------------------------------
check(api._log_is_permanent_failure("rsync: mkstemp ... Disk quota exceeded (122)"),
      "Disk-quota-Zeile wird als permanent erkannt")
check(api._log_is_permanent_failure("No space left on device"),
      "No-space-Zeile wird als permanent erkannt")
check(not api._log_is_permanent_failure("rsync: some transient IO error rc=11"),
      "transienter IO-Fehler ist NICHT permanent")
check(not api._log_is_permanent_failure(""), "leeres Log ist nicht permanent")


# --- 3) Marker-Helper + Auto-Resume-Suppression ---------------------------
api.PERMANENT_FAIL_MARKER = os.path.join(tmp, "permanent-fail")
api.ABORT_MARKER = os.path.join(tmp, "aborted")
api._auto_resume_enabled = lambda: True

api._clear_permanent_failure()
check(not os.path.exists(api.PERMANENT_FAIL_MARKER), "clear ohne Marker ist idempotent")
api._mark_permanent_failure("Testgrund Quota")
check(os.path.exists(api.PERMANENT_FAIL_MARKER), "_mark_permanent_failure schreibt Marker")

# _maybe_fire_resume darf bei gesetztem Marker NICHT feuern
fired = {"n": 0}
api.trigger_backup = lambda _auto=False: fired.__setitem__("n", fired["n"] + 1)
api._resume["next_at"] = 1.0        # in der Vergangenheit -> waere faellig
api._resume["attempts"] = 0
api._maybe_fire_resume()
check(fired["n"] == 0, "Auto-Resume feuert NICHT bei gesetztem Permanent-Marker")

# ohne Marker feuert es (Kontrolle: Aussagekraft des Tests)
api._clear_permanent_failure()
api._resume["next_at"] = 1.0
api._maybe_fire_resume()
check(fired["n"] == 1, "Kontrolle: ohne Marker feuert Auto-Resume (Test aussagekraeftig)")

print()
if fails:
    print(f"FEHLGESCHLAGEN: {len(fails)} Faelle")
    sys.exit(1)
print("Alle permanent_fail/oom-Tests bestanden.")
