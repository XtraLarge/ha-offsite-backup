#!/usr/bin/env python3
"""HTTP-API und Web-Dashboard für das HA Offsite Backup Add-on."""
import json
import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
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
NAS_KEY = SECRETS_DIR + "/id_ed25519_storage"
SCREEN_NAME = "offsite-backup"
REMOTE_RUNDIR = "/dev/shm/offsite-backup"

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
            all_lines = f.readlines()
            return all_lines[-lines:]
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
                return f.readlines()[-lines:]
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


_screen_cache = {"ts": 0.0, "running": False}


def is_backup_running():
    """Quelle der Wahrheit ist die screen-Session auf der NAS. Ergebnis wird
    kurz gecacht, damit Status-Polls die NAS nicht überfluten. Ist die NAS nicht
    erreichbar, dient die lokale Lock-Datei als Rückfallebene."""
    now = time.time()
    if now - _screen_cache["ts"] < 8:
        return _screen_cache["running"]
    r = _nas_ssh(f"screen -ls 2>/dev/null | grep -q {SCREEN_NAME} && echo RUN || echo IDLE")
    if r is None:
        running = os.path.exists(BACKUP_LOCK)
    else:
        running = "RUN" in (r.stdout or "")
    _screen_cache.update(ts=now, running=running)
    return running


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
    if not is_backup_running():
        return "Bereit"
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
_backup_started_at = None


def trigger_backup():
    if is_backup_running():
        return False, "Backup läuft bereits"
    t = threading.Thread(target=_run_backup, daemon=True)
    t.start()
    return True, "Backup gestartet"


def abort_backup():
    global _backup_proc
    if not is_backup_running():
        return False, "Kein Backup läuft"
    # screen-Session auf der NAS beenden und verwaiste rsync/ssh-Prozesse killen,
    # damit der ZFS-Snapshot-Mount freigegeben wird.
    _nas_ssh(
        f"screen -S {SCREEN_NAME} -X quit 2>/dev/null; "
        r"pkill -f 'ctl-rsync-offline|\.zfs/snapshot/pre_rsync' 2>/dev/null; true",
        timeout=20,
    )
    # Lokalen Launcher/Tail beenden (das eigentliche Backup lief auf der NAS).
    proc = _backup_proc
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    _screen_cache["ts"] = 0.0
    try:
        os.unlink(BACKUP_LOCK)
    except OSError:
        pass
    log.info("Backup manuell abgebrochen (NAS screen beendet)")
    return True, "Backup abgebrochen"


def _run_backup():
    global _backup_proc, _backup_started_at
    open(BACKUP_LOCK, "w").close()
    _backup_started_at = datetime.now(timezone.utc).isoformat()
    try:
        _backup_proc = subprocess.Popen(["/scripts/backup.sh"])
        _backup_proc.wait()
    finally:
        _backup_proc = None
        _backup_started_at = None
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
    status = "success" if ec == "0" else "failed"
    # Vollständiges Log holen, BEVOR das tmpfs-RunDir gelöscht wird.
    log_r = _nas_ssh(f"cat '{REMOTE_RUNDIR}/run.log' 2>/dev/null", timeout=30)
    if log_r is None:
        return False  # Log nicht erreicht → RunDir NICHT löschen, erneut versuchen
    _archive_run_log(status, ec, log_r.stdout)
    try:
        with open(STATUS_FILE, "w") as f:
            json.dump({"status": status, "last_run": datetime.now().astimezone().isoformat()}, f)
    except OSError:
        pass
    _nas_ssh(f"rm -rf {REMOTE_RUNDIR}")
    log.info("Backup auf NAS abgeschlossen (rc=%s) – Log archiviert, status.json nachgezogen", ec or "?")
    if _mqtt_client:
        _mqtt_client.publish_state()
    return True


def _nas_watch_loop():
    """Erkennt das Ende eines NAS-Laufs auch ohne lebenden Launcher (Container-
    Neustart-Resilienz) und finalisiert dann den Status. Schlägt das Finalize
    fehl (NAS kurz nicht erreichbar), wird es bei den nächsten Ticks erneut
    versucht, bis das RunDir geholt+aufgeräumt ist."""
    was_running = is_backup_running()
    pending = False
    while True:
        time.sleep(20)
        try:
            running = is_backup_running()
            if (was_running and not running) or pending:
                pending = not _finalize_from_nas()
            was_running = running
        except Exception as e:
            log.warning("NAS-Watch Fehler: %s", e)


def trigger_recovery(action, snapshot_name=""):
    try:
        if action == "start":
            opts = read_options()
            _supervisor_request("POST", f"/addons/{RECOVERY_ADDON_SLUG}/options", {
                "options": {
                    "offsite_user":    opts.get("offsite_user", ""),
                    "offsite_host":    opts.get("offsite_host", ""),
                    "offsite_port":    int(opts.get("offsite_port", 23)),
                    "snapshot_name":   snapshot_name,
                    "mqtt_host":       opts.get("mqtt_host", ""),
                    "mqtt_port":       int(opts.get("mqtt_port", 1883)),
                    "mqtt_user":       opts.get("mqtt_user", ""),
                    "mqtt_password":   opts.get("mqtt_password", ""),
                    "ssh_key_offsite": opts.get("ssh_key_offsite", ""),
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

  <!-- Karte 2: BackupPC Recovery Umgebung -->
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

  <!-- Karte 3: Log -->
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

function openRecoveryUI() {
  const url = document.getElementById('recovery-open-btn').dataset.url;
  if (url) window.open(url, '_blank');
}

loadStatus(); loadLog();
setInterval(loadStatus, 15000);
setInterval(() => loadLog(false), 30000);
</script>
</body>
</html>
"""


_API_ROUTES = (
    "/api/recovery/start", "/api/recovery/stop",
    "/api/status", "/api/options", "/api/log", "/api/backups",
    "/api/backup/abort", "/api/backup",
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
            s["backup_started_at"] = _backup_started_at
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


if __name__ == "__main__":
    os.makedirs("/data/logs", exist_ok=True)
    if not os.environ.get("SUPERVISOR_TOKEN"):
        log.warning("SUPERVISOR_TOKEN nicht verfügbar — BackupPC-Steuerung deaktiviert")
    start_mqtt()
    threading.Thread(target=_nas_watch_loop, daemon=True).start()
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"API läuft auf Port {PORT} (ingress: '{INGRESS_PATH}')", flush=True)
    server.serve_forever()
