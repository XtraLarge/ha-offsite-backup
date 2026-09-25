#!/usr/bin/env python3
"""HTTP-API und Web-Dashboard für das HA Offsite Backup Add-on."""
import json
from collections import deque
import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
import ssl
import urllib.parse
import urllib.request

PORT = 8099
INGRESS_PATH = os.environ.get("INGRESS_PATH", "")
OPTIONS_FILE = "/data/options.json"
LOG_FILE = "/data/logs/backup.log"
RUNS_DIR = "/data/logs/runs"
RUNS_KEEP = 20
STATUS_FILE = "/data/logs/status.json"
BACKUP_LOCK = "/tmp/backup-running"
SECRETS_DIR = "/data/secrets"
NAS_KEY     = SECRETS_DIR + "/id_ed25519_storage"
OFFSITE_KEY = SECRETS_DIR + "/id_ed25519_offsite"
SCREEN_NAME = "offsite-backup"
REMOTE_RUNDIR = "/dev/shm/offsite-backup"
ABORT_MARKER = "/data/aborted-by-user"   # manueller Abbruch → kein Auto-Resume
PERMANENT_FAIL_MARKER = "/data/permanent-fail"  # persistenter Stopp fuer Auto-Resume bei permanentem Fehlerbild (z.B. Offsite-Quota voll) – ueberlebt Container-Neustart/OOM
PERMANENT_FAIL_MARKERS_TXT = ("disk quota exceeded", "quota exceeded", "no space left on device")
BACKUP_STARTED_MARKER = "/data/backup-started-at"  # ISO-Startzeit des AKTUELLEN Laufs (persistent, ueberlebt Neustart); Quelle fuer MQTT/status backup_started_at (#1627/#840)
# Watchdog / Auto-Resume
STALL_SECS = 1800           # run.log seit >30 min ohne Aktivität → hängend
RESUME_BACKOFF_SECS = 1800  # Wartezeit vor automatischer Wiederaufnahme (30 min)
MAX_RESUME_ATTEMPTS = 3     # danach aufgeben (kein Endlos-Resume)
# Recovery-Smoke-Test (Wissen #751): nach jedem erfolgreichen Transfer prueft die
# Recovery-Umgebung die Offsite-Kopie (Hosts + Zeiten + Mini-Restore). Nur bei
# 3/3 gruen gilt die Sicherung als success.
SMOKE_POLL_TIMEOUT = 900    # max. Wartezeit auf ein Smoke-Ergebnis (s)
SMOKE_POLL_INTERVAL = 5     # Poll-Intervall gegen den Recovery-HTTP-Status (s)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("offsite-backup")


def _find_recovery_slug():
    token = os.environ.get("SUPERVISOR_TOKEN", "").strip()
    if not token:
        return "local_backuppc_recovery"
    try:
        req = urllib.request.Request(
            "http://supervisor/addons",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        for addon in data.get("data", {}).get("addons", []):
            slug = addon.get("slug", "")
            if "backuppc_recovery" in slug:
                log.info("Recovery-Slug gefunden: %s", slug)
                return slug
    except Exception as e:
        log.warning("Recovery-Slug-Erkennung fehlgeschlagen: %s", e)
    return "local_backuppc_recovery"


RECOVERY_ADDON_SLUG = "3e98a749_backuppc_recovery"


def _update_recovery_slug():
    global RECOVERY_ADDON_SLUG
    slug = _find_recovery_slug()
    if slug != RECOVERY_ADDON_SLUG:
        log.info("Recovery-Slug aktualisiert: %s → %s", RECOVERY_ADDON_SLUG, slug)
        RECOVERY_ADDON_SLUG = slug


threading.Thread(target=_update_recovery_slug, daemon=True).start()

_mqtt_client = None


def read_options():
    try:
        with open(OPTIONS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def read_log(lines=100):
    try:
        with open(LOG_FILE) as f:
            # bounded tail: haelt nur die letzten `lines` Zeilen im RAM,
            # egal wie gross die Datei ist (verhinderte Host-OOM bei Riesen-Log)
            return list(deque(f, maxlen=lines))
    except Exception:
        return []


def _latest_run_file():
    """Neuestes archiviertes Run-Log in RUNS_DIR (oder None)."""
    try:
        files = sorted(
            n for n in os.listdir(RUNS_DIR)
            if n.startswith("backup-") and n.endswith(".log")
        )
        return os.path.join(RUNS_DIR, files[-1]) if files else None
    except Exception:
        return None


def read_finished_log(lines=100):
    """Idle-Ansicht: das vollständige, archivierte run.log des letzten Laufs
    bevorzugen (auch wenn der Live-Spiegel backup.log durch einen Container-
    Neustart abgeschnitten wurde). Fällt auf backup.log zurück."""
    path = _latest_run_file()
    if path:
        try:
            with open(path) as f:
                return list(deque(f, maxlen=lines))
        except Exception:
            pass
    return read_log(lines)


_log_cache = {"ts": 0.0, "lines": None}


def get_log_lines(lines=100):
    """Während eines Laufs das run.log direkt von der NAS holen (damit das
    Dashboard auch dann aktuell ist, wenn die Tail-Pipe des Launchers durch ein
    Netzwerk-/Container-Problem abgerissen ist). Sonst die lokale Logdatei."""
    if not is_backup_running():
        return read_finished_log(lines)
    now = time.time()
    if now - _log_cache["ts"] < 8 and _log_cache["lines"] is not None:
        return _log_cache["lines"]
    r = _nas_ssh(f"tail -n {int(lines)} '{REMOTE_RUNDIR}/run.log' 2>/dev/null", timeout=12)
    if r is not None and r.returncode == 0 and r.stdout:
        result = [ln + "\n" for ln in r.stdout.splitlines()]
    else:
        result = read_log(lines)
    _log_cache.update(ts=now, lines=result)
    return result


def read_status():
    try:
        with open(STATUS_FILE) as f:
            return json.load(f)
    except Exception:
        return {"status": "unbekannt", "last_run": None}


def _nas_ssh(remote_cmd, timeout=12):
    """Führt einen Befehl auf der NAS aus. Der Storage-Key ist in
    authorized_keys auf `command="bash -s"` festgenagelt (forced command) –
    Argument-Befehle würden ignoriert. Daher wird der Befehl über STDIN an das
    erzwungene `bash -s` gepipt. Gibt CompletedProcess oder None zurück."""
    opts = read_options()
    host = opts.get("zfs_storage_host", "")
    user = opts.get("zfs_storage_user", "root") or "root"
    if not host or not os.path.exists(NAS_KEY):
        return None
    cmd = [
        "ssh", "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={timeout}",
        "-i", NAS_KEY, f"{user}@{host}",
    ]
    try:
        return subprocess.run(cmd, input=remote_cmd, capture_output=True,
                              text=True, timeout=timeout + 8)
    except Exception:
        return None


# Robuste Lauf-Erkennung: NICHT „screen existiert" (ein toter/idle screen ohne
# RunDir las sich früher für immer als „läuft"), sondern eine NAS-Zustandssonde
# aus screen + Prozess + RunDir + exit_code + run.log-Alter.
_STATE_SNIPPET = f"""s=0; p=0; d=0
screen -ls 2>/dev/null | grep -q {SCREEN_NAME} && s=1
pgrep -f '{REMOTE_RUNDIR}/backup_nas.sh' >/dev/null 2>&1 && p=1
[ -d "{REMOTE_RUNDIR}" ] && d=1
ec=$(cat "{REMOTE_RUNDIR}/exit_code" 2>/dev/null)
age=-1
[ -f "{REMOTE_RUNDIR}/run.log" ] && age=$(( $(date +%s) - $(stat -c %Y "{REMOTE_RUNDIR}/run.log") ))
printf 'screen=%s proc=%s rundir=%s exit=%s age=%s\\n' "$s" "$p" "$d" "$ec" "$age"
"""

_state_cache = {"ts": 0.0, "val": None}


def _classify_state(st):
    if st["exit"] is not None:
        return "finished"               # exit_code geschrieben → finalisieren
    if st["screen"] or st["proc"]:
        if st["rundir"] and 0 <= st["age"] <= STALL_SECS:
            return "running"            # echte, frische Aktivität
        if st["rundir"]:
            return "stalled"            # RunDir da, run.log stale → echter Hänger
        # RunDir bereits weg (Lauf finalisiert), aber screen/proc noch nicht
        # gereapt = Post-Finalize-Zombie. KEIN Hänger → idle, damit kein
        # unnötiger Auto-Resume eines bereits erfolgreichen Laufs ausgelöst wird.
        return "idle"
    if st["rundir"]:
        return "crashed"                # Prozess tot, RunDir ohne exit_code zurück
    return "idle"


# --- backup_started_at (Startzeitpunkt des laufenden NAS-Laufs) --------------
# Der geplante Lauf wird NAS-seitig gestartet (kein Addon-Scheduler ruft
# _run_backup), daher darf backup_started_at NICHT am _run_backup-Lebenszyklus
# haengen. Ein persistenter Marker wird gesetzt, sobald der NAS-Lauf als laufend
# erkannt wird, und geloescht, sobald er idle/crashed ist. So ist
# backup_started_at fuer die GESAMTE Laufdauer und ueber ALLE Startpfade
# (geplant/manuell/Auto-Resume) hinweg verfuegbar -> Basis fuer die korrekte
# Icinga-Stall-Age-Berechnung (#1627/#840).
_RUNNING_CLASSES = ("running", "stalled", "finished")


def _set_backup_started_marker():
    if os.path.exists(BACKUP_STARTED_MARKER):
        return
    try:
        with open(BACKUP_STARTED_MARKER, "w") as f:
            f.write(datetime.now(timezone.utc).isoformat())
    except OSError:
        pass


def _clear_backup_started_marker():
    try:
        os.unlink(BACKUP_STARTED_MARKER)
    except OSError:
        pass


def _sync_backup_started_marker(cls):
    if cls in _RUNNING_CLASSES:
        _set_backup_started_marker()
    elif cls in ("idle", "crashed"):
        _clear_backup_started_marker()
    # "unknown" (NAS-SSH-Aussetzer): Marker unveraendert lassen, damit ein
    # transienter Sondenfehler den echten Startzeitpunkt nicht verwirft.


def get_backup_started_at():
    """ISO-Startzeit des aktuell laufenden Laufs oder None (idle)."""
    try:
        with open(BACKUP_STARTED_MARKER) as f:
            return f.read().strip() or None
    except OSError:
        return None


def _nas_backup_state(force=False):
    """Eine kurz gecachte SSH-Sonde des NAS-Laufs, klassifiziert als
    idle | running | stalled | crashed | finished | unknown."""
    now = time.time()
    if not force and _state_cache["val"] is not None and now - _state_cache["ts"] < 8:
        return _state_cache["val"]
    r = _nas_ssh(_STATE_SNIPPET)
    if r is None or r.returncode != 0 or not (r.stdout or "").strip():
        # NAS nicht erreichbar → Lock-Datei als grobe (konservative) Rückfallebene.
        cls = "running" if os.path.exists(BACKUP_LOCK) else "unknown"
        st = {"class": cls, "screen": False, "proc": False, "rundir": False,
              "exit": None, "age": -1, "reachable": False}
        _sync_backup_started_marker(cls)
        _state_cache.update(ts=now, val=st)
        return st
    kv = dict(tok.split("=", 1) for tok in r.stdout.strip().splitlines()[-1].split())
    st = {
        "screen": kv.get("screen") == "1",
        "proc": kv.get("proc") == "1",
        "rundir": kv.get("rundir") == "1",
        "exit": (kv.get("exit") or "").strip() or None,
        "age": int(kv.get("age", "-1") or -1),
        "reachable": True,
    }
    st["class"] = _classify_state(st)
    _sync_backup_started_marker(st["class"])
    _state_cache.update(ts=now, val=st)
    return st


def is_backup_running():
    """True, solange der Slot belegt ist (läuft/hängt/wird-abgeschlossen) — damit
    kein zweiter Lauf darüberstartet. crashed/idle/unknown gelten als frei."""
    return _nas_backup_state()["class"] in ("running", "stalled", "finished")


def _supervisor_request(method, path, body=None):
    token = os.environ.get("SUPERVISOR_TOKEN", "").strip()
    if not token:
        raise RuntimeError("SUPERVISOR_TOKEN nicht verfügbar")
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        f"http://supervisor{path}",
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def is_recovery_running():
    try:
        data = _supervisor_request("GET", f"/addons/{RECOVERY_ADDON_SLUG}/info")
        return data.get("data", {}).get("state") == "started"
    except Exception:
        return False


def get_recovery_datastand():
    url = f"http://{RECOVERY_ADDON_SLUG.replace('_', '-')}.local.hass.io:9080/"
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            return json.loads(r.read()).get("datastand", "")
    except Exception:
        return ""


def get_next_run():
    opts = read_options()
    schedule = opts.get("backup_schedule", "")
    if not schedule:
        return None
    try:
        from croniter import croniter
        now = datetime.now().astimezone()
        it = croniter(schedule, now)
        nxt = it.get_next(datetime).astimezone()
        return nxt.isoformat()
    except Exception:
        return None


# Berechnet den Fortschritt serverseitig auf der NAS aus dem laufenden run.log
# und gibt eine kompakte, pipe-getrennte Zeile zurück:
#   src|pool_total|pool_done|pool_complete|snap_pct|finished
# Die Pool-Zählung erfolgt ab der letzten "Shards zu …"-Kopfzeile, damit ein
# 100-Zeilen-Tail-Fenster (das bei vielen Shards überläuft) nicht nötig ist.
_PROGRESS_SNIPPET = f"""f="{REMOTE_RUNDIR}/run.log"
[ -f "$f" ] || {{ echo "|||||"; exit 0; }}
src=$(grep -oE 'Quelle [0-9]+/[0-9]+: [^ ]+' "$f" | tail -1 | sed 's/^Quelle //')
fin=$(grep -c ': Fertig\\.' "$f")
snap=$(grep -oE 'Snapshot-Status: [a-z]+ \\([0-9]+%\\)' "$f" | tail -1 | grep -oE '[0-9]+' | tail -1)
hdr=$(grep -n 'Shards zu ' "$f" | tail -1 | cut -d: -f1)
ptotal=""; pdone=""; pcomplete=0
if [ -n "$hdr" ]; then
  ptotal=$(sed -n "${{hdr}}p" "$f" | grep -oE '[0-9]+ Shards zu ' | grep -oE '[0-9]+' | head -1)
  pdone=$(tail -n +"$hdr" "$f" | grep -c 'Shard fertig:')
  tail -n +"$hdr" "$f" | grep -q 'Shards erfolgreich' && pcomplete=1
fi
printf '%s|%s|%s|%s|%s|%s\\n' "$src" "$ptotal" "$pdone" "$pcomplete" "$snap" "$fin"
"""

_progress_cache = {"ts": 0.0, "val": "Bereit"}


def get_progress():
    cls = _nas_backup_state()["class"]
    if cls in ("idle", "unknown"):
        return "Bereit"
    if cls == "finished":
        return "Wird abgeschlossen"
    if cls in ("stalled", "crashed"):
        if os.path.exists(ABORT_MARKER):
            return "Abgebrochen"
        if _resume["next_at"] < 0:
            return f"Hängt – nach {MAX_RESUME_ATTEMPTS} Versuchen aufgegeben (bitte prüfen)"
        if _resume["next_at"] > 0:
            mins = max(0, int((_resume["next_at"] - time.time()) / 60))
            return (f"Hängt – Wiederaufnahme in ~{mins} min "
                    f"(Versuch {_resume['attempts'] + 1}/{MAX_RESUME_ATTEMPTS})")
        return "Hänger erkannt – wird aufgeräumt"
    # cls == "running"
    now = time.time()
    if now - _progress_cache["ts"] < 8:
        return _progress_cache["val"]
    val = _compute_progress()
    _progress_cache.update(ts=now, val=val)
    return val


def _compute_progress():
    r = _nas_ssh(_PROGRESS_SNIPPET, timeout=12)
    if r is None or r.returncode != 0 or not r.stdout.strip():
        return "Läuft"
    parts = r.stdout.strip().splitlines()[-1].split("|")
    if len(parts) != 6:
        return "Läuft"
    src, ptotal, pdone, pcomplete, snap, fin = parts
    if fin and fin != "0":
        return "Fertig"
    if snap:
        return f"Offsite-Snapshot {snap}%"
    if not src:
        return "Vorbereitung"
    num, _, dest = src.partition(": ")
    label = f"Quelle {num} · {dest}" if dest else f"Quelle {num}"
    if ptotal and pcomplete != "1":
        try:
            t, d = int(ptotal), int(pdone or 0)
            pct = round(d / t * 100) if t else 0
            label += f" · Pool {d}/{t} ({pct}%)"
        except ValueError:
            pass
    return label


_backup_proc = None
# Auto-Resume-Zustand: attempts = bisherige Wiederaufnahmen,
# next_at = Unix-Zeit der nächsten Wiederaufnahme (0 = keine geplant, <0 = aufgegeben).
_resume = {"attempts": 0, "next_at": 0.0}


def _clear_abort_marker():
    try:
        os.unlink(ABORT_MARKER)
    except OSError:
        pass


def _cleanup_nas_run():
    """Beendet screen + verwaiste Offsite-Prozesse, gibt den Snapshot-Mount frei,
    löscht verwaiste pre_rsync-Snapshots (entkoppelt – zfs destroy kann blockieren)
    und räumt das RunDir. Gemeinsam genutzt von Abbruch und Crash-Recovery."""
    _nas_ssh(
        f"screen -S {SCREEN_NAME} -X quit 2>/dev/null; "
        f"pkill -f '{REMOTE_RUNDIR}/backup_nas.sh' 2>/dev/null; "
        f"pkill -f '{REMOTE_RUNDIR}/nas_bootstrap.sh' 2>/dev/null; "
        r"pkill -f 'ctl-rsync-offline|\.zfs/snapshot/pre_rsync' 2>/dev/null; "
        "for s in $(zfs list -t snapshot -H -o name 2>/dev/null | grep '@pre_rsync_'); do "
        "nohup zfs destroy \"$s\" >/dev/null 2>&1 & done; "
        f"rm -rf {REMOTE_RUNDIR}; true",
        timeout=30,
    )
    _state_cache["ts"] = 0.0


def trigger_backup(_auto=False):
    if _nas_backup_state(force=True)["class"] in ("running", "stalled", "finished"):
        return False, "Backup läuft bereits"
    if not _auto:
        # Manueller/geplanter Start = frische Absicht: Abbruch-Marker löschen,
        # Resume-Zähler zurücksetzen.
        _clear_abort_marker()
        _clear_permanent_failure()
        _resume["attempts"] = 0
        _resume["next_at"] = 0.0
    threading.Thread(target=_run_backup, daemon=True).start()
    return True, ("Backup wiederaufgenommen" if _auto else "Backup gestartet")


def abort_backup():
    global _backup_proc
    if not is_backup_running():
        return False, "Kein Backup läuft"
    # Marker setzen: ein MANUELLER Abbruch darf nicht automatisch wiederaufgenommen
    # werden (im Gegensatz zu einem Crash/Hänger).
    try:
        with open(ABORT_MARKER, "w") as f:
            f.write(datetime.now(timezone.utc).isoformat())
    except OSError:
        pass
    _resume["attempts"] = 0
    _resume["next_at"] = 0.0
    _cleanup_nas_run()
    # Lokalen Launcher/Tail beenden (das eigentliche Backup lief auf der NAS).
    proc = _backup_proc
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    try:
        os.unlink(BACKUP_LOCK)
    except OSError:
        pass
    log.info("Backup manuell abgebrochen (kein Auto-Resume)")
    return True, "Backup abgebrochen"


def _run_backup():
    global _backup_proc
    open(BACKUP_LOCK, "w").close()
    # Sofort-Feedback bei manuellem Start; der NAS-State-Sync haelt den Marker
    # danach ueber die gesamte (laenger als backup.sh laufende) NAS-Laufdauer.
    _set_backup_started_marker()
    try:
        _backup_proc = subprocess.Popen(["/scripts/backup.sh"])
        _backup_proc.wait()
    finally:
        _backup_proc = None
        try:
            os.unlink(BACKUP_LOCK)
        except OSError:
            pass
    if _mqtt_client:
        _mqtt_client.publish_state()


def _archive_run_log(status, ec, log_text):
    """Schreibt das vollständige NAS-run.log nach Abschluss persistent nach
    RUNS_DIR und rotiert auf die letzten RUNS_KEEP Läufe. So bleibt jeder Lauf
    auf hassio nachvollziehbar – auch wenn der Live-Spiegel backup.log durch
    einen Container-Neustart abgeschnitten wurde."""
    try:
        os.makedirs(RUNS_DIR, exist_ok=True)
        ts = datetime.now().astimezone()
        header = (
            f"# Offsite Backup – abgeschlossener Lauf\n"
            f"# status: {status} (rc={ec or '?'})\n"
            f"# finalisiert: {ts.isoformat()}\n"
            f"{'#' * 60}\n"
        )
        with open(os.path.join(RUNS_DIR, ts.strftime("backup-%Y%m%d_%H%M%S.log")), "w") as f:
            f.write(header)
            f.write(log_text if log_text else "(kein Log von der NAS erhalten)\n")
        files = sorted(
            os.path.join(RUNS_DIR, n) for n in os.listdir(RUNS_DIR)
            if n.startswith("backup-") and n.endswith(".log")
        )
        for old in files[:-RUNS_KEEP]:
            try:
                os.unlink(old)
            except OSError:
                pass
    except Exception as e:
        log.warning("Run-Log-Archiv fehlgeschlagen: %s", e)


def _finalize_from_nas():
    """Alleiniger Abschluss-Besitzer eines NAS-Laufs (läuft im langlebigen
    api.py-Prozess, überlebt also einen Container-Neustart, der den Launcher
    backup.sh killt). Holt das vollständige run.log VOR dem Aufräumen vom
    tmpfs-RunDir, archiviert es persistent, schreibt status.json und löscht
    dann erst das RunDir.
    Rückgabe: True wenn finalisiert oder nichts zu tun; False wenn die NAS
    nicht erreichbar war → der Watcher versucht es erneut (Log nicht verlieren)."""
    r = _nas_ssh(
        f"if [ -d {REMOTE_RUNDIR} ]; then printf 'DIR;'; "
        f"cat {REMOTE_RUNDIR}/exit_code 2>/dev/null; fi"
    )
    if r is None:
        return False  # NAS nicht erreichbar → später erneut versuchen
    if "DIR;" not in (r.stdout or ""):
        return True  # RunDir weg → bereits finalisiert
    ec = "".join(c for c in (r.stdout or "").split("DIR;", 1)[1] if c.isdigit())
    # Vollständiges Log holen, BEVOR das tmpfs-RunDir gelöscht wird.
    log_r = _nas_ssh(f"cat '{REMOTE_RUNDIR}/run.log' 2>/dev/null", timeout=30)
    if log_r is None:
        return False  # Log nicht erreicht → RunDir NICHT löschen, erneut versuchen
    log_text = log_r.stdout
    # RunDir entfernen UND die jetzt beendete screen-Session abräumen, damit eine
    # als „Dead" zurückbleibende Session weder als Post-Finalize-Stall
    # fehlklassifiziert wird noch den nächsten Lauf-Launcher („ALREADY_RUNNING")
    # blockiert. Der Lauf auf der NAS ist beendet – unabhängig vom Smoke.
    _nas_ssh(f"rm -rf {REMOTE_RUNDIR}; "
             f"screen -S {SCREEN_NAME} -X quit 2>/dev/null; "
             f"screen -wipe 2>/dev/null; true")

    # Transfer fehlgeschlagen → sofort failed, kein Smoke nötig.
    if ec != "0":
        _archive_run_log("failed", ec, log_text)
        if _log_is_permanent_failure(log_text):
            _mark_permanent_failure(f"Permanenter Offsite-Fehler (rc={ec}, z.B. Storagebox-Quota voll)")
            _write_final_status("failed", reason=f"Permanenter Fehler (Offsite-Quota o.ä., rc={ec}) – Auto-Resume gestoppt, manueller Eingriff nötig")
            log.error("Backup permanent fehlgeschlagen (rc=%s, Quota o.ä.) – Auto-Resume dauerhaft gestoppt (Marker gesetzt)", ec or "?")
        else:
            _write_final_status("failed", reason=f"Offsite-Transfer fehlgeschlagen (rc={ec})")
            log.info("Backup auf NAS fehlgeschlagen (rc=%s) – status.json=failed", ec or "?")
        return True

    # Transfer ok. Der Erfolgsstatus wird NICHT mehr allein aus rc=0 abgeleitet,
    # sondern durch den Recovery-Smoke-Test bestimmt (Wissen #751): success NUR,
    # wenn die Recovery-Umgebung die Offsite-Kopie verifiziert (Hosts + Zeiten +
    # Mini-Restore). Der Smoke läuft in einem eigenen Thread, damit der Watcher
    # antwortbereit bleibt; bis dahin steht der Status auf „verifying".
    if not _smoke_enabled():
        _archive_run_log("success", ec, log_text)
        _write_final_status("success")
        log.info("Backup auf NAS abgeschlossen (rc=0) – Smoke deaktiviert, status.json=success")
        return True

    _write_final_status("verifying", reason="Recovery-Smoke-Test läuft")
    log.info("Offsite-Transfer ok (rc=0) – starte Recovery-Smoke-Test zur Verifikation")
    threading.Thread(target=_smoke_and_finalize, args=(ec, log_text),
                     daemon=True).start()
    return True


def _write_status(status):
    try:
        with open(STATUS_FILE, "w") as f:
            json.dump({"status": status,
                       "last_run": datetime.now().astimezone().isoformat()}, f)
    except OSError:
        pass
    if _mqtt_client:
        try:
            _mqtt_client.publish_state()
        except Exception:
            pass


def _log_is_permanent_failure(text):
    """True, wenn ein Lauf-Log ein PERMANENTES Fehlerbild zeigt (z.B. Offsite-
    Storagebox-Quota voll). Solche Fehler wiederholen sich identisch – ein
    Auto-Resume wuerde nur getaktete Volllast-Laeufe (bis zum Host-OOM) erzeugen."""
    low = (text or "").lower()
    return any(m in low for m in PERMANENT_FAIL_MARKERS_TXT)


def _mark_permanent_failure(reason):
    """Persistenter Stopp-Marker (ueberlebt Container-Neustart/OOM, im Gegensatz
    zum In-Memory-_resume-Zaehler, der bei jedem OOM-Neustart auf 0 zuruecksetzt
    und so den 3-Versuche-Deckel unwirksam machte)."""
    try:
        with open(PERMANENT_FAIL_MARKER, "w") as f:
            f.write(f"{datetime.now().astimezone().isoformat()} {reason}")
    except OSError:
        pass


def _clear_permanent_failure():
    try:
        os.remove(PERMANENT_FAIL_MARKER)
    except OSError:
        pass


def _nas_run_shows_permanent_failure():
    """Prueft das aktuelle NAS-run.log (Tail) auf ein permanentes Fehlerbild."""
    try:
        r = _nas_ssh(f"tail -c 65536 '{REMOTE_RUNDIR}/run.log' 2>/dev/null", timeout=15)
        txt = r.stdout if (r is not None and r.returncode == 0) else ""
        return _log_is_permanent_failure(txt)
    except Exception:
        return False


def _auto_resume_enabled():
    return bool(read_options().get("auto_resume_backup", True))


def _handle_stuck(cls):
    """Hänger/Crash: aufräumen und – sofern kein manueller Abbruch und Auto-Resume
    aktiv – nach Backoff automatisch wiederaufnehmen (begrenzt auf MAX-Versuche)."""
    if os.path.exists(ABORT_MARKER):
        return  # manueller Abbruch → niemals automatisch wiederaufnehmen
    now = time.time()
    if os.path.exists(PERMANENT_FAIL_MARKER):
        # permanentes Fehlerbild bereits erkannt (persistenter Marker, ueberlebt
        # OOM-Neustart) → NIE Auto-Resume, nur einmal sauber als failed melden.
        if _resume["next_at"] != -1.0:
            log.error("Offsite-Backup hängt (%s) – permanenter Fehler-Marker aktiv, kein Auto-Resume", cls)
            _cleanup_nas_run()
            _write_final_status("failed", reason="Permanenter Fehler (Offsite-Quota o.ä.) – Auto-Resume gestoppt, manueller Eingriff nötig")
            _resume["next_at"] = -1.0
        return
    if not _auto_resume_enabled():
        if _resume["next_at"] == 0.0:
            log.warning("Offsite-Backup hängt (%s) – Auto-Resume deaktiviert, nur aufräumen", cls)
            _cleanup_nas_run()
            _write_status("failed")
            _resume["next_at"] = -1.0
        return
    if _resume["next_at"] == 0.0:
        # Permanentes Fehlerbild (z.B. Offsite-Quota voll) VOR dem Cleanup am
        # NAS-run.log erkennen – ein Auto-Resume waere sinnlos und OOM-treibend.
        if _nas_run_shows_permanent_failure():
            log.error("Offsite-Backup hängt (%s) – PERMANENTES Fehlerbild (Quota o.ä.) erkannt; Auto-Resume gestoppt (Marker gesetzt)", cls)
            _mark_permanent_failure("Permanenter Offsite-Fehler (Quota o.ä.) bei Hänger erkannt")
            _cleanup_nas_run()
            _write_final_status("failed", reason="Permanenter Fehler (Offsite-Quota o.ä.) – Auto-Resume gestoppt, manueller Eingriff nötig")
            _resume["next_at"] = -1.0
            return
        # Erstes Erkennen dieses Hängers: aufräumen + Wiederaufnahme planen.
        log.warning("Offsite-Backup hängt (%s) – aufräumen + Wiederaufnahme planen", cls)
        _cleanup_nas_run()
        _write_status("failed")
        if _resume["attempts"] >= MAX_RESUME_ATTEMPTS:
            log.error("Auto-Resume: Max. Versuche (%d) erreicht – gebe auf", MAX_RESUME_ATTEMPTS)
            _resume["next_at"] = -1.0
            return
        _resume["next_at"] = now + RESUME_BACKOFF_SECS
        log.warning("Auto-Resume in %d min geplant (Versuch %d/%d)",
                    RESUME_BACKOFF_SECS // 60, _resume["attempts"] + 1, MAX_RESUME_ATTEMPTS)


def _maybe_fire_resume():
    """Löst eine fällige, geplante Wiederaufnahme aus — zustandsunabhängig, da
    der Lauf nach dem Cleanup wieder `idle` ist."""
    if (_resume["next_at"] > 0 and time.time() >= _resume["next_at"]
            and not os.path.exists(ABORT_MARKER)
            and not os.path.exists(PERMANENT_FAIL_MARKER)
            and _auto_resume_enabled()):
        _resume["attempts"] += 1
        _resume["next_at"] = 0.0
        log.warning("Auto-Resume #%d des Offsite-Backups wird gestartet", _resume["attempts"])
        trigger_backup(_auto=True)


def _nas_watch_loop():
    """Zustandsmaschine über die NAS-Sonde: finalisiert beendete Läufe (auch ohne
    lebenden Launcher → Container-Neustart-Resilienz), erkennt Hänger/Crashes und
    nimmt das Backup nach Backoff automatisch wieder auf — außer es wurde manuell
    abgebrochen (Marker) oder Auto-Resume ist deaktiviert."""
    pending = False
    while True:
        time.sleep(20)
        try:
            cls = _nas_backup_state(force=True)["class"]
            if cls == "finished" or pending:
                pending = not _finalize_from_nas()
                if not pending:
                    _resume["attempts"] = 0
                    _resume["next_at"] = 0.0
            elif cls == "running":
                _resume["next_at"] = 0.0   # echte Aktivität → keine Wiederaufnahme nötig
            elif cls in ("stalled", "crashed"):
                _handle_stuck(cls)
            elif cls == "idle":
                _maybe_fire_resume()   # geplante Wiederaufnahme nach Cleanup auslösen
            # unknown → nichts tun
        except Exception as e:
            log.warning("NAS-Watch Fehler: %s", e)


def trigger_recovery(action, snapshot_name=""):
    try:
        if action == "start":
            opts = read_options()
            # Der Supervisor /options-Endpoint ersetzt die Optionen vollständig
            # und validiert gegen das komplette Schema – alle Pflichtfelder müssen
            # mit. offsite_path + backup_sources gehören seit 2.1.0 dazu; sie an
            # die Recovery durchzureichen spiegelt zugleich das Backup-Mapping 1:1.
            _supervisor_request("POST", f"/addons/{RECOVERY_ADDON_SLUG}/options", {
                "options": {
                    "offsite_user":    opts.get("offsite_user", ""),
                    "offsite_host":    opts.get("offsite_host", ""),
                    "offsite_port":    int(opts.get("offsite_port", 23)),
                    "offsite_path":    opts.get("offsite_path", "/home"),
                    "snapshot_name":   snapshot_name,
                    "mqtt_host":       opts.get("mqtt_host", ""),
                    "mqtt_port":       int(opts.get("mqtt_port", 1883)),
                    "mqtt_user":       opts.get("mqtt_user", ""),
                    "mqtt_password":   opts.get("mqtt_password", ""),
                    "ssh_key_offsite": opts.get("ssh_key_offsite", ""),
                    "backup_sources":  opts.get("backup_sources", []),
                }
            })
            _supervisor_request("POST", f"/addons/{RECOVERY_ADDON_SLUG}/start")
        else:
            _supervisor_request("POST", f"/addons/{RECOVERY_ADDON_SLUG}/stop")
        if _mqtt_client:
            threading.Timer(3, _mqtt_client.publish_state).start()
        return True, f"Recovery {action} ausgelöst"
    except Exception as e:
        log.warning("Recovery %s fehlgeschlagen: %s", action, e)
        return False, f"Recovery {action} fehlgeschlagen: {e}"


# ── Recovery-Smoke-Test-Orchestrierung (Wissen #751) ─────────────────────────
_smoke_active = threading.Lock()


def _smoke_enabled():
    return bool(read_options().get("smoke_test_after_backup", True))


def _write_final_status(status, reason="", smoke=None):
    """Einzige autoritative status.json-Schreibstelle für den Abschluss. Enthält
    optional Grund + Smoke-Zusammenfassung (Beobachtbarkeit)."""
    if status == "success":
        # erfolgreicher Lauf – persistenten Permanent-Fehler-Marker aufheben
        _clear_permanent_failure()
    payload = {"status": status,
               "last_run": datetime.now().astimezone().isoformat()}
    if reason:
        payload["reason"] = reason
    if smoke is not None:
        checks = smoke.get("checks") or {}
        payload["smoke"] = {
            "ok": smoke.get("ok"),
            "reason": smoke.get("reason", ""),
            "target": smoke.get("target"),
            "skipped": bool(smoke.get("skipped", False)),
            "checks": {k: bool(v.get("ok")) for k, v in checks.items()
                       if isinstance(v, dict)},
        }
    try:
        with open(STATUS_FILE, "w") as fobj:
            json.dump(payload, fobj)
    except OSError:
        pass
    if _mqtt_client:
        try:
            _mqtt_client.publish_state()
        except Exception:
            pass


def _recovery_smoke_url():
    return (f"http://{RECOVERY_ADDON_SLUG.replace('_', '-')}"
            f".local.hass.io:9080/smoke")


def _poll_recovery_smoke(timeout=SMOKE_POLL_TIMEOUT):
    """Pollt den Recovery-HTTP-Status auf ein fertiges Smoke-Ergebnis.
    Rückgabe: result-dict oder None (Timeout)."""
    url = _recovery_smoke_url()
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                data = json.loads(r.read())
            if data.get("status") in ("done", "error"):
                return data.get("result")
        except Exception:
            pass  # Recovery bootet ggf. noch – weiter pollen
        time.sleep(SMOKE_POLL_INTERVAL)
    return None


def run_recovery_smoke(snapshot_name=""):
    """Startet die Recovery-Umgebung, holt das Smoke-Ergebnis, stoppt sie wieder.
    Rückgabe: (ok, reason, result). Läuft die Recovery bereits (manuelle Nutzung),
    wird der Smoke übersprungen (ok=True, skipped) statt die Sitzung zu stören."""
    if is_recovery_running():
        log.warning("Recovery-Umgebung in Benutzung – Smoke übersprungen")
        return True, "", {"ok": True, "skipped": True,
                          "reason": "Recovery-Umgebung in Benutzung – Smoke übersprungen",
                          "checks": {}, "target": {"host": None, "num": None}}
    ok_start, msg = trigger_recovery("start", snapshot_name)
    if not ok_start:
        return False, f"Recovery-Start fehlgeschlagen: {msg}", None
    try:
        result = _poll_recovery_smoke()
    finally:
        trigger_recovery("stop")
    if result is None:
        return False, "Smoke-Test Timeout (kein Ergebnis vom Recovery-Addon)", None
    return bool(result.get("ok")), result.get("reason", ""), result


def _smoke_and_finalize(ec, log_text):
    """Läuft im eigenen Thread nach erfolgreichem Transfer: Recovery-Smoke → der
    Erfolgsstatus ergibt sich AUSSCHLIESSLICH aus dem Smoke-Ergebnis."""
    if not _smoke_active.acquire(blocking=False):
        log.warning("Smoke bereits aktiv – überspringe zweiten Lauf")
        return
    try:
        ok, reason, result = run_recovery_smoke("")
        status = "success" if ok else "failed"
        note = f"\n{'#' * 60}\n# Recovery-Smoke-Test: ok={ok} – {reason or 'alle Checks grün'}\n"
        _archive_run_log(status, ec, (log_text or "") + note)
        _write_final_status(status, reason=("" if ok else reason), smoke=result)
        log.info("Recovery-Smoke abgeschlossen: status=%s reason=%s", status, reason or "-")
    finally:
        _smoke_active.release()


def list_snapshots():
    try:
        with open("/data/secrets/offsite_token") as f:
            token = f.read().strip()
    except Exception:
        return None, "offsite_token nicht gefunden (/data/secrets/offsite_token)"

    opts = read_options()
    box_id = opts.get("offsite_box_id", "")
    if not box_id:
        return None, "offsite_box_id nicht konfiguriert"

    req = urllib.request.Request(
        f"https://api.hetzner.com/v1/storage_boxes/{box_id}/snapshots",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read()), None
    except Exception as e:
        return None, str(e)



_offsite_info_cache = {"data": None, "ts": 0.0}
_OFFSITE_CACHE_TTL = 300   # 5 Minuten


def get_offsite_box_info(force=False):
    """Hetzner Storage-Box: Platz + eigene Snapshot-Zähler — gecacht 5 min."""
    global _offsite_info_cache
    now = time.time()
    if (not force and _offsite_info_cache["data"] is not None
            and (now - _offsite_info_cache["ts"]) < _OFFSITE_CACHE_TTL):
        return _offsite_info_cache["data"], None

    try:
        with open("/data/secrets/offsite_token") as f:
            token = f.read().strip()
    except Exception:
        return None, "offsite_token nicht gefunden"

    opts = read_options()
    box_id = opts.get("offsite_box_id", "")
    if not box_id:
        return None, "offsite_box_id nicht konfiguriert"
    snapshot_keep = int(opts.get("offsite_snapshot_keep", 20) or 20)

    # Platz-Info über Storage-Box-Endpunkt
    disk_used_mb = disk_quota_mb = 0
    try:
        req = urllib.request.Request(
            f"https://api.hetzner.com/v1/storage_boxes/{box_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            sb = json.loads(resp.read()).get("storage_box", {})
        # Hetzner Cloud API: stats.size = Gesamtbelegung (Bytes), storage_box_type.size = Kapazität (Bytes)
        disk_used_mb  = int((sb.get("stats") or {}).get("size",  0) or 0) // (1024 * 1024)
        disk_quota_mb = int((sb.get("storage_box_type") or {}).get("size", 0) or 0) // (1024 * 1024)
    except Exception as e:
        log.warning("Hetzner Box-Info nicht abrufbar: %s", e)

    # Snapshot-Liste — nur eigene (Beschreibung beginnt mit "Snap_")
    snaps = []
    own_count = 0
    try:
        req = urllib.request.Request(
            f"https://api.hetzner.com/v1/storage_boxes/{box_id}/snapshots",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            all_snaps = json.loads(resp.read()).get("snapshots", [])
        own = sorted(
            [s for s in all_snaps if (s.get("description") or "").startswith("Snap_")],
            key=lambda s: s.get("created", ""), reverse=True,
        )
        snaps = [{"name": s.get("name", ""), "created": s.get("created", ""),
                  "description": s.get("description", "")} for s in own]
        own_count = len(own)
    except Exception as e:
        log.warning("Hetzner Snapshot-Liste nicht abrufbar: %s", e)

    result = {
        "disk_used_mb":  disk_used_mb,
        "disk_quota_mb": disk_quota_mb,
        "disk_used_gb":  round(disk_used_mb  / 1024, 1) if disk_quota_mb else None,
        "disk_quota_gb": round(disk_quota_mb / 1024, 1) if disk_quota_mb else None,
        "disk_pct":      round(disk_used_mb / disk_quota_mb * 100, 1) if disk_quota_mb else None,
        "snapshot_count": own_count,
        "snapshot_keep":  snapshot_keep,
        "snapshots": snaps,
    }
    _offsite_info_cache = {"data": result, "ts": now}
    return result, None



# ── PBS/PVE Recovery API ──────────────────────────────────────────────────────

def _proxmox_api(base_url, auth_header, method, path, body=None, timeout=20):
    """Generischer Proxmox/PBS API Call – self-signed TLS wird akzeptiert."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    url = base_url.rstrip("/") + path
    headers = dict(auth_header)
    data = None
    if body:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as r:
            return json.loads(r.read()), None
    except urllib.error.HTTPError as e:
        msg = e.read().decode(errors="replace")[:300]
        return None, f"HTTP {e.code}: {msg}"
    except Exception as e:
        return None, str(e)


def _pbs_api(method, path, body=None):
    """PBS API Call gegen konfigurierten PBS-Server."""
    opts = read_options()
    host = opts.get("pbs_server_host", "")
    port = int(opts.get("pbs_server_port", 8007))
    token = opts.get("pbs_api_token", "")
    if not host or not token:
        return None, "PBS nicht konfiguriert (pbs_server_host/pbs_api_token fehlt)"
    return _proxmox_api(
        f"https://{host}:{port}",
        {"Authorization": f"PBSAPIToken={token}"},
        method, path, body,
    )


def _pve_api(method, path, body=None):
    """PVE API Call gegen konfigurierten PVE-Host."""
    opts = read_options()
    host = opts.get("pve_host", "")
    token = opts.get("pve_api_token", "")
    if not host or not token:
        return None, "PVE nicht konfiguriert (pve_host/pve_api_token fehlt)"
    return _proxmox_api(
        f"https://{host}:8006",
        {"Authorization": f"PVEAPIToken={token}"},
        method, path, body,
    )


_PBS_SNAP_CACHE = {"data": None, "ts": 0.0}
_PBS_SNAP_TTL = 120.0  # 2 Minuten


def get_pbs_snapshots(force=False):
    """Listet PBS-Snapshots via rsync --list-only auf Hetzner — gecacht 2 min.
    Pfad: <offsite_path>/ZPool/PBS/NAS/ns/<namespace>/{vm,ct}/<id>/<timestamp>/
    Hetzner Storage Box hat restricted shell (kein find/ls), rsync ist erlaubt."""
    global _PBS_SNAP_CACHE
    now = time.time()
    if (not force and _PBS_SNAP_CACHE["data"] is not None
            and (now - _PBS_SNAP_CACHE["ts"]) < _PBS_SNAP_TTL):
        return _PBS_SNAP_CACHE["data"], None
    opts = read_options()
    host = opts.get("offsite_host", "")
    user = opts.get("offsite_user", "")
    port = int(opts.get("offsite_port", 23))
    base = opts.get("offsite_path", "/home")
    ns   = opts.get("pbs_namespace", "GVMHP") or "GVMHP"
    if not host or not user or not os.path.exists(OFFSITE_KEY):
        return None, "Offsite-Verbindung nicht konfiguriert (offsite_host/user/key fehlt)"
    # PBS-Namespaces liegen unter ns/ innerhalb des Datastores
    pbs_base = f"{base}/ZPool/PBS/NAS/ns/{ns}"
    ssh_opt = (f"ssh -p {port} -i {OFFSITE_KEY} "
               f"-o StrictHostKeyChecking=no -o BatchMode=yes -o ConnectTimeout=15")
    result = []
    errors = []
    for btype in ("vm", "ct"):
        cmd = [
            "rsync", "-e", ssh_opt,
            "--list-only", "-r",
            f"{user}@{host}:{pbs_base}/{btype}/",
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except Exception as e:
            errors.append(f"{btype}: {e}")
            continue
        if r.returncode not in (0, 23):  # 23 = partial (Verzeichnis nicht vorhanden)
            errors.append(f"{btype}: rsync rc={r.returncode}: {r.stderr.strip()[:100]}")
            continue
        for line in r.stdout.splitlines():
            # rsync --list-only Format: "drwxr-xr-x  N YYYY/MM/DD HH:MM:SS relpath"
            # Snapshot-Zeilen: relpath = "<vmid>/2026-09-21T00:30:04Z"
            parts = line.split()
            if len(parts) < 5:
                continue
            path = parts[4]
            path_parts = path.split("/")
            if len(path_parts) != 2:
                continue
            bid, btime_str = path_parts
            # Nur ISO-Timestamp-Verzeichnisse (nicht "owner" o.ä.)
            if len(btime_str) < 16 or btime_str[4] != '-' or 'T' not in btime_str:
                continue
            try:
                dt = datetime.fromisoformat(btime_str.replace("Z", "+00:00"))
                bt = int(dt.timestamp())
            except ValueError:
                continue
            result.append({
                "backup_type":     btype,
                "backup_id":       bid,
                "backup_time":     bt,
                "backup_time_iso": btime_str,
                "size":            0,
                "protected":       False,
            })
    if not result and errors:
        return None, "; ".join(errors)
    result.sort(key=lambda x: (x["backup_type"], x["backup_id"], x["backup_time"]), reverse=True)
    _PBS_SNAP_CACHE = {"data": result, "ts": now}
    return result, None


def restore_from_pbs(backup_type, backup_id, backup_time_ts,
                     pve_node, target_storage, target_vmid=None):
    """Startet Restore-Job PBS → PVE.
    Gibt UPID (Task-ID) zurück oder (None, Fehlermeldung)."""
    opts = read_options()
    pbs_storage = opts.get("pbs_pve_storage_name", "PBS-GVMHP") or "PBS-GVMHP"
    dt_str = datetime.fromtimestamp(int(backup_time_ts)).strftime("%Y-%m-%dT%H:%M:%SZ")
    archive = f"{pbs_storage}:{backup_type}/{backup_id}/{dt_str}"
    vm_id = int(target_vmid) if target_vmid else int(backup_id)
    if backup_type == "vm":
        endpoint = f"/api2/json/nodes/{pve_node}/qemu"
        payload = {"vmid": vm_id, "restore": 1,
                   "storage": target_storage, "archive": archive}
    else:
        endpoint = f"/api2/json/nodes/{pve_node}/lxc"
        payload = {"vmid": vm_id, "restore": 1,
                   "storage": target_storage, "ostemplate": archive}
    data, err = _pve_api("POST", endpoint, payload)
    if err:
        return None, err
    return data.get("data"), None  # UPID


def get_pve_task_status(pve_node, upid):
    """Pollt den Status eines PVE Task (UPID)."""
    encoded = urllib.parse.quote(upid, safe="")
    data, err = _pve_api("GET", f"/api2/json/nodes/{pve_node}/tasks/{encoded}/status")
    if err:
        return None, err
    return data.get("data", {}), None



# ── PBS LXC Container Recovery ─────────────────────────────────────────────

PBS_LXC_STATUS_FILE = "/data/pbs_lxc_restore_status.json"
PBS_LXC_LOG_FILE    = "/data/logs/pbs_lxc_restore.log"
_PBS_LXC_THREAD = None

def _pbs_lxc_log(msg):
    os.makedirs("/data/logs", exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} | {msg}\n"
    log.info("PBS-LXC-Recovery: %s", msg)
    try:
        with open(PBS_LXC_LOG_FILE, "a") as f:
            f.write(line)
    except Exception:
        pass

def _pbs_lxc_status_write(status, step="", msg="", error=""):
    data = {"status": status, "step": step, "msg": msg, "error": error, "ts": time.time()}
    try:
        with open(PBS_LXC_STATUS_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass

def get_pbs_lxc_restore_status():
    try:
        with open(PBS_LXC_STATUS_FILE) as f:
            st = json.load(f)
        # Log-Tail anhängen
        try:
            with open(PBS_LXC_LOG_FILE) as f:
                lines = f.readlines()
            st["log_tail"] = "".join(lines[-30:])
        except Exception:
            st["log_tail"] = ""
        return st
    except Exception:
        return {"status": "idle", "step": "", "msg": "", "error": "", "log_tail": ""}

def list_vzdump_snapshots():
    """Listet vzdump-lxc-*-Dateien auf Hetzner via rsync --list-only (OFFSITE_KEY)."""
    opts = read_options()
    host = opts.get("offsite_host", "")
    user = opts.get("offsite_user", "")
    port = int(opts.get("offsite_port", 23))
    base = opts.get("offsite_path", "/home")
    dump_path = opts.get("pbs_lxc_hetzner_dump_path", "ZPool/VMGuest/VMBackup/dump")
    if not host or not user or not os.path.exists(OFFSITE_KEY):
        return None, "Offsite-Verbindung nicht konfiguriert"
    ssh_opt = (f"ssh -p {port} -i {OFFSITE_KEY} "
               f"-o StrictHostKeyChecking=no -o BatchMode=yes -o ConnectTimeout=15")
    cmd = ["rsync", "-e", ssh_opt, "--list-only",
           f"{user}@{host}:{base}/{dump_path}/"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except Exception as e:
        return None, str(e)
    if r.returncode not in (0, 23):
        return None, f"rsync rc={r.returncode}: {r.stderr.strip()[:200]}"
    result = []
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        fname = parts[4]
        if not (fname.startswith("vzdump-lxc-") and fname.endswith(".tar.zst")):
            continue
        try:
            size_bytes = int(parts[1].replace(",", ""))
        except ValueError:
            size_bytes = 0
        result.append({
            "filename": fname,
            "size_bytes": size_bytes,
            "size_gb": round(size_bytes / 1024**3, 2),
            "date_str": parts[2],  # YYYY/MM/DD
        })
    result.sort(key=lambda x: x["date_str"], reverse=True)
    return result, None

def _nas_ssh_long(remote_cmd, timeout=7200):
    """Wie _nas_ssh aber mit langem Timeout für rsync-Operationen."""
    opts = read_options()
    host = opts.get("zfs_storage_host", "")
    user = opts.get("zfs_storage_user", "root") or "root"
    if not host or not os.path.exists(NAS_KEY):
        return None
    cmd = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
           "-o", "ConnectTimeout=30", "-i", NAS_KEY, f"{user}@{host}"]
    try:
        return subprocess.run(cmd, input=remote_cmd, capture_output=True,
                              text=True, timeout=timeout)
    except Exception as e:
        _pbs_lxc_log(f"_nas_ssh_long Exception: {e}")
        return None

def _do_pbs_lxc_restore(vzdump_file, pve_node, target_vmid, os_storage,
                         data_zfs_dataset, offsite_pbs_path):
    """Background-Thread: PBS-Container vollständig wiederherstellen."""
    def step(n, msg):
        _pbs_lxc_log(f"[Schritt {n}/5] {msg}")
        _pbs_lxc_status_write("running", f"Schritt {n}/5", msg)
    try:
        opts = read_options()
        offsite_host = opts.get("offsite_host", "")
        offsite_user = opts.get("offsite_user", "")
        offsite_port = int(opts.get("offsite_port", 23))
        offsite_base = opts.get("offsite_path", "/home")
        nas_hetzner_key = opts.get("nas_hetzner_key", "/root/offsite-restore/id_ed25519_restore")
        vmid = int(target_vmid)
        tmp_dir = "/tmp/pbs-lxc-restore"
        vzdump_remote_path = f"{offsite_base}/{opts.get('pbs_lxc_hetzner_dump_path', 'ZPool/VMGuest/VMBackup/dump')}/{vzdump_file}"
        vzdump_local = f"{tmp_dir}/{vzdump_file}"
        data_mount = f"/{data_zfs_dataset}"
        pbs_src = f"{offsite_base}/{offsite_pbs_path}/"
        pbs_dst = f"{data_mount}/"

        # Schritt 1: vzdump von Hetzner → NAS
        step(1, f"Lade {vzdump_file} von Hetzner → NAS ...")
        r = _nas_ssh_long(f"""
set -e
mkdir -p {tmp_dir}
rsync -a --info=progress2 \
  -e "ssh -p {offsite_port} -i {nas_hetzner_key} -o BatchMode=yes -o StrictHostKeyChecking=no" \
  "{offsite_user}@{offsite_host}:{vzdump_remote_path}" \
  "{tmp_dir}/"
echo "vzdump_ok"
""", timeout=3600)
        out = (r.stdout or "") if r else ""
        if not r or r.returncode != 0 or "vzdump_ok" not in out:
            err = (r.stderr.strip()[-400:] if r else "NAS SSH nicht verfügbar")
            raise RuntimeError(f"Schritt 1 fehlgeschlagen: {err}")

        # Schritt 2: ZFS-Dataset anlegen
        step(2, f"Erstelle ZFS-Dataset {data_zfs_dataset} ...")
        r = _nas_ssh_long(f"""
set -e
if ! zfs list {data_zfs_dataset} >/dev/null 2>&1; then
  zfs create -p {data_zfs_dataset}
  echo "dataset_created"
else
  echo "dataset_exists"
fi
""", timeout=30)
        if not r or r.returncode != 0:
            err = (r.stderr.strip()[-200:] if r else "NAS SSH Fehler")
            raise RuntimeError(f"Schritt 2 fehlgeschlagen: {err}")
        _pbs_lxc_log(f"ZFS: {(r.stdout or '').strip()}")

        # Schritt 3: pct restore (direkt auf NAS via SSH)
        step(3, f"Stelle LXC {vmid} auf {pve_node} wieder her (pct restore) ...")
        r = _nas_ssh_long(f"""
set -e
pct restore {vmid} {vzdump_local} \
  --storage {os_storage} \
  --mp0 {data_mount},mp=/PBS \
  --force 1 2>&1
echo "pct_restore_done:$?"
""", timeout=1800)
        out = (r.stdout or "") if r else ""
        if not r or ("pct_restore_done:0" not in out):
            err = out[-400:] if out else ((r.stderr.strip()[-400:] if r else "NAS SSH Fehler"))
            raise RuntimeError(f"Schritt 3 pct restore Fehler: {err}")
        _pbs_lxc_log(f"pct restore Output: {out[-200:]}")

        # Schritt 4: PBS-Daten von Hetzner → Dataset syncen
        step(4, f"Sync PBS-Daten von Hetzner → {data_zfs_dataset} ...")
        r = _nas_ssh_long(f"""
set -e
mkdir -p {pbs_dst}
rsync -a --info=progress2 \
  -e "ssh -p {offsite_port} -i {nas_hetzner_key} -o BatchMode=yes -o StrictHostKeyChecking=no" \
  "{offsite_user}@{offsite_host}:{pbs_src}" \
  "{pbs_dst}"
echo "pbs_sync_ok"
""", timeout=21600)
        out = (r.stdout or "") if r else ""
        if not r or (r.returncode not in (0, 24)) or "pbs_sync_ok" not in out:
            err = (r.stderr.strip()[-400:] if r else "NAS SSH Fehler")
            raise RuntimeError(f"Schritt 4 rsync PBS-Daten Fehler: {err}")

        # Schritt 5: Container starten
        step(5, f"Starte LXC {vmid} ...")
        r = _nas_ssh_long(f"""
pct start {vmid} 2>&1
echo "start_done:$?"
""", timeout=60)
        out = (r.stdout or "") if r else ""
        if not r or "start_done:0" not in out:
            _pbs_lxc_log(f"WARN: pct start Fehler (manuell starten): {out[-200:]}")
        else:
            _pbs_lxc_log("Container gestartet.")

        # Cleanup
        _nas_ssh_long(f"rm -rf {tmp_dir}", timeout=60)
        _pbs_lxc_log("PBS-Container-Recovery abgeschlossen.")
        _pbs_lxc_status_write("done", "Fertig", "PBS-Container erfolgreich wiederhergestellt")

    except Exception as e:
        _pbs_lxc_log(f"FEHLER: {e}")
        _pbs_lxc_status_write("error", "", "", str(e))

def start_pbs_lxc_restore(vzdump_file, pve_node, target_vmid, os_storage,
                           data_zfs_dataset, offsite_pbs_path):
    global _PBS_LXC_THREAD
    if _PBS_LXC_THREAD and _PBS_LXC_THREAD.is_alive():
        return False, "Restore läuft bereits"
    if not vzdump_file or not target_vmid or not os_storage or not data_zfs_dataset:
        return False, "Pflichtfelder fehlen"
    # Log rotieren
    try:
        if os.path.exists(PBS_LXC_LOG_FILE):
            os.rename(PBS_LXC_LOG_FILE, PBS_LXC_LOG_FILE + ".bak")
    except Exception:
        pass
    _pbs_lxc_status_write("running", "Initialisierung", "Recovery wird gestartet...")
    _pbs_lxc_log(f"Starte Recovery: {vzdump_file}, Node={pve_node}, VMID={target_vmid}, Storage={os_storage}, Dataset={data_zfs_dataset}")
    _PBS_LXC_THREAD = threading.Thread(
        target=_do_pbs_lxc_restore,
        args=(vzdump_file, pve_node, target_vmid, os_storage, data_zfs_dataset, offsite_pbs_path),
        daemon=True,
    )
    _PBS_LXC_THREAD.start()
    return True, "Recovery gestartet"

# ── BackupPC Docker Restore ─────────────────────────────────────────────────

BPPC_STATUS_FILE = "/data/backuppc_restore_status.json"
BPPC_LOG_FILE    = "/data/logs/backuppc_restore.log"
_BPPC_THREAD = None

def _bppc_log(msg):
    os.makedirs("/data/logs", exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} | {msg}\n"
    log.info("BackupPC-Recovery: %s", msg)
    try:
        with open(BPPC_LOG_FILE, "a") as f:
            f.write(line)
    except Exception:
        pass

def _bppc_status_write(status, step="", msg="", error=""):
    data = {"status": status, "step": step, "msg": msg, "error": error, "ts": time.time()}
    try:
        with open(BPPC_STATUS_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass

def get_bppc_restore_status():
    try:
        with open(BPPC_STATUS_FILE) as f:
            st = json.load(f)
        try:
            with open(BPPC_LOG_FILE) as f:
                lines = f.readlines()
            st["log_tail"] = "".join(lines[-30:])
        except Exception:
            st["log_tail"] = ""
        return st
    except Exception:
        return {"status": "idle", "step": "", "msg": "", "error": "", "log_tail": ""}

def _do_bppc_restore(docker_host, container_name, data_path, config_path,
                     home_path, sshconfig_path, offsite_snapshot=""):
    """Background-Thread: BackupPC-Docker vollständig von Hetzner wiederherstellen."""
    def step(n, msg):
        _bppc_log(f"[Schritt {n}/4] {msg}")
        _bppc_status_write("running", f"Schritt {n}/4", msg)
    try:
        opts = read_options()
        offsite_host  = opts.get("offsite_host", "")
        offsite_user  = opts.get("offsite_user", "")
        offsite_port  = int(opts.get("offsite_port", 23))
        offsite_base  = opts.get("offsite_path", "/home")
        nas_key       = opts.get("nas_hetzner_key", "/root/offsite-restore/id_ed25519_restore")

        # Snapshot-Präfix für Pfade
        if offsite_snapshot:
            snap_prefix = f"{offsite_base}/.snapshots/{offsite_snapshot}"
        else:
            snap_prefix = offsite_base

        ssh_e = (f"ssh -i {nas_key} -p {offsite_port} "
                 f"-o BatchMode=yes -o StrictHostKeyChecking=no")

        # Schritt 1: BackupPC Container stoppen (über NAS per pct exec)
        step(1, f"Stoppe BackupPC-Container '{container_name}' ...")
        r = _nas_ssh_long(f"""
set -e
pct exec 900 -- docker stop {container_name} 2>&1 || true
echo "stop_done"
""", timeout=60)
        out = (r.stdout or "") if r else ""
        if not r or "stop_done" not in out:
            err = (r.stderr.strip()[-200:] if r else "NAS SSH nicht verfügbar")
            raise RuntimeError(f"Schritt 1 fehlgeschlagen: {err}")
        _bppc_log(f"Container gestoppt. Output: {out.strip()[-100:]}")

        # Schritt 2: 4x rsync Hetzner → NAS
        step(2, "Synchronisiere Daten von Hetzner → NAS (4 Pfade) ...")

        rsync_pairs = [
            (f"{snap_prefix}/ZPool/BackupPC/",              f"{data_path}/"),
            (f"{snap_prefix}/ZPool/Docker/backuppc/config/", f"{config_path}/"),
            (f"{snap_prefix}/ZPool/Docker/backuppc/home/",   f"{home_path}/"),
            (f"{snap_prefix}/ZPool/Docker/backuppc/ssh_config/", f"{sshconfig_path}/"),
        ]

        for idx, (src, dst) in enumerate(rsync_pairs, 1):
            _bppc_log(f"  rsync {idx}/4: {src} → {dst}")
            r = _nas_ssh_long(f"""
set -e
mkdir -p "{dst}"
rsync -avz --delete \\
  -e "{ssh_e}" \\
  "{offsite_user}@{offsite_host}:{src}" \\
  "{dst}"
echo "rsync_{idx}_ok"
""", timeout=21600)
            out = (r.stdout or "") if r else ""
            if not r or (r.returncode not in (0, 24)) or f"rsync_{idx}_ok" not in out:
                err = (r.stderr.strip()[-400:] if r else "NAS SSH Fehler")
                raise RuntimeError(f"Schritt 2 rsync {idx}/4 fehlgeschlagen: {err}")
            _bppc_log(f"  rsync {idx}/4 OK")

        # Schritt 3: Container neu starten (über NAS per pct exec)
        step(3, f"Starte BackupPC-Container '{container_name}' neu ...")
        r = _nas_ssh_long(f"""
set -e
pct exec 900 -- docker start {container_name} 2>&1
echo "start_done:$?"
""", timeout=60)
        out = (r.stdout or "") if r else ""
        if not r or "start_done:0" not in out:
            _bppc_log(f"WARN: docker start Fehler (ggf. manuell starten): {out[-200:]}")
        else:
            _bppc_log("Container gestartet.")

        # Schritt 4: Smoke-Test HTTP GET http://<docker_host>:8080
        step(4, f"Smoke-Test: HTTP GET http://{docker_host}:8080 ...")
        import urllib.request as _ureq
        smoke_ok = False
        for attempt in range(1, 6):
            _bppc_log(f"  Smoke-Test Versuch {attempt}/5 ...")
            try:
                req = _ureq.Request(f"http://{docker_host}:8080",
                                    method="GET")
                with _ureq.urlopen(req, timeout=30) as resp:
                    if resp.status < 500:
                        smoke_ok = True
                        break
            except Exception as se:
                _bppc_log(f"  Versuch {attempt} fehlgeschlagen: {se}")
                if attempt < 5:
                    time.sleep(10)

        if smoke_ok:
            _bppc_log("Smoke-Test bestanden.")
        else:
            _bppc_log("WARN: Smoke-Test fehlgeschlagen — Container läuft möglicherweise noch nicht.")

        _bppc_log("BackupPC-Recovery abgeschlossen.")
        _bppc_status_write("done", "Fertig", "BackupPC erfolgreich wiederhergestellt")

    except Exception as e:
        _bppc_log(f"FEHLER: {e}")
        _bppc_status_write("error", "", "", str(e))

def start_bppc_restore(docker_host, container_name, data_path, config_path,
                       home_path, sshconfig_path, offsite_snapshot=""):
    global _BPPC_THREAD
    if _BPPC_THREAD and _BPPC_THREAD.is_alive():
        return False, "Restore läuft bereits"
    if not container_name or not data_path:
        return False, "Pflichtfelder fehlen (container_name, data_path)"
    try:
        if os.path.exists(BPPC_LOG_FILE):
            os.rename(BPPC_LOG_FILE, BPPC_LOG_FILE + ".bak")
    except Exception:
        pass
    _bppc_status_write("running", "Initialisierung", "Recovery wird gestartet...")
    _bppc_log(f"Starte Recovery: docker_host={docker_host}, container={container_name}, "
              f"data={data_path}, snapshot='{offsite_snapshot}'")
    _BPPC_THREAD = threading.Thread(
        target=_do_bppc_restore,
        args=(docker_host, container_name, data_path, config_path,
              home_path, sshconfig_path, offsite_snapshot),
        daemon=True,
    )
    _BPPC_THREAD.start()
    return True, "Recovery gestartet"


class MQTTClient:
    DEVICE = {
        "identifiers": ["offsite_backup"],
        "name": "Offsite Backup",
        "model": "HA Add-on v1.0",
        "manufacturer": "XtraLarge",
    }
    STATE_TOPIC = "offsite_backup/state"
    DISCOVERY_ENTITIES = [
        ("sensor", "offsite_backup_status", {
            "name": "Backup Status",
            "state_topic": "offsite_backup/state",
            "value_template": "{{ value_json.status }}",
            "icon": "mdi:cloud-check",
        }),
        ("sensor", "offsite_backup_last_run", {
            "name": "Letzter Backup",
            "device_class": "timestamp",
            "state_topic": "offsite_backup/state",
            "value_template": "{{ value_json.last_run }}",
            "icon": "mdi:clock-check",
        }),
        ("sensor", "offsite_backup_next_run", {
            "name": "Nächster Backup",
            "device_class": "timestamp",
            "state_topic": "offsite_backup/state",
            "value_template": "{{ value_json.next_run }}",
            "icon": "mdi:clock-outline",
        }),
        ("sensor", "offsite_backup_progress", {
            "name": "Backup Fortschritt",
            "state_topic": "offsite_backup/state",
            "value_template": "{{ value_json.progress }}",
            "icon": "mdi:progress-upload",
        }),
        ("binary_sensor", "offsite_backup_running", {
            "name": "Backup läuft",
            "state_topic": "offsite_backup/state",
            "value_template": "{{ value_json.backup_running }}",
            "payload_on": "True",
            "payload_off": "False",
            "device_class": "running",
        }),
        ("binary_sensor", "offsite_backup_recovery_running", {
            "name": "Recovery aktiv",
            "state_topic": "offsite_backup/state",
            "value_template": "{{ value_json.recovery_running }}",
            "payload_on": "True",
            "payload_off": "False",
            "icon": "mdi:hospital-box",
        }),
        ("button", "offsite_backup_trigger", {
            "name": "Backup starten",
            "command_topic": "offsite_backup/backup/trigger",
            "payload_press": "trigger",
            "icon": "mdi:cloud-upload",
        }),
        ("switch", "offsite_backup_recovery", {
            "name": "Recovery Umgebung",
            "state_topic": "offsite_backup/state",
            "value_template": "{{ value_json.recovery_running }}",
            "payload_on": "True",
            "payload_off": "False",
            "command_topic": "offsite_backup/recovery/set",
            "icon": "mdi:hospital-box",
        }),
    ]

    def __init__(self, host, port, username, password):
        import paho.mqtt.client as mqtt
        self._client = mqtt.Client(client_id="offsite_backup_addon")
        self._client.username_pw_set(username, password)
        self._client.reconnect_delay_set(min_delay=5, max_delay=60)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.connect(host, port, keepalive=60)
        self._client.loop_start()

    def _on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            log.warning("MQTT Verbindung fehlgeschlagen (rc=%s)", rc)
            return
        log.info("MQTT verbunden")
        self._publish_discovery()
        self.publish_state()
        client.subscribe("offsite_backup/backup/trigger")
        client.subscribe("offsite_backup/recovery/set")

    def _on_message(self, client, userdata, msg):
        topic = msg.topic
        payload = msg.payload.decode().strip()
        if topic == "offsite_backup/backup/trigger":
            if payload == "trigger":
                log.info("MQTT: Backup-Trigger empfangen")
                trigger_backup()
        elif topic == "offsite_backup/recovery/set":
            if payload.upper() == "ON":
                log.info("MQTT: Recovery start")
                trigger_recovery("start")
            elif payload.upper() == "OFF":
                log.info("MQTT: Recovery stop")
                trigger_recovery("stop")

    def _publish_discovery(self):
        for entity_type, unique_id, config in self.DISCOVERY_ENTITIES:
            payload = dict(config)
            payload["unique_id"] = unique_id
            payload["device"] = self.DEVICE
            topic = f"homeassistant/{entity_type}/{unique_id}/config"
            self._client.publish(topic, json.dumps(payload, ensure_ascii=False), retain=True)
        log.info("MQTT auto-discovery veröffentlicht")

    def publish_state(self):
        status = read_status()
        state = {
            "status": status.get("status", "unbekannt"),
            "last_run": status.get("last_run"),
            "next_run": get_next_run(),
            "backup_running": is_backup_running(),
            "backup_started_at": get_backup_started_at(),
            "recovery_running": is_recovery_running(),
            "progress": get_progress(),
        }
        self._client.publish(self.STATE_TOPIC, json.dumps(state, ensure_ascii=False), retain=True)

    def start_state_loop(self):
        def _loop():
            while True:
                try:
                    self.publish_state()
                except Exception as e:
                    log.warning("MQTT state publish Fehler: %s", e)
                time.sleep(30)
        t = threading.Thread(target=_loop, daemon=True)
        t.start()


def _get_mqtt_credentials():
    opts = read_options()
    host = opts.get("mqtt_host", "").strip()
    if host:
        return (
            host,
            int(opts.get("mqtt_port", 1883)),
            opts.get("mqtt_user", ""),
            opts.get("mqtt_password", ""),
        )

    supervisor_token = os.environ.get("SUPERVISOR_TOKEN")
    if supervisor_token:
        try:
            req = urllib.request.Request(
                "http://supervisor/services/mqtt",
                headers={"Authorization": f"Bearer {supervisor_token}"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            return (
                data["host"],
                int(data.get("port", 1883)),
                data["username"],
                data["password"],
            )
        except Exception as e:
            log.warning("Supervisor MQTT-Abfrage fehlgeschlagen: %s", e)

    return None


def start_mqtt():
    global _mqtt_client
    opts = read_options()
    if not opts.get("mqtt_discovery", False):
        log.info("MQTT auto-discovery deaktiviert")
        return

    creds = _get_mqtt_credentials()
    if not creds:
        log.warning("MQTT-Zugangsdaten nicht verfügbar, MQTT wird nicht gestartet")
        return

    host, port, username, password = creds
    try:
        _mqtt_client = MQTTClient(host, port, username, password)
        _mqtt_client.start_state_loop()
        log.info("MQTT gestartet (%s:%s)", host, port)
    except Exception as e:
        log.warning("MQTT-Start fehlgeschlagen: %s", e)
        _mqtt_client = None


DASHBOARD_HTML = """\
<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Offsite Backup</title>
  <style>
    :root { --ok:#4CAF50; --err:#f44336; --run:#2196F3; --warn:#FF9800; }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: -apple-system, sans-serif; background: #f0f2f5; color: #333; }
    header { background: #1976D2; color: #fff; padding: 1rem 1.5rem; display: flex; align-items: center; gap: .75rem; }
    header h1 { font-size: 1.2rem; font-weight: 600; }
    main { max-width: 960px; margin: 1.5rem auto; padding: 0 1rem; display: grid; gap: 1rem; }
    .card { background: #fff; border-radius: 8px; padding: 1.25rem; box-shadow: 0 1px 4px rgba(0,0,0,.08); }
    .card-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: .75rem; }
    .card-header h2 { font-size: .95rem; font-weight: 600; color: #555; text-transform: uppercase; letter-spacing: .04em; }
    .btn-icon { background: none; border: 1px solid #ddd; color: #888; border-radius: 50%; width: 28px; height: 28px; padding: 0; display: flex; align-items: center; justify-content: center; cursor: pointer; font-size: 1rem; line-height: 1; transition: background .15s, color .15s; flex-shrink: 0; }
    .btn-icon:hover { background: #f0f0f0; color: #333; opacity: 1; }
    .row { display: flex; align-items: center; gap: .5rem; margin: .35rem 0; font-size: .9rem; }
    .label { color: #888; min-width: 110px; }
    .badge { padding: .2em .6em; border-radius: 4px; font-weight: 600; font-size: .82rem; }
    .badge-ok    { background: #e8f5e9; color: var(--ok); }
    .badge-failed { background: #ffebee; color: var(--err); }
    .badge-running { background: #e3f2fd; color: var(--run); }
    .badge-unbekannt { background: #f5f5f5; color: #999; }
    .actions { display: flex; gap: .5rem; flex-wrap: wrap; margin-top: .75rem; }
    button { border: none; padding: .55rem 1.2rem; border-radius: 6px; cursor: pointer; font-size: .88rem; font-weight: 500; transition: opacity .15s; }
    button:hover { opacity: .85; }
    .btn-primary { background: #1976D2; color: #fff; }
    .btn-success { background: #388E3C; color: #fff; }
    .btn-danger  { background: #c62828; color: #fff; }
    .btn-secondary { background: #eee; color: #333; }
    pre { background: #1a1a2e; color: #a0d0a0; padding: 1rem; border-radius: 6px; font-size: .78rem; line-height: 1.45; overflow: auto; max-height: 420px; white-space: pre-wrap; word-break: break-word; }
    .spinner { display: inline-block; width: 14px; height: 14px; border: 2px solid #ccc; border-top-color: var(--run); border-radius: 50%; animation: spin .8s linear infinite; vertical-align: middle; margin-right: 4px; }
    @keyframes spin { to { transform: rotate(360deg); } }
    code { background: #f5f5f5; padding: .1em .35em; border-radius: 3px; font-size: .85em; }
    #msg { position: fixed; bottom: 1.5rem; right: 1.5rem; background: #333; color: #fff; padding: .7rem 1.2rem; border-radius: 8px; display: none; font-size: .88rem; z-index: 999; }
  </style>
</head>
<body>
<header>
  <span style="font-size:1.5rem">&#9729;</span>
  <h1>Offsite Backup</h1>
</header>
<main>

  <!-- Karte 1: Status -->
  <div class="card" id="status-card">
    <div class="card-header"><h2>Status</h2></div>
    <div class="row"><span class="label">Letzter Lauf</span><span id="last-run">—</span></div>
    <div class="row"><span class="label">Ergebnis</span><span id="status-badge" class="badge">—</span></div>
    <div class="row" id="backup-running-row" style="display:none">
      <span class="label">L&auml;uft seit</span><span id="backup-running-since">—</span>
      <span style="margin-left:.5rem;color:#888;font-size:.85rem" id="backup-progress-label"></span>
    </div>
    <div class="row"><span class="label">ZFS-Storage</span><code id="nas-host">—</code></div>
    <div class="row"><span class="label">Zeitplan</span><code id="schedule">—</code></div>
    <div class="row"><span class="label">N&auml;chster Backup</span><span id="next-run">—</span></div>
    <div class="row"><span class="label">BackupPC</span><span id="recovery-status">—</span></div>
    <div class="actions">
      <button id="start-btn" class="btn-primary" onclick="triggerBackup()">&#9654; Backup jetzt starten</button>
      <button id="abort-btn" class="btn-danger" onclick="abortBackup()" style="display:none">&#9632; Backup abbrechen</button>
    </div>
  </div>


  <!-- Karte 2: Offsite (Hetzner) -->
  <div class="card" id="offsite-card">
    <div class="card-header">
      <h2>Offsite (Hetzner)</h2>
      <button class="btn-icon" onclick="loadOffsiteInfo(true)" title="Aktualisieren">&#8635;</button>
    </div>
    <div class="row"><span class="label">Belegung</span><span id="offsite-disk">&#8230;</span></div>
    <div class="row"><span class="label">Kopien</span><span id="offsite-snaps">&#8230;</span></div>
    <div id="offsite-snap-list" style="margin-top:.4rem;display:none">
      <table style="width:100%;font-size:.82rem;border-collapse:collapse" id="offsite-snap-table"></table>
    </div>
    <div class="actions" style="margin-top:.5rem">
      <button class="btn-secondary" id="offsite-toggle-btn" onclick="toggleSnapList()">&#9658; Snapshots anzeigen</button>
    </div>
  </div>

  <!-- Karte 3: BackupPC Recovery Umgebung -->
  <div class="card">
    <div class="card-header"><h2>BackupPC Recovery Umgebung</h2></div>
    <p style="font-size:.88rem;color:#666;margin-bottom:.75rem">
      Startet BackupPC via SSHFS (read-only) &mdash; Lesezugriff auf alle Sicherungen, keine neuen Backups.
    </p>
    <div class="actions">
      <button class="btn-success" onclick="triggerRecovery('start')">&#9654; BackupPC starten</button>
      <button class="btn-danger"  onclick="triggerRecovery('stop')">&#9632; BackupPC beenden</button>
      <button id="recovery-open-btn" class="btn-primary" onclick="openRecoveryUI()" style="display:none">&#10548; BackupPC UI öffnen</button>
    </div>
  </div>

  <!-- Karte 5: PBS Container Recovery (LXC 901) -->
  <div class="card" id="pbs-lxc-card">
    <div class="card-header">
      <h2>PBS Server wiederherstellen (LXC 901)</h2>
      <button class="btn-icon" onclick="loadPbsLxcDumps(true)" title="Aktualisieren">&#8635;</button>
    </div>
    <p style="font-size:.85rem;color:#666;margin-bottom:.75rem">
      PBS-Container (LXC 901) inkl. Datastore von Hetzner wiederherstellen &mdash;
      Schritte: vzdump laden &rarr; ZFS-Dataset &rarr; pct restore &rarr; PBS-Daten sync &rarr; start.
    </p>

    <!-- Schritt 1: vzdump auswählen -->
    <div id="pbs-lxc-dumps-container">
      <span style="color:#999;font-size:.88rem">Lade&#8230;</span>
    </div>

    <!-- Schritt 2: Konfiguration + Starten -->
    <div id="pbs-lxc-form" style="display:none;margin-top:.75rem;padding:.75rem;background:#f9f9f9;border-radius:6px">
      <div style="font-size:.85rem;font-weight:600;margin-bottom:.5rem" id="pbs-lxc-selected-label"></div>
      <div style="display:grid;gap:.4rem">
        <div class="row"><span class="label">PVE Node</span>
          <input id="pbs-lxc-node" type="text" style="border:1px solid #ddd;border-radius:4px;padding:.3rem .5rem;font-size:.88rem;flex:1" placeholder="nas">
        </div>
        <div class="row"><span class="label">Ziel-VMID</span>
          <input id="pbs-lxc-vmid" type="number" style="border:1px solid #ddd;border-radius:4px;padding:.3rem .5rem;font-size:.88rem;flex:1" placeholder="901">
        </div>
        <div class="row"><span class="label">OS Storage</span>
          <input id="pbs-lxc-os-storage" type="text" style="border:1px solid #ddd;border-radius:4px;padding:.3rem .5rem;font-size:.88rem;flex:1" placeholder="local-lvm">
        </div>
        <div class="row"><span class="label">Daten ZFS Dataset</span>
          <input id="pbs-lxc-zfs-dataset" type="text" style="border:1px solid #ddd;border-radius:4px;padding:.3rem .5rem;font-size:.88rem;flex:1" placeholder="ZPool/PBS-recovered">
        </div>
        <div class="row"><span class="label">Hetzner PBS Pfad</span>
          <input id="pbs-lxc-hetzner-pbs" type="text" style="border:1px solid #ddd;border-radius:4px;padding:.3rem .5rem;font-size:.88rem;flex:1" placeholder="ZPool/PBS">
        </div>
      </div>
      <div class="actions" style="margin-top:.6rem">
        <button class="btn-success" onclick="startPbsLxcRestore()">&#9654; Recovery starten</button>
        <button class="btn-secondary" onclick="closePbsLxcForm()">&#10005; Abbrechen</button>
      </div>
    </div>

    <!-- Status / Fortschritt -->
    <div id="pbs-lxc-status" style="display:none;margin-top:.75rem;padding:.75rem;background:#f0f8ff;border-radius:6px;font-size:.85rem">
      <div style="font-weight:600" id="pbs-lxc-status-title"></div>
      <div style="color:#555;margin:.3rem 0" id="pbs-lxc-status-msg"></div>
      <pre id="pbs-lxc-log" style="max-height:200px;overflow-y:auto;background:#fff;padding:.5rem;border-radius:4px;font-size:.78rem;margin-top:.4rem"></pre>
      <div class="actions" style="margin-top:.5rem">
        <button class="btn-secondary btn-sm" onclick="loadPbsLxcStatus(true)">&#8635; Status aktualisieren</button>
        <button class="btn-primary btn-sm" onclick="resetPbsLxcStatus()" id="pbs-lxc-reset-btn" style="display:none">&#10006; Zur&#252;cksetzen</button>
      </div>
    </div>
  </div>

  <!-- Karte 6: BackupPC wiederherstellen -->
  <div class="card" id="backuppc-restore-card">
    <div class="card-header">
      <h2>BackupPC wiederherstellen</h2>
      <button class="btn-icon" onclick="loadBppcStatus(true)" title="Aktualisieren">&#8635;</button>
    </div>
    <p style="font-size:.85rem;color:#666;margin-bottom:.75rem">
      BackupPC-Docker inkl. Daten von Hetzner wiederherstellen &mdash;
      Schritte: Container stoppen &rarr; Daten sync (4 Pfade) &rarr; Container starten &rarr; Smoke-Test.
    </p>

    <!-- Hetzner-Snapshot auswählen -->
    <div style="display:flex;align-items:center;gap:.5rem;margin:.35rem 0;font-size:.9rem">
      <span class="label">Quelle (Hetzner)</span>
      <select id="bppc-snapshot" style="border:1px solid #ddd;border-radius:4px;padding:.3rem .5rem;font-size:.88rem;flex:1">
        <option value="">Aktueller Stand (live)</option>
      </select>
    </div>

    <!-- Konfigurationsfelder -->
    <div id="bppc-form-fields" style="margin-top:.6rem;padding:.75rem;background:#f9f9f9;border-radius:6px;display:grid;gap:.4rem">
      <div class="row"><span class="label">Docker-Host</span>
        <input id="bppc-docker-host" type="text" style="border:1px solid #ddd;border-radius:4px;padding:.3rem .5rem;font-size:.88rem;flex:1" placeholder="10.10.11.0">
      </div>
      <div class="row"><span class="label">Container-Name</span>
        <input id="bppc-container-name" type="text" style="border:1px solid #ddd;border-radius:4px;padding:.3rem .5rem;font-size:.88rem;flex:1" placeholder="backuppc">
      </div>
      <div class="row"><span class="label">Daten-Pfad</span>
        <input id="bppc-data-path" type="text" style="border:1px solid #ddd;border-radius:4px;padding:.3rem .5rem;font-size:.88rem;flex:1" placeholder="/ZPool/BackupPC">
      </div>
      <div class="row"><span class="label">Config-Pfad</span>
        <input id="bppc-config-path" type="text" style="border:1px solid #ddd;border-radius:4px;padding:.3rem .5rem;font-size:.88rem;flex:1" placeholder="/ZPool/Docker/backuppc/config">
      </div>
      <div class="row"><span class="label">Home-Pfad</span>
        <input id="bppc-home-path" type="text" style="border:1px solid #ddd;border-radius:4px;padding:.3rem .5rem;font-size:.88rem;flex:1" placeholder="/ZPool/Docker/backuppc/home">
      </div>
      <div class="row"><span class="label">SSH-Config-Pfad</span>
        <input id="bppc-sshconfig-path" type="text" style="border:1px solid #ddd;border-radius:4px;padding:.3rem .5rem;font-size:.88rem;flex:1" placeholder="/ZPool/Docker/backuppc/ssh_config">
      </div>
    </div>

    <div class="actions" style="margin-top:.75rem">
      <button class="btn-success" onclick="startBppcRestore()">&#9654; Recovery starten</button>
    </div>

    <!-- Status / Fortschritt -->
    <div id="bppc-status" style="display:none;margin-top:.75rem;padding:.75rem;background:#f0f8ff;border-radius:6px;font-size:.85rem">
      <div style="font-weight:600" id="bppc-status-title"></div>
      <div style="color:#555;margin:.3rem 0" id="bppc-status-msg"></div>
      <pre id="bppc-log" style="max-height:200px;overflow-y:auto;background:#fff;padding:.5rem;border-radius:4px;font-size:.78rem;margin-top:.4rem"></pre>
      <div class="actions" style="margin-top:.5rem">
        <button class="btn-secondary btn-sm" onclick="loadBppcStatus(true)">&#8635; Status aktualisieren</button>
        <button class="btn-primary btn-sm" onclick="resetBppcStatus()" id="bppc-reset-btn" style="display:none">&#10006; Zur&#252;cksetzen</button>
      </div>
    </div>
  </div>

  <!-- Karte 4: Log -->
  <div class="card">
    <div class="card-header">
      <h2>Log (letzte 100 Zeilen)</h2>
      <button class="btn-icon" onclick="loadLog(true)" title="Log aktualisieren">&#8635;</button>
    </div>
    <pre id="log-content">Lade...</pre>
  </div>

</main>
<div id="msg"></div>

<script>
const base = "__INGRESS_PATH__";

// ── Debug-Banner (v1.12.1) ─────────────────────────────────────────────────
(function() {
  function _showBanner(msg, color) {
    var b = document.getElementById('_js_debug');
    if (!b) {
      b = document.createElement('div');
      b.id = '_js_debug';
      b.style.cssText = 'position:fixed;bottom:0;left:0;right:0;padding:10px 14px;z-index:9999;font-size:13px;word-break:break-all;max-height:120px;overflow:auto;';
      document.body.appendChild(b);
    }
    b.style.background = color || '#c00';
    b.style.color = color ? '#000' : '#fff';
    b.innerHTML += '<div>' + msg + '</div>';
  }
  window.onerror = function(msg, src, line, col, err) {
    _showBanner('JS-Fehler Zeile ' + line + ': ' + msg, '#ffcccc');
    return false;
  };
  window.addEventListener('unhandledrejection', function(e) {
    var r = e.reason;
    _showBanner('Promise-Fehler: ' + (r && r.message ? r.message : String(r)), '#ffe0b2');
  });
  // DOM-Selbsttest nach 2s
  setTimeout(function() {
    var ids = ['last-run','status-badge','offsite-disk','offsite-snaps','pbs-lxc-dumps-container','bppc-docker-host'];
    var missing = ids.filter(function(id) { return !document.getElementById(id); });
    if (missing.length) {
      _showBanner('Fehlende DOM-IDs: ' + missing.join(', '), '#fff3cd');
    }
    if (document.getElementById('last-run') && document.getElementById('last-run').textContent === '\u2014') {
      _showBanner('STATUS nicht geladen nach 2s — fetch-Basis: ' + base, '#ffcccc');
    }
  }, 2000);
})();
// ── Ende Debug-Banner ────────────────────────────────────────────────────────

function showMsg(text, dur=3000) {
  const el = document.getElementById('msg');
  el.textContent = text;
  el.style.display = 'block';
  clearTimeout(el._t);
  el._t = setTimeout(() => el.style.display = 'none', dur);
}

function fmtDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString('de-DE', {day:'2-digit', month:'2-digit', year:'numeric', hour:'2-digit', minute:'2-digit'});
}

function statusBadgeClass(s) {
  const map = { success:'ok', failed:'failed', running:'running', unbekannt:'unbekannt' };
  return 'badge badge-' + (map[s] || 'unbekannt');
}

async function loadStatus() {
  try {
    const [s, o] = await Promise.all([
      fetch(base + '/api/status').then(r => r.json()),
      fetch(base + '/api/options').then(r => r.json()),
    ]);
    const el = id => document.getElementById(id);
    el('last-run').textContent = fmtDate(s.last_run);
    const badge = el('status-badge');

    if (s.backup_running) {
      badge.textContent = 'läuft';
      badge.className = 'badge badge-running';
      const row = el('backup-running-row');
      if (row) row.style.display = 'flex';
      const since = el('backup-running-since');
      if (since) since.innerHTML = '<span class="spinner"></span>' + fmtDate(s.backup_started_at);
      const prog = el('backup-progress-label');
      if (prog) prog.textContent = s.progress || '';
      const sb = el('start-btn'); if (sb) sb.style.display = 'none';
      const ab = el('abort-btn'); if (ab) ab.style.display = 'inline-block';
    } else {
      badge.textContent = s.status || '—';
      badge.className = statusBadgeClass(s.status);
      const row = el('backup-running-row');
      if (row) row.style.display = 'none';
      const sb = el('start-btn'); if (sb) sb.style.display = 'inline-block';
      const ab = el('abort-btn'); if (ab) ab.style.display = 'none';
    }

    el('nas-host').textContent = o.zfs_storage_host || '?';
    el('schedule').textContent = o.backup_schedule || '?';
    el('next-run').textContent = fmtDate(s.next_run);

    const rec = el('recovery-status');
    const openBtn = el('recovery-open-btn');
    if (s.recovery_running) {
      rec.innerHTML = '<span class="badge badge-running"><span class="spinner"></span>läuft</span>';
      const port = o.backuppc_port || 8080;
      openBtn.dataset.url = `http://${location.hostname}:${port}/BackupPC_Admin`;
      openBtn.style.display = 'inline-block';
    } else {
      rec.innerHTML = '<span class="badge badge-unbekannt">inaktiv</span>';
      openBtn.style.display = 'none';
    }
  } catch(e) { console.error('loadStatus Fehler:', e); }
}

async function loadLog(showFeedback=false) {
  try {
    const d = await fetch(base + '/api/log').then(r => r.json());
    const pre = document.getElementById('log-content');
    const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 60;
    pre.textContent = d.lines.join('') || '(kein Log)';
    if (atBottom) pre.scrollTop = pre.scrollHeight;
    if (showFeedback) showMsg('Log aktualisiert', 1500);
  } catch(e) { document.getElementById('log-content').textContent = 'Fehler beim Laden: ' + e; }
}

async function triggerBackup() {
  if (!confirm('Backup jetzt manuell starten?')) return;
  const d = await fetch(base + '/api/backup', {method:'POST'}).then(r => r.json());
  showMsg(d.message, 4000);
  setTimeout(loadStatus, 1000);
}

async function abortBackup() {
  if (!confirm('Laufendes Backup abbrechen?\\n\\nDer SSH-Prozess zur NAS wird beendet.')) return;
  const d = await fetch(base + '/api/backup/abort', {method:'POST'}).then(r => r.json());
  showMsg(d.message, 5000);
  setTimeout(loadStatus, 1500);
}

async function triggerRecovery(action) {
  const label = action === 'start' ? 'starten' : 'beenden';
  if (!confirm(`BackupPC Recovery Umgebung ${label}?`)) return;
  const body = action === 'start' ? {snapshot_name: ''} : {};
  const d = await fetch(base + `/api/recovery/${action}`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  }).then(r => r.json());
  showMsg(d.message, 4000);
  setTimeout(loadStatus, 2000);
}

async function loadOffsiteInfo(showFeedback) {
  if (showFeedback === undefined) showFeedback = false;
  try {
    const d = await fetch(base + '/api/offsite_info').then(r => r.json());
    const diskEl = document.getElementById('offsite-disk');
    const snapsEl = document.getElementById('offsite-snaps');
    if (d.error) { if (diskEl) diskEl.textContent = 'Fehler: ' + d.error; return; }
    if (diskEl) {
      const used  = d.disk_used_gb  != null ? d.disk_used_gb  + ' GB' : '?';
      const total = d.disk_quota_gb != null ? d.disk_quota_gb + ' GB' : '?';
      const pct   = d.disk_pct      != null ? ' (' + d.disk_pct + ' %)' : '';
      diskEl.textContent = used + ' / ' + total + pct;
    }
    if (snapsEl) {
      const keep = d.snapshot_keep  != null ? d.snapshot_keep  : '?';
      const cnt  = d.snapshot_count != null ? d.snapshot_count : '?';
      snapsEl.textContent = cnt + ' / ' + keep + ' (konfiguriert)';
    }
    const tbl = document.getElementById('offsite-snap-table');
    if (tbl) {
      tbl.innerHTML = '';
      (d.snapshots || []).forEach(function(s) {
        var tr = document.createElement('tr');
        var created = s.created
          ? new Date(s.created).toLocaleString('de-DE', {day:'2-digit', month:'2-digit', year:'numeric', hour:'2-digit', minute:'2-digit'})
          : '&mdash;';
        tr.innerHTML = '<td style="padding:.15rem .4rem;color:#888;white-space:nowrap">' + created + '</td>'
                     + '<td style="padding:.15rem .4rem">' + (s.description || s.name || '&mdash;') + '</td>';
        tbl.appendChild(tr);
      });
    }
    if (showFeedback) showMsg('Aktualisiert', 1500);
  } catch(e) { console.error('loadOffsiteInfo:', e); }
}

function toggleSnapList() {
  var div = document.getElementById('offsite-snap-list');
  var btn = document.getElementById('offsite-toggle-btn');
  if (!div || !btn) return;
  var visible = div.style.display !== 'none';
  div.style.display = visible ? 'none' : 'block';
  btn.innerHTML = visible ? '&#9658; Snapshots anzeigen' : '&#9660; Snapshots verbergen';
}

function openRecoveryUI() {
  const url = document.getElementById('recovery-open-btn').dataset.url;
  if (url) window.open(url, '_blank');
}

// ── PBS LXC Container Recovery ─────────────────────────────────────────────
let _pbsLxcSelected = null;
let _pbsLxcPollTimer = null;

async function loadPbsLxcDumps(force) {
  const container = document.getElementById('pbs-lxc-dumps-container');
  container.innerHTML = '<span style="color:#999;font-size:.88rem">Lade&#8230;</span>';
  try {
    const url = base + '/api/recovery/pbs_lxc/dumps' + (force ? '?force=1' : '');
    const resp = await fetch(url);
    const d = await resp.json();
    if (d.error) { container.innerHTML = `<span style="color:red">${d.error}</span>`; return; }
    const dumps = d.dumps || [];
    if (!dumps.length) { container.innerHTML = '<span style="color:#999;font-size:.88rem">Keine vzdump-Dateien gefunden.</span>'; return; }
    let html = '<table style="width:100%;border-collapse:collapse;font-size:.83rem">';
    html += '<tr style="background:#f5f5f5"><th style="text-align:left;padding:.3rem .4rem">Datei</th><th style="text-align:right;padding:.3rem .4rem">Gr&#246;&#223;e</th><th style="text-align:right;padding:.3rem .4rem">Datum</th><th></th></tr>';
    for (const d of dumps) {
      html += `<tr style="border-top:1px solid #eee">
        <td style="padding:.3rem .4rem;font-family:monospace">${d.filename}</td>
        <td style="text-align:right;padding:.3rem .4rem">${d.size_gb} GB</td>
        <td style="text-align:right;padding:.3rem .4rem">${d.date_str}</td>
        <td style="padding:.3rem .4rem"><button class="btn-secondary" style="padding:.2rem .5rem;font-size:.8rem" onclick='selectPbsLxcDump(${JSON.stringify(d)})'>Ausw&#228;hlen</button></td>
      </tr>`;
    }
    html += '</table>';
    container.innerHTML = html;
    // Auch Restore-Status laden
    loadPbsLxcStatus(false);
  } catch(e) {
    container.innerHTML = `<span style="color:red">Fehler: ${e.message}</span>`;
  }
}

function selectPbsLxcDump(dump) {
  _pbsLxcSelected = dump;
  const form = document.getElementById('pbs-lxc-form');
  const label = document.getElementById('pbs-lxc-selected-label');
  label.textContent = `Ausgewählt: ${dump.filename} (${dump.size_gb} GB, ${dump.date_str})`;
  // Vorbelegen mit Config-Werten / letzten Werten
  const nodeEl = document.getElementById('pbs-lxc-node');
  const vmidEl = document.getElementById('pbs-lxc-vmid');
  const storEl = document.getElementById('pbs-lxc-os-storage');
  const zfsEl  = document.getElementById('pbs-lxc-zfs-dataset');
  const pbsEl  = document.getElementById('pbs-lxc-hetzner-pbs');
  if (!nodeEl.value) nodeEl.value = _pbsLxcLastValues.node || 'nas';
  if (!vmidEl.value) vmidEl.value = _pbsLxcLastValues.vmid || '901';
  if (!storEl.value) storEl.value = _pbsLxcLastValues.storage || 'local-lvm';
  if (!zfsEl.value)  zfsEl.value  = _pbsLxcLastValues.dataset || 'ZPool/PBS-recovered';
  if (!pbsEl.value)  pbsEl.value  = _pbsLxcLastValues.pbsPath || 'ZPool/PBS';
  form.style.display = '';
}

function closePbsLxcForm() {
  _pbsLxcSelected = null;
  document.getElementById('pbs-lxc-form').style.display = 'none';
}

let _pbsLxcLastValues = {};

async function startPbsLxcRestore() {
  if (!_pbsLxcSelected) return;
  const node    = document.getElementById('pbs-lxc-node').value.trim();
  const vmid    = document.getElementById('pbs-lxc-vmid').value.trim();
  const storage = document.getElementById('pbs-lxc-os-storage').value.trim();
  const dataset = document.getElementById('pbs-lxc-zfs-dataset').value.trim();
  const pbsPath = document.getElementById('pbs-lxc-hetzner-pbs').value.trim();
  if (!node || !vmid || !storage || !dataset) { showMsg('Bitte alle Felder ausfüllen'); return; }
  // Letzte Werte merken
  _pbsLxcLastValues = { node, vmid, storage, dataset, pbsPath };
  try {
    localStorage.setItem('pbsLxcLastValues', JSON.stringify(_pbsLxcLastValues));
  } catch(e) {}
  closePbsLxcForm();
  showMsg('Recovery wird gestartet…');
  try {
    const resp = await fetch(base + '/api/recovery/pbs_lxc/start', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        vzdump_file: _pbsLxcSelected.filename,
        pve_node: node, target_vmid: parseInt(vmid),
        os_storage: storage, data_zfs_dataset: dataset,
        offsite_pbs_path: pbsPath,
      }),
    });
    const d = await resp.json();
    if (!d.ok) { showMsg('Fehler: ' + d.message, 5000); return; }
    showMsg('Recovery gestartet!');
    loadPbsLxcStatus(true);
    _startPbsLxcPoll();
  } catch(e) {
    showMsg('Fehler: ' + e.message, 5000);
  }
}

async function loadPbsLxcStatus(force) {
  try {
    const resp = await fetch(base + '/api/recovery/pbs_lxc/status');
    const d = await resp.json();
    const statusEl = document.getElementById('pbs-lxc-status');
    const titleEl  = document.getElementById('pbs-lxc-status-title');
    const msgEl    = document.getElementById('pbs-lxc-status-msg');
    const logEl    = document.getElementById('pbs-lxc-log');
    const resetBtn = document.getElementById('pbs-lxc-reset-btn');
    if (d.status === 'idle') { statusEl.style.display = 'none'; return; }
    statusEl.style.display = '';
    const icons = {running: '⏳', done: '✅', error: '❌'};
    titleEl.textContent = (icons[d.status] || '') + ' ' + (d.step || d.status);
    if (d.error) {
      msgEl.innerHTML = `<span style="color:red">${d.error}</span>`;
    } else {
      msgEl.textContent = d.msg || '';
    }
    if (d.log_tail) {
      logEl.textContent = d.log_tail;
      logEl.scrollTop = logEl.scrollHeight;
    }
    resetBtn.style.display = (d.status !== 'running') ? '' : 'none';
    if (d.status === 'running') {
      _startPbsLxcPoll();
    } else {
      _stopPbsLxcPoll();
    }
  } catch(e) { /* ignore */ }
}

function resetPbsLxcStatus() {
  fetch(base + '/api/recovery/pbs_lxc/start', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({_reset: true}),
  }).catch(() => {});
  document.getElementById('pbs-lxc-status').style.display = 'none';
}

function _startPbsLxcPoll() {
  if (_pbsLxcPollTimer) return;
  _pbsLxcPollTimer = setInterval(() => loadPbsLxcStatus(false), 10000);
}
function _stopPbsLxcPoll() {
  if (_pbsLxcPollTimer) { clearInterval(_pbsLxcPollTimer); _pbsLxcPollTimer = null; }
}

// Beim Laden: letzte Werte aus localStorage, dumps laden
try {
  const saved = localStorage.getItem('pbsLxcLastValues');
  if (saved) _pbsLxcLastValues = JSON.parse(saved);
} catch(e) {}
loadPbsLxcDumps(false);

// ── BackupPC Docker Restore ─────────────────────────────────────────────────
let _bppcPollTimer = null;

async function loadBppcOptions() {
  try {
    const opts = await fetch(base + '/api/options').then(r => r.json());
    const f = (id, key, def) => {
      const el = document.getElementById(id);
      if (el && !el.value) el.value = opts[key] || def;
    };
    f('bppc-docker-host',    'backuppc_docker_host',    '10.10.11.0');
    f('bppc-container-name', 'backuppc_container_name', 'backuppc');
    f('bppc-data-path',      'backuppc_data_path',      '/ZPool/BackupPC');
    f('bppc-config-path',    'backuppc_config_path',    '/ZPool/Docker/backuppc/config');
    f('bppc-home-path',      'backuppc_home_path',      '/ZPool/Docker/backuppc/home');
    f('bppc-sshconfig-path', 'backuppc_sshconfig_path', '/ZPool/Docker/backuppc/ssh_config');
  } catch(e) { console.error('loadBppcOptions:', e); }
  // Snapshots in Select befüllen
  try {
    const d = await fetch(base + '/api/offsite_info').then(r => r.json());
    const sel = document.getElementById('bppc-snapshot');
    if (sel && d.snapshots && d.snapshots.length) {
      d.snapshots.forEach(function(s) {
        const opt = document.createElement('option');
        opt.value = s.name || s.description || '';
        const created = s.created
          ? new Date(s.created).toLocaleString('de-DE', {day:'2-digit', month:'2-digit', year:'numeric', hour:'2-digit', minute:'2-digit'})
          : '';
        opt.textContent = (s.description || s.name || opt.value) + (created ? '  (' + created + ')' : '');
        sel.appendChild(opt);
      });
    }
  } catch(e) { console.error('loadBppcOptions/snapshots:', e); }
}

async function startBppcRestore() {
  const docker_host    = document.getElementById('bppc-docker-host').value.trim();
  const container_name = document.getElementById('bppc-container-name').value.trim();
  const data_path      = document.getElementById('bppc-data-path').value.trim();
  const config_path    = document.getElementById('bppc-config-path').value.trim();
  const home_path      = document.getElementById('bppc-home-path').value.trim();
  const sshconfig_path = document.getElementById('bppc-sshconfig-path').value.trim();
  const offsite_snapshot = document.getElementById('bppc-snapshot').value;
  if (!container_name || !data_path) { showMsg('Bitte Container-Name und Daten-Pfad angeben'); return; }
  if (!confirm('BackupPC-Recovery jetzt starten?\n\nContainer wird gestoppt, Daten von Hetzner synchronisiert.')) return;
  showMsg('Recovery wird gestartet…');
  try {
    const resp = await fetch(base + '/api/backuppc_restore/start', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ docker_host, container_name, data_path, config_path,
                             home_path, sshconfig_path, offsite_snapshot }),
    });
    const d = await resp.json();
    if (!d.ok) { showMsg('Fehler: ' + d.message, 5000); return; }
    showMsg('Recovery gestartet!');
    loadBppcStatus(true);
    _startBppcPoll();
  } catch(e) {
    showMsg('Fehler: ' + e.message, 5000);
  }
}

async function loadBppcStatus(force) {
  try {
    const resp = await fetch(base + '/api/backuppc_restore/status');
    const d = await resp.json();
    const statusEl = document.getElementById('bppc-status');
    const titleEl  = document.getElementById('bppc-status-title');
    const msgEl    = document.getElementById('bppc-status-msg');
    const logEl    = document.getElementById('bppc-log');
    const resetBtn = document.getElementById('bppc-reset-btn');
    if (d.status === 'idle') { if (statusEl) statusEl.style.display = 'none'; return; }
    statusEl.style.display = '';
    const icons = {running: '⏳', done: '✅', error: '❌'};
    titleEl.textContent = (icons[d.status] || '') + ' ' + (d.step || d.status);
    if (d.error) {
      msgEl.innerHTML = '<span style="color:red">' + d.error + '</span>';
    } else {
      msgEl.textContent = d.msg || '';
    }
    if (d.log_tail) {
      logEl.textContent = d.log_tail;
      logEl.scrollTop = logEl.scrollHeight;
    }
    resetBtn.style.display = (d.status !== 'running') ? '' : 'none';
    if (d.status === 'running') {
      _startBppcPoll();
    } else {
      _stopBppcPoll();
    }
  } catch(e) { /* ignore */ }
}

async function resetBppcStatus() {
  try {
    await fetch(base + '/api/backuppc_restore/reset', { method: 'POST' });
  } catch(e) {}
  const statusEl = document.getElementById('bppc-status');
  if (statusEl) statusEl.style.display = 'none';
  _stopBppcPoll();
}

function _startBppcPoll() {
  if (_bppcPollTimer) return;
  _bppcPollTimer = setInterval(() => loadBppcStatus(false), 10000);
}
function _stopBppcPoll() {
  if (_bppcPollTimer) { clearInterval(_bppcPollTimer); _bppcPollTimer = null; }
}


loadBppcOptions();
loadBppcStatus(false);
loadStatus(); loadLog(); loadOffsiteInfo();
setInterval(loadStatus, 15000);
setInterval(() => loadLog(false), 30000);
setInterval(function() { loadOffsiteInfo(false); }, 300000);
</script>
</body>
</html>
"""


_API_ROUTES = (
    "/api/recovery/start", "/api/recovery/stop",
    "/api/status", "/api/options", "/api/log", "/api/backups",
    "/api/backup/abort", "/api/backup", "/api/offsite_info",
    "/api/recovery/pbs/snapshots", "/api/recovery/pbs/restore", "/api/recovery/pbs/task",
    "/api/recovery/pbs_lxc/dumps", "/api/recovery/pbs_lxc/status",
    "/api/recovery/pbs_lxc/start",
    "/api/backuppc_restore/status", "/api/backuppc_restore/log",
    "/api/backuppc_restore/start", "/api/backuppc_restore/reset",
)


def _normalize_path(raw, ingress=""):
    p = raw.split("?")[0].rstrip("/")
    prefix = ingress or INGRESS_PATH
    if prefix and p.startswith(prefix):
        return p[len(prefix):].rstrip("/") or "/"
    for route in _API_ROUTES:
        if p == route or p.endswith(route):
            return route
    return "/"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        ingress = INGRESS_PATH or self.headers.get("X-Ingress-Path", "")
        path = _normalize_path(self.path, ingress)

        if path == "/":
            html = DASHBOARD_HTML.replace("__INGRESS_PATH__", ingress)
            self._html(html)
        elif path == "/api/status":
            s = read_status()
            s["backup_running"] = is_backup_running()
            s["backup_state"] = _nas_backup_state()["class"]
            s["backup_started_at"] = get_backup_started_at()
            s["recovery_running"] = is_recovery_running()
            s["next_run"] = get_next_run()
            s["progress"] = get_progress()
            self._json(s)
        elif path == "/api/options":
            opts = read_options()
            _hidden = {"offsite_user", "offsite_host", "offsite_box_id",
                       "ssh_key_storage", "ssh_key_offsite",
                       "offsite_token", "mqtt_password"}
            safe = {k: v for k, v in opts.items() if k not in _hidden}
            self._json(safe)
        elif path == "/api/log":
            self._json({"lines": get_log_lines()})
        elif path == "/api/backups":
            data, err = list_snapshots()
            if err:
                self._json({"error": err}, 500)
            else:
                self._json(data)
        elif path == "/api/offsite_info":
            data, err = get_offsite_box_info()
            if err:
                self._json({"error": err}, 500)
            else:
                self._json(data)
        elif path == "/api/recovery/pbs/snapshots":
            data, err = get_pbs_snapshots(force="force" in self.path)
            if err:
                self._json({"error": err}, 500)
            else:
                self._json({"snapshots": data})
        elif path == "/api/recovery/pbs_lxc/dumps":
            data, err = list_vzdump_snapshots()
            if err:
                self._json({"error": err}, 500)
            else:
                self._json({"dumps": data})
        elif path == "/api/recovery/pbs_lxc/status":
            self._json(get_pbs_lxc_restore_status())
        elif path == "/api/backuppc_restore/status":
            self._json(get_bppc_restore_status())
        elif path == "/api/backuppc_restore/log":
            try:
                with open(BPPC_LOG_FILE) as f:
                    lines = f.readlines()
                self._json({"lines": lines[-100:]})
            except Exception:
                self._json({"lines": []})
        else:
            self._json({"error": "Not found"}, 404)

    def do_POST(self):
        ingress = INGRESS_PATH or self.headers.get("X-Ingress-Path", "")
        path = _normalize_path(self.path, ingress)
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        if path == "/api/backup":
            ok_flag, msg = trigger_backup()
            self._json({"ok": ok_flag, "message": msg})
        elif path == "/api/backup/abort":
            ok_flag, msg = abort_backup()
            self._json({"ok": ok_flag, "message": msg})
        elif path == "/api/recovery/start":
            ok_flag, msg = trigger_recovery("start", body.get("snapshot_name", ""))
            self._json({"ok": ok_flag, "message": msg})
        elif path == "/api/recovery/stop":
            ok_flag, msg = trigger_recovery("stop")
            self._json({"ok": ok_flag, "message": msg})
        elif path == "/api/recovery/pbs/restore":
            btype  = body.get("backup_type", "vm")
            bid    = body.get("backup_id", "")
            btime  = body.get("backup_time", 0)
            node   = body.get("pve_node", "")
            stor   = body.get("target_storage", "")
            tvmid  = body.get("target_vmid")
            if not bid or not node or not stor:
                self._json({"ok": False, "message": "backup_id, pve_node, target_storage fehlen"}, 400)
                return
            upid, err = restore_from_pbs(btype, bid, btime, node, stor, tvmid)
            if err:
                self._json({"ok": False, "message": err}, 500)
            else:
                self._json({"ok": True, "upid": upid})
        elif path == "/api/recovery/pbs/task":
            node = body.get("pve_node", "")
            upid = body.get("upid", "")
            if not node or not upid:
                self._json({"ok": False, "message": "pve_node und upid fehlen"}, 400)
                return
            status, err = get_pve_task_status(node, upid)
            if err:
                self._json({"ok": False, "message": err}, 500)
            else:
                self._json({"ok": True, "status": status})
        elif path == "/api/recovery/pbs_lxc/start":
            vzdump_file      = body.get("vzdump_file", "")
            pve_node         = body.get("pve_node", "nas")
            target_vmid      = body.get("target_vmid", 901)
            os_storage       = body.get("os_storage", "local-lvm")
            data_zfs_dataset = body.get("data_zfs_dataset", "ZPool/PBS-recovered")
            offsite_pbs_path = body.get("offsite_pbs_path", "ZPool/PBS")
            if body.get("_reset"):
                _pbs_lxc_status_write("idle")
                self._json({"ok": True, "message": "Reset"})
                return
            ok, msg = start_pbs_lxc_restore(
                vzdump_file, pve_node, target_vmid, os_storage,
                data_zfs_dataset, offsite_pbs_path)
            self._json({"ok": ok, "message": msg}, 200 if ok else 409)
        elif path == "/api/backuppc_restore/start":
            opts = read_options()
            docker_host    = body.get("docker_host",    opts.get("backuppc_docker_host", "10.10.11.0"))
            container_name = body.get("container_name", opts.get("backuppc_container_name", "backuppc"))
            data_path      = body.get("data_path",      opts.get("backuppc_data_path", "/ZPool/BackupPC"))
            config_path    = body.get("config_path",    opts.get("backuppc_config_path", "/ZPool/Docker/backuppc/config"))
            home_path      = body.get("home_path",      opts.get("backuppc_home_path", "/ZPool/Docker/backuppc/home"))
            sshconfig_path = body.get("sshconfig_path", opts.get("backuppc_sshconfig_path", "/ZPool/Docker/backuppc/ssh_config"))
            offsite_snapshot = body.get("offsite_snapshot", "")
            ok, msg = start_bppc_restore(
                docker_host, container_name, data_path, config_path,
                home_path, sshconfig_path, offsite_snapshot)
            self._json({"ok": ok, "message": msg}, 200 if ok else 409)
        elif path == "/api/backuppc_restore/reset":
            _bppc_status_write("idle")
            self._json({"ok": True, "message": "Reset"})
        else:
            self._json({"error": "Not found"}, 404)

    def _json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, html, code=200):
        body = html.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        log.info("HTTP %s", fmt % args)



class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Requests in separaten Threads — kein Blockieren bei SSH-Calls."""
    daemon_threads = True


if __name__ == "__main__":
    os.makedirs("/data/logs", exist_ok=True)
    if not os.environ.get("SUPERVISOR_TOKEN"):
        log.warning("SUPERVISOR_TOKEN nicht verfügbar — BackupPC-Steuerung deaktiviert")
    start_mqtt()
    threading.Thread(target=_nas_watch_loop, daemon=True).start()
    server = ThreadedHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"API läuft auf Port {PORT} (ingress: '{INGRESS_PATH}')", flush=True)
    server.serve_forever()
