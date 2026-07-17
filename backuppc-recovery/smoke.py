#!/usr/bin/env python3
"""Recovery-Smoke-Test (Wissen #751).

Durchstich-Test der BackupPC-Recovery-Umgebung gegen die Offsite-Kopie. Wird
nach jedem Offsite-Lauf ausgeloest (offsite-backup startet dieses Recovery-Addon,
smoke laeuft hier, das Ergebnis wird ueber den HTTP-Status (state.py :9080 /smoke)
abgeholt). NUR wenn alle drei Checks gruen sind, gilt die Offsite-Sicherung als
"fehlerfrei abgeschlossen":

  1) HOSTS  – sieht die Recovery-Instanz auf der Offsite-Kopie die erwarteten
              Hosts? (pc/-Verzeichnisse <-> BackupPC-hosts-Konfig)
  2) ZEITEN – ist der juengste Backup-Zeitpunkt je Host plausibel (nicht zu alt,
              nicht in der Zukunft)?
  3) RESTORE– Mini-Restore EINER kleinen, folgenlosen Datei aus dem juengsten
              Backup ueber BackupPC_tarCreate (echter Offsite->Pool->lesbar).

Alle Kern-Funktionen sind rein und ueber Fixtures/injizierten Runner testbar
(tests/smoke_test.py). Der Restore-Schritt kapselt den einzigen BackupPC-Aufruf.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time

# BackupPC v4 kanonische Pfade in der Recovery-Umgebung (configure.pl).
DEFAULT_CONFIG_PL = "/etc/backuppc/config.pl"
DEFAULT_HOSTS_FILE = "/etc/backuppc/hosts"
DEFAULT_BPC_BIN = "/usr/local/BackupPC/bin"
SMOKE_JSON = "/data/smoke.json"

# Plausibilitaets-Fenster fuer den juengsten Backup-Zeitpunkt je Host.
DEFAULT_MAX_AGE_DAYS = float(os.environ.get("SMOKE_MAX_AGE_DAYS", "30"))
# Toleranz gegen kleine Uhr-Differenzen (Backup-Ende leicht in der Zukunft).
FUTURE_SKEW_SECS = 6 * 3600
# Pseudo-Hosts der BackupPC-Konfig, die keine echte pc/-Sicherung haben muessen.
PSEUDO_HOSTS = {"localhost"}


# ─────────────────────────── Konfig/Bestandsaufnahme ────────────────────────
def read_topdir(config_pl: str = DEFAULT_CONFIG_PL) -> str | None:
    """Liest $Conf{TopDir} aus der BackupPC-config.pl (letzte Zuweisung gewinnt)."""
    try:
        text = open(config_pl, encoding="utf-8", errors="replace").read()
    except OSError:
        return None
    matches = re.findall(r'\$Conf\{TopDir\}\s*=\s*["\']([^"\']+)["\']', text)
    return matches[-1] if matches else None


def parse_hosts_conf(hosts_file: str = DEFAULT_HOSTS_FILE) -> list[str]:
    """Hostnamen aus der BackupPC-hosts-Datei (erste Spalte, ohne Header/Kommentar
    und ohne Pseudo-Hosts)."""
    hosts: list[str] = []
    try:
        lines = open(hosts_file, encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        return hosts
    for ln in lines:
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        name = s.split()[0]
        if name.lower() == "host" or name.lower() in PSEUDO_HOSTS:
            continue  # Kopfzeile / Pseudo-Host
        hosts.append(name)
    return hosts


def list_pc_hosts(pc_dir: str) -> list[str]:
    """Host-Verzeichnisse unter TopDir/pc/ (die tatsaechlich gesicherten Hosts)."""
    try:
        return sorted(
            d for d in os.listdir(pc_dir)
            if os.path.isdir(os.path.join(pc_dir, d)) and not d.startswith(".")
        )
    except OSError:
        return []


def parse_backups(backups_file: str) -> list[dict]:
    """BackupPC pc/<host>/backups: je Zeile ein Backup (tab-getrennt).
    Felder v4: num type startTime endTime nFiles size ... Wir brauchen num+endTime.
    Rueckgabe nach num aufsteigend sortiert."""
    out: list[dict] = []
    try:
        lines = open(backups_file, encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        return out
    for ln in lines:
        f = ln.rstrip("\n").split("\t")
        if len(f) < 4:
            continue
        try:
            num = int(f[0])
            end = int(f[3])
        except ValueError:
            continue
        out.append({"num": num, "end": end})
    out.sort(key=lambda b: b["num"])
    return out


# ─────────────────────────────── Checks (rein) ──────────────────────────────
def check_hosts(pc_hosts: list[str], conf_hosts: list[str]) -> dict:
    """Gruen, wenn pc/ nicht leer ist UND jeder konfigurierte (echte) Host eine
    pc/-Sicherung hat. Fehlende Konfig-Hosts = Recovery sieht erwartete Hosts
    nicht -> rot."""
    actual = sorted(set(pc_hosts))
    expected = sorted(set(conf_hosts))
    if not actual:
        return {"ok": False, "detail": "keine pc/-Hosts auf der Offsite-Kopie sichtbar",
                "actual": actual, "expected": expected}
    missing = [h for h in expected if h not in set(actual)]
    if missing:
        return {"ok": False,
                "detail": f"konfigurierte Hosts ohne Offsite-Sicherung: {', '.join(missing)}",
                "actual": actual, "expected": expected}
    return {"ok": True,
            "detail": f"{len(actual)} Host(s) sichtbar, alle erwarteten gedeckt",
            "actual": actual, "expected": expected}


def check_times(backups_by_host: dict, now: float,
                max_age_days: float = DEFAULT_MAX_AGE_DAYS) -> dict:
    """Gruen, wenn jeder Host mind. ein Backup hat UND dessen juengstes Ende
    plausibel ist (nicht aelter als max_age_days, nicht > FUTURE_SKEW in der
    Zukunft). max_age_days<=0 deaktiviert die Altersschranke (nur Existenz)."""
    per_host: dict[str, dict] = {}
    problems: list[str] = []
    if not backups_by_host:
        return {"ok": False, "detail": "keine Backup-Historie vorhanden", "per_host": per_host}
    for host, backups in sorted(backups_by_host.items()):
        if not backups:
            per_host[host] = {"num": None, "end": None, "age_days": None}
            problems.append(f"{host}: keine Backups")
            continue
        latest = backups[-1]
        age_days = (now - latest["end"]) / 86400.0
        per_host[host] = {"num": latest["num"], "end": latest["end"],
                          "age_days": round(age_days, 2)}
        if latest["end"] - now > FUTURE_SKEW_SECS:
            problems.append(f"{host}: Backup-Ende in der Zukunft ({per_host[host]['age_days']}d)")
        elif max_age_days > 0 and age_days > max_age_days:
            problems.append(f"{host}: juengstes Backup zu alt ({per_host[host]['age_days']}d > {max_age_days}d)")
    if problems:
        return {"ok": False, "detail": "; ".join(problems), "per_host": per_host}
    return {"ok": True, "detail": f"{len(per_host)} Host(s) mit plausiblem juengsten Backup",
            "per_host": per_host}


def pick_restore_target(backups_by_host: dict) -> tuple[str | None, int | None]:
    """Host mit dem juengsten Backup-Ende + dessen juengste Backup-Nummer."""
    best_host, best_num, best_end = None, None, -1
    for host, backups in backups_by_host.items():
        if not backups:
            continue
        latest = backups[-1]
        if latest["end"] > best_end:
            best_host, best_num, best_end = host, latest["num"], latest["end"]
    return best_host, best_num


# ───────────────────────── BackupPC-Namen-Demangling ────────────────────────
def demangle(name: str) -> str:
    """BackupPC-Pfadelement demanglen: fuehrendes 'f' entfernen, %XX dekodieren.
    (BackupPC::Lib::fileNameMangle: 'f' + %XX-Kodierung fuer % und Sonderzeichen.)"""
    if name.startswith("f"):
        name = name[1:]
    return re.sub(r"%([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), name)


def discover_share(pc_dir: str, host: str, num: int) -> str | None:
    """Erste Share des Backups aus dem juengsten Backup-Verzeichnis ableiten
    (oberste 'f'-Verzeichnisse = gemanglete Share-Namen)."""
    bdir = os.path.join(pc_dir, host, str(num))
    try:
        entries = sorted(
            d for d in os.listdir(bdir)
            if d.startswith("f") and os.path.isdir(os.path.join(bdir, d))
        )
    except OSError:
        return None
    return demangle(entries[0]) if entries else None


# ───────────────────────────── Restore-Durchstich ───────────────────────────
def _real_runner(argv: list[str], timeout: int = 120) -> tuple[int, bytes, str]:
    """Fuehrt BackupPC_tarCreate aus, liest nur die ersten Bytes echten
    Datei-Inhalts (SIGPIPE bricht tarCreate frueh ab -> leicht). Rueckgabe
    (rc, first_bytes, stderr)."""
    try:
        p = subprocess.run(argv, capture_output=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return 124, b"", "timeout"
    except OSError as e:
        return 127, b"", str(e)


def restore_probe(pc_dir: str, host: str | None, num: int | None,
                  bpc_bin: str = DEFAULT_BPC_BIN, runner=None) -> dict:
    """Mini-Restore: streamt den juengsten Backup-Share via BackupPC_tarCreate,
    extrahiert die ersten Bytes echten Datei-Inhalts und wertet sie als Beleg,
    dass Offsite-Pool -> lesbar -> wiederherstellbar funktioniert.
    FAIL-CLOSED: jeder Fehler/0 Bytes => rot (sicherer Default)."""
    if host is None or num is None:
        return {"ok": False, "detail": "kein Restore-Ziel (keine Backups)", "bytes": 0}
    share = discover_share(pc_dir, host, num)
    if not share:
        return {"ok": False, "detail": f"keine Share im Backup {host}#{num} gefunden",
                "bytes": 0, "host": host, "num": num}
    run = runner or _real_runner
    tar_create = os.path.join(bpc_bin, "BackupPC_tarCreate")
    # -t = tar-Format an stdout; wir extrahieren via Shell-Pipe den ersten
    # Datei-Inhalt. Der Runner kapselt die Pipe (Prod: tar -xO | head -c).
    argv = ["/bin/sh", "-c",
            f'"{tar_create}" -h "$1" -n "$2" -s "$3" / 2>/tmp/smoke_tar.err '
            f'| tar -xO 2>/dev/null | head -c 64',
            "sh", host, str(num), share]
    rc, out, err = run(argv)
    nbytes = len(out or b"")
    if nbytes >= 1:
        return {"ok": True,
                "detail": f"Restore-Durchstich ok ({nbytes} Byte aus {host}#{num}:{share})",
                "bytes": nbytes, "host": host, "num": num, "share": share}
    return {"ok": False,
            "detail": f"Restore lieferte 0 Byte (rc={rc}) aus {host}#{num}:{share}: {err[:200]}",
            "bytes": nbytes, "host": host, "num": num, "share": share}


# ─────────────────────────────── Aggregation ────────────────────────────────
def run_smoke(topdir: str | None = None, hosts_file: str = DEFAULT_HOSTS_FILE,
              now: float | None = None, max_age_days: float = DEFAULT_MAX_AGE_DAYS,
              bpc_bin: str = DEFAULT_BPC_BIN, runner=None,
              config_pl: str = DEFAULT_CONFIG_PL) -> dict:
    """Fuehrt alle drei Checks aus und aggregiert. success NUR bei 3/3 gruen."""
    if now is None:
        now = time.time()
    if topdir is None:
        topdir = read_topdir(config_pl)
    result = {"ok": False, "ts": None, "target": {"host": None, "num": None},
              "checks": {}, "reason": ""}
    if not topdir:
        result["reason"] = "TopDir nicht ermittelbar (config.pl)"
        result["checks"]["hosts"] = {"ok": False, "detail": result["reason"]}
        return result
    pc_dir = os.path.join(topdir, "pc")

    pc_hosts = list_pc_hosts(pc_dir)
    conf_hosts = parse_hosts_conf(hosts_file)
    backups_by_host = {h: parse_backups(os.path.join(pc_dir, h, "backups")) for h in pc_hosts}

    c_hosts = check_hosts(pc_hosts, conf_hosts)
    c_times = check_times(backups_by_host, now, max_age_days)
    thost, tnum = pick_restore_target(backups_by_host)
    c_restore = restore_probe(pc_dir, thost, tnum, bpc_bin=bpc_bin, runner=runner)

    result["checks"] = {"hosts": c_hosts, "times": c_times, "restore": c_restore}
    result["target"] = {"host": thost, "num": tnum}
    result["ok"] = bool(c_hosts["ok"] and c_times["ok"] and c_restore["ok"])
    if not result["ok"]:
        reasons = [f"{k}: {v.get('detail','')}" for k, v in result["checks"].items() if not v.get("ok")]
        result["reason"] = " | ".join(reasons)
    return result


def write_result(result: dict, path: str = SMOKE_JSON) -> None:
    result = dict(result)
    result["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)
    except OSError:
        pass


if __name__ == "__main__":
    res = run_smoke()
    write_result(res)
    print(json.dumps(res, ensure_ascii=False, indent=2))
