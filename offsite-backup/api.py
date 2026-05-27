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
STATUS_FILE = "/data/logs/status.json"
BACKUP_LOCK = "/tmp/backup-running"
RECOVERY_ADDON_SLUG = "3e98a749_backuppc_recovery"

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("offsite-backup")

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


def read_status():
    try:
        with open(STATUS_FILE) as f:
            return json.load(f)
    except Exception:
        return {"status": "unbekannt", "last_run": None}


def is_backup_running():
    return os.path.exists(BACKUP_LOCK)


def _supervisor_request(method, path, body=None):
    token = os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HASSIO_TOKEN")
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


_PROGRESS_PATTERNS = [
    ("erfolgreich abgeschlossen", "Fertig"),
    ("rsync BackupPC Docker Konfiguration", "rsync Docker Config (3/3)"),
    ("rsync BackupPC Docker Data", "rsync Docker Data (2/3)"),
    ("rsync BackupPC Pool", "rsync BackupPC Pool (1/3)"),
    ("Erstelle Hetzner Snapshot", "Hetzner Snapshot"),
    ("Snapshot erstellen", "ZFS Snapshot"),
]


def get_progress():
    if not is_backup_running():
        return "Bereit"
    try:
        with open(LOG_FILE) as f:
            lines = f.readlines()
        for line in reversed(lines):
            for pattern, label in _PROGRESS_PATTERNS:
                if pattern in line:
                    return label
    except Exception:
        pass
    return "Bereit"


def trigger_backup():
    if is_backup_running():
        return False, "Backup läuft bereits"
    t = threading.Thread(target=_run_backup, daemon=True)
    t.start()
    return True, "Backup gestartet"


def _run_backup():
    open(BACKUP_LOCK, "w").close()
    try:
        subprocess.run(["/scripts/backup.sh"], capture_output=False)
    finally:
        try:
            os.unlink(BACKUP_LOCK)
        except OSError:
            pass
    if _mqtt_client:
        _mqtt_client.publish_state()


def trigger_recovery(action, snapshot_name=""):
    try:
        if action == "start":
            opts = read_options()
            _supervisor_request("POST", f"/addons/{RECOVERY_ADDON_SLUG}/options", {
                "options": {
                    "hetzner_user":    opts.get("hetzner_user", ""),
                    "hetzner_host":    opts.get("hetzner_host", ""),
                    "hetzner_port":    int(opts.get("hetzner_port", 23)),
                    "snapshot_name":   snapshot_name,
                    "mqtt_host":       opts.get("mqtt_host", ""),
                    "mqtt_port":       int(opts.get("mqtt_port", 1883)),
                    "mqtt_user":       opts.get("mqtt_user", ""),
                    "mqtt_password":   opts.get("mqtt_password", ""),
                    "ssh_key_hetzner": opts.get("ssh_key_hetzner", ""),
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
        with open("/data/secrets/hetzner_token") as f:
            token = f.read().strip()
    except Exception:
        return None, "hetzner_token nicht gefunden (/data/secrets/hetzner_token)"

    opts = read_options()
    box_id = opts.get("hetzner_box_id", "")
    if not box_id:
        return None, "hetzner_box_id nicht konfiguriert"

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
    .card h2 { font-size: .95rem; font-weight: 600; color: #555; margin-bottom: .75rem; text-transform: uppercase; letter-spacing: .04em; }
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
    table { width: 100%; border-collapse: collapse; font-size: .88rem; }
    th { background: #f5f5f5; padding: .5rem .75rem; text-align: left; font-weight: 600; color: #555; }
    td { padding: .45rem .75rem; border-top: 1px solid #eee; }
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

  <div class="card" id="status-card">
    <h2>Status</h2>
    <div class="row"><span class="label">Letzter Lauf</span><span id="last-run">—</span></div>
    <div class="row"><span class="label">Ergebnis</span><span id="status-badge" class="badge">—</span></div>
    <div class="row"><span class="label">NAS</span><code id="nas-host">—</code></div>
    <div class="row"><span class="label">Zeitplan</span><code id="schedule">—</code></div>
    <div class="row"><span class="label">Nächster Backup</span><span id="next-run">—</span></div>
    <div class="row"><span class="label">BackupPC</span><span id="recovery-status">—</span></div>
    <div class="actions">
      <button class="btn-primary" onclick="triggerBackup()">&#9654; Backup jetzt starten</button>
      <button class="btn-secondary" onclick="loadLog()">&#8635; Log aktualisieren</button>
    </div>
  </div>

  <div class="card">
    <h2>BackupPC Umgebung</h2>
    <p style="font-size:.88rem;color:#666;margin-bottom:.75rem">
      Startet die BackupPC-Oberfläche mit Hetzner-Daten via SSHFS.<br>
      Lesezugriff auf alle Backups &mdash; keine neuen Sicherungen werden erstellt.
    </p>
    <div class="row" style="margin-bottom:.5rem">
      <span class="label">Datenquelle</span>
      <select id="snapshot-select" style="flex:1;padding:.4rem .6rem;border:1px solid #ddd;border-radius:6px;font-size:.88rem;background:#fff">
        <option value="">Live-Daten (aktuell)</option>
      </select>
    </div>
    <div class="actions">
      <button class="btn-success" onclick="triggerRecovery('start')">&#9654; BackupPC starten</button>
      <button class="btn-danger"  onclick="triggerRecovery('stop')">&#9632; BackupPC beenden</button>
    </div>
    <div id="recovery-url" style="margin-top:.75rem;font-size:.88rem;display:none">
      BackupPC-UI: <a id="recovery-link" href="#" target="_blank"></a>
    </div>
  </div>

  <div class="card">
    <h2>Hetzner Snapshots</h2>
    <div style="margin-bottom:.75rem">
      <button class="btn-secondary" onclick="loadSnapshots()">&#128190; Snapshots aktualisieren</button>
    </div>
    <div id="snapshots-content"><em style="color:#aaa;font-size:.88rem">Noch nicht geladen</em></div>
  </div>

  <div class="card">
    <h2>Log (letzte 100 Zeilen)</h2>
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
    document.getElementById('last-run').textContent = fmtDate(s.last_run);
    const badge = document.getElementById('status-badge');
    badge.textContent = s.status || '—';
    badge.className = statusBadgeClass(s.status);
    document.getElementById('nas-host').textContent = o.nas_host || '?';
    document.getElementById('schedule').textContent = o.backup_schedule || '?';
    document.getElementById('next-run').textContent = fmtDate(s.next_run);

    const rec = document.getElementById('recovery-status');
    if (s.recovery_running) {
      rec.innerHTML = '<span class="badge badge-running"><span class="spinner"></span>läuft</span>';
      const url = `http://${location.hostname}:8900`;
      document.getElementById('recovery-url').style.display = 'block';
      document.getElementById('recovery-link').href = url;
      document.getElementById('recovery-link').textContent = url;
    } else {
      rec.innerHTML = '<span class="badge badge-unbekannt">inaktiv</span>';
      document.getElementById('recovery-url').style.display = 'none';
    }
  } catch(e) { console.error(e); }
}

async function loadLog() {
  try {
    const d = await fetch(base + '/api/log').then(r => r.json());
    document.getElementById('log-content').textContent = d.lines.join('') || '(kein Log)';
  } catch(e) { document.getElementById('log-content').textContent = 'Fehler beim Laden'; }
}

async function loadSnapshots() {
  const el = document.getElementById('snapshots-content');
  el.innerHTML = '<em>Lade...</em>';
  try {
    const d = await fetch(base + '/api/backups').then(r => r.json());
    if (d.error) { el.innerHTML = `<span style="color:red">${d.error}</span>`; return; }
    const snaps = (d.snapshots || []).sort((a, b) => new Date(b.created) - new Date(a.created));
    if (!snaps.length) { el.innerHTML = '<em style="color:#aaa">Keine Snapshots vorhanden</em>'; return; }
    el.innerHTML = '<table><thead><tr><th>Name</th><th>Erstellt</th><th>Beschreibung</th></tr></thead><tbody>'
      + snaps.map(s => `<tr><td><code>${s.name||''}</code></td><td>${fmtDate(s.created)}</td><td>${s.description||''}</td></tr>`).join('')
      + '</tbody></table>';
    // Dropdown in BackupPC-Card befüllen
    const sel = document.getElementById('snapshot-select');
    if (sel) {
      const prev = sel.value;
      sel.innerHTML = '<option value="">Live-Daten (aktuell)</option>'
        + snaps.map(s => `<option value="${s.name||''}">${s.name||''} &mdash; ${fmtDate(s.created)}</option>`).join('');
      if (prev) sel.value = prev;
    }
  } catch(e) { el.innerHTML = `<span style="color:red">Fehler: ${e}</span>`; }
}

async function triggerBackup() {
  if (!confirm('Backup jetzt manuell starten?')) return;
  const d = await fetch(base + '/api/backup', {method:'POST'}).then(r => r.json());
  showMsg(d.message, 4000);
  setTimeout(loadStatus, 1000);
}

async function triggerRecovery(action) {
  const sel = document.getElementById('snapshot-select');
  const snapshot = sel ? sel.value : '';
  const label = action === 'start' ? 'starten' : 'beenden';
  const src = (action === 'start') ? (snapshot ? `Snapshot: ${snapshot}` : 'Live-Daten') : '';
  const msg = src ? `BackupPC Umgebung ${label}?\n\nDatenquelle: ${src}` : `BackupPC Umgebung ${label}?`;
  if (!confirm(msg)) return;
  const body = action === 'start' ? {snapshot_name: snapshot} : {};
  const d = await fetch(base + `/api/recovery/${action}`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  }).then(r => r.json());
  showMsg(d.message, 4000);
  setTimeout(loadStatus, 2000);
}

// Initial laden
loadStatus(); loadLog(); loadSnapshots();
setInterval(loadStatus, 15000);
setInterval(loadLog, 30000);
</script>
</body>
</html>
"""


_API_ROUTES = (
    "/api/recovery/start", "/api/recovery/stop",
    "/api/status", "/api/options", "/api/log", "/api/backups", "/api/backup",
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
            s["recovery_running"] = is_recovery_running()
            s["next_run"] = get_next_run()
            s["progress"] = get_progress()
            self._json(s)
        elif path == "/api/options":
            opts = read_options()
            safe = {k: v for k, v in opts.items() if k not in ("hetzner_user", "hetzner_host", "hetzner_box_id")}
            self._json(safe)
        elif path == "/api/log":
            self._json({"lines": read_log()})
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
    _sv_token = os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HASSIO_TOKEN")
    log.info("Supervisor-Token: %s", "verfügbar" if _sv_token else "NICHT VERFÜGBAR")
    _token_keys = [k for k in os.environ if any(x in k.upper() for x in ("TOKEN", "HASSIO", "SUPERVISOR", "SECRET"))]
    log.info("Token-Env-Vars vorhanden: %s", _token_keys or "keine")
    start_mqtt()
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"API läuft auf Port {PORT} (ingress: '{INGRESS_PATH}')", flush=True)
    server.serve_forever()
