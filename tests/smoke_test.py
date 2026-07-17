#!/usr/bin/env python3
"""Tests fuer den Recovery-Smoke-Test (backuppc-recovery/smoke.py, Wissen #751).

Deckt POSITIV (alle drei Checks gruen -> success) und NEGATIV (je ein kuenstlich
eingebauter Fehler MUSS zu failed fuehren) ab. Ohne echte BackupPC-Umgebung:
Fixture-TopDir + injizierter Restore-Runner.
"""
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backuppc-recovery"))
import smoke  # noqa: E402

NOW = 1_700_000_000.0  # fixer "Jetzt"-Zeitpunkt fuer deterministische Alters-Checks
DAY = 86400

_fails = 0


def check(name, cond):
    global _fails
    status = "OK " if cond else "FAIL"
    if not cond:
        _fails += 1
    print(f"[{status}] {name}")


def make_topdir(hosts_backups, shares=("f%2fetc",)):
    """hosts_backups: {host: [(num, end_epoch), ...]}. Legt pc/<host>/backups +
    pc/<host>/<maxnum>/<share>/ an. Rueckgabe TopDir-Pfad."""
    td = tempfile.mkdtemp(prefix="smoke_td_")
    pc = os.path.join(td, "pc")
    for host, backups in hosts_backups.items():
        hd = os.path.join(pc, host)
        os.makedirs(hd, exist_ok=True)
        with open(os.path.join(hd, "backups"), "w") as f:
            for num, end in backups:
                # num type start end nFiles size ...
                f.write(f"{num}\tfull\t{end-100}\t{end}\t10\t1024\t10\t1024\t0\t0\t0\n")
        if backups:
            maxnum = max(b[0] for b in backups)
            for sh in shares:
                os.makedirs(os.path.join(hd, str(maxnum), sh), exist_ok=True)
    return td


def make_hosts_file(hosts):
    fd, path = tempfile.mkstemp(prefix="smoke_hosts_")
    with os.fdopen(fd, "w") as f:
        f.write("host        dhcp    user      moreUsers\n")
        f.write("localhost   0       backuppc\n")  # Pseudo-Host -> ignoriert
        for h in hosts:
            f.write(f"{h}   0   someuser\n")
    return path


def runner_ok(argv):
    return 0, b"realfilecontent", ""


def runner_empty(argv):
    return 0, b"", "keine Daten"


def cleanup(*paths):
    for p in paths:
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        elif os.path.exists(p):
            os.remove(p)


# ── reine Hilfsfunktionen ────────────────────────────────────────────────────
check("demangle f%2fetc -> /etc", smoke.demangle("f%2fetc") == "/etc")
check("demangle f%2f -> /", smoke.demangle("f%2f") == "/")
check("demangle ohne f-Prefix", smoke.demangle("%2fvar") == "/var")


# ── POSITIV: alle drei gruen ─────────────────────────────────────────────────
td = make_topdir({"hostA": [(1, int(NOW) - 2 * DAY), (2, int(NOW) - DAY)],
                  "hostB": [(1, int(NOW) - 3 * DAY)]})
hf = make_hosts_file(["hostA", "hostB"])
res = smoke.run_smoke(topdir=td, hosts_file=hf, now=NOW, runner=runner_ok)
check("POSITIV: ok=True", res["ok"] is True)
check("POSITIV: hosts gruen", res["checks"]["hosts"]["ok"])
check("POSITIV: times gruen", res["checks"]["times"]["ok"])
check("POSITIV: restore gruen", res["checks"]["restore"]["ok"])
check("POSITIV: Restore-Ziel = juengster Host (hostA#2)",
      res["target"] == {"host": "hostA", "num": 2})
check("POSITIV: reason leer", res["reason"] == "")
cleanup(td, hf)

# ── NEGATIV 1: konfigurierter Host ohne pc/-Sicherung ────────────────────────
td = make_topdir({"hostA": [(1, int(NOW) - DAY)]})
hf = make_hosts_file(["hostA", "hostB"])  # hostB fehlt in pc/
res = smoke.run_smoke(topdir=td, hosts_file=hf, now=NOW, runner=runner_ok)
check("NEG-hosts: ok=False", res["ok"] is False)
check("NEG-hosts: hosts rot", not res["checks"]["hosts"]["ok"])
check("NEG-hosts: hostB im Grund", "hostB" in res["reason"])
cleanup(td, hf)

# ── NEGATIV 2: juengstes Backup zu alt ───────────────────────────────────────
td = make_topdir({"hostA": [(1, int(NOW) - 90 * DAY)]})  # 90d > 30d Default
hf = make_hosts_file(["hostA"])
res = smoke.run_smoke(topdir=td, hosts_file=hf, now=NOW, runner=runner_ok)
check("NEG-times: ok=False", res["ok"] is False)
check("NEG-times: times rot", not res["checks"]["times"]["ok"])
check("NEG-times: 'zu alt' im Grund", "zu alt" in res["reason"])
cleanup(td, hf)

# ── NEGATIV 3: Backup-Ende in der Zukunft (Uhr-/Datenfehler) ─────────────────
td = make_topdir({"hostA": [(1, int(NOW) + 5 * DAY)]})
hf = make_hosts_file(["hostA"])
res = smoke.run_smoke(topdir=td, hosts_file=hf, now=NOW, runner=runner_ok)
check("NEG-future: ok=False", res["ok"] is False)
check("NEG-future: 'Zukunft' im Grund", "Zukunft" in res["reason"])
cleanup(td, hf)

# ── NEGATIV 4: Restore liefert 0 Byte (Pool nicht lesbar) ────────────────────
td = make_topdir({"hostA": [(1, int(NOW) - DAY)]})
hf = make_hosts_file(["hostA"])
res = smoke.run_smoke(topdir=td, hosts_file=hf, now=NOW, runner=runner_empty)
check("NEG-restore: ok=False", res["ok"] is False)
check("NEG-restore: restore rot", not res["checks"]["restore"]["ok"])
check("NEG-restore: '0 Byte' im Grund", "0 Byte" in res["reason"])
cleanup(td, hf)

# ── NEGATIV 5: gar keine pc/-Hosts sichtbar ──────────────────────────────────
td = make_topdir({})
hf = make_hosts_file(["hostA"])
res = smoke.run_smoke(topdir=td, hosts_file=hf, now=NOW, runner=runner_ok)
check("NEG-empty: ok=False", res["ok"] is False)
check("NEG-empty: hosts rot (keine pc/)", not res["checks"]["hosts"]["ok"])
cleanup(td, hf)

# ── Randfall: TopDir nicht ermittelbar ───────────────────────────────────────
res = smoke.run_smoke(topdir=None, config_pl="/nonexistent/config.pl", now=NOW, runner=runner_ok)
check("EDGE: kein TopDir -> ok=False", res["ok"] is False)

print()
if _fails:
    print(f"FEHLGESCHLAGEN: {_fails} Assertion(s)")
    sys.exit(1)
print("Alle smoke-Tests bestanden.")
