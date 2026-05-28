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
RECOVERY_ADDON_SLUG = "local_backuppc_recovery"

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


RECOVERY_STATUS_URL = "http://local-backuppc-recovery.local.hass.io:9080/"


def get_recovery_datastand():
    try:
        with urllib.request.urlopen(RECOVERY_STATUS_URL, timeout=3) as r:
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
    .radio-list { margin-top: .5rem; border: 1px solid #eee; border-radius: 6px; overflow: hidden; }
    .radio-item { display: flex; align-items: center; gap: .6rem; padding: .5rem .75rem; border-top: 1px solid #eee; font-size: .88rem; cursor: pointer; transition: background .1s; }
    .radio-item:first-child { border-top: none; }
    .radio-item:hover { background: #f7f9fc; }
    .radio-item input[type=radio] { margin: 0; flex-shrink: 0; cursor: pointer; }
    .radio-item .snap-name { font-weight: 500; }
    .radio-item .snap-date { color: #888; font-size: .82rem; margin-left: auto; }
    .radio-item-live .snap-name { color: #388E3C; }
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
    <div class="row"><span class="label">ZFS-Storage</span><code id="nas-host">—</code></div>
    <div class="row"><span class="label">Zeitplan</span><code id="schedule">—</code></div>
    <div class="row"><span class="label">Nächster Backup</span><span id="next-run">—</span></div>
    <div class="row"><span class="label">BackupPC</span><span id="recovery-status">—</span></div>
    <div class="actions">
      <button class="btn-primary" onclick="triggerBackup()">&#9654; Backup jetzt starten</button>
    </div>
  </div>

  <!-- Karte 2: BackupPC Recovery Umgebung + Snapshots -->
  <div class="card">
    <div class="card-header">
      <h2>BackupPC Recovery Umgebung</h2>
      <button class="btn-icon" onclick="loadSnapshots()" title="Snapshots aktualisieren">&#8635;</button>
    </div>
    <p style="font-size:.88rem;color:#666;margin-bottom:.75rem">
      Startet BackupPC via SSHFS &mdash; Lesezugriff auf alle Sicherungen, keine neuen Backups.
    </p>
    <div id="snapshots-content">
      <div class="radio-list">
        <label class="radio-item radio-item-live">
          <input type="radio" name="snapshot" value="" checked>
          <span class="snap-name">Live-Daten (aktuell)</span>
        </label>
      </div>
    </div>
    <div class="actions" style="margin-top:.85rem">
      <button class="btn-success" onclick="triggerRecovery('start')">&#9654; BackupPC starten</button>
      <button class="btn-danger"  onclick="triggerRecovery('stop')">&#9632; BackupPC beenden</button>
      <button id="recovery-open-btn" class="btn-primary" onclick="openRecoveryUI()" style="display:none">&#10548; BackupPC UI öffnen</button>
    </div>
  </div>

  <!-- Karte 3: SSH Keys -->
  <div class="card" id="settings-card">
    <div class="card-header"><h2>SSH Keys</h2></div>
    <p style="font-size:.88rem;color:#666;margin-bottom:.75rem">
      Keys werden nie angezeigt &mdash; nur ausfüllen zum Ändern. Mehrzeilig einfügen, wird intern mit <code>\n</code> kodiert gespeichert.
    </p>
    <div style="display:grid;gap:.75rem">
      <div>
        <label style="font-size:.85rem;color:#555;display:block;margin-bottom:.3rem">ssh_key_storage (NAS &rarr; Hetzner)</label>
        <textarea id="key-storage" rows="8" placeholder="-----BEGIN OPENSSH PRIVATE KEY-----&#10;...&#10;-----END OPENSSH PRIVATE KEY-----" style="width:100%;font-family:monospace;font-size:.78rem;padding:.5rem;border:1px solid #ddd;border-radius:4px;resize:vertical"></textarea>
      </div>
      <div>
        <label style="font-size:.85rem;color:#555;display:block;margin-bottom:.3rem">ssh_key_offsite (Recovery &rarr; Hetzner)</label>
        <textarea id="key-offsite" rows="8" placeholder="-----BEGIN OPENSSH PRIVATE KEY-----&#10;...&#10;-----END OPENSSH PRIVATE KEY-----" style="width:100%;font-family:monospace;font-size:.78rem;padding:.5rem;border:1px solid #ddd;border-radius:4px;resize:vertical"></textarea>
      </div>
    </div>
    <div class="actions">
      <button class="btn-primary" onclick="saveKeys()">&#128190; Keys speichern</button>
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

function getSelectedSnapshot() {
  const el = document.querySelector('input[name="snapshot"]:checked');
  return el ? el.value : '';
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
    document.getElementById('nas-host').textContent = o.zfs_storage_host || '?';
    document.getElementById('schedule').textContent = o.backup_schedule || '?';
    document.getElementById('next-run').textContent = fmtDate(s.next_run);

    const rec = document.getElementById('recovery-status');
    const openBtn = document.getElementById('recovery-open-btn');
    if (s.recovery_running) {
      rec.innerHTML = '<span class="badge badge-running"><span class="spinner"></span>läuft</span>';
      const port = o.backuppc_port || 8080;
      openBtn.dataset.url = `http://${location.hostname}:${port}/BackupPC_Admin`;
      openBtn.style.display = 'inline-block';
    } else {
      rec.innerHTML = '<span class="badge badge-unbekannt">inaktiv</span>';
      openBtn.style.display = 'none';
    }
  } catch(e) { console.error(e); }
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

async function loadSnapshots() {
  const el = document.getElementById('snapshots-content');
  const prevSelected = getSelectedSnapshot();
  el.innerHTML = '<em style="color:#aaa;font-size:.88rem;padding:.5rem 0;display:block">Lade...</em>';
  try {
    const d = await fetch(base + '/api/backups').then(r => r.json());
    if (d.error) {
      el.innerHTML = `<div class="radio-list"><label class="radio-item radio-item-live"><input type="radio" name="snapshot" value="" checked><span class="snap-name">Live-Daten (aktuell)</span></label></div><p style="color:red;font-size:.85rem;margin-top:.5rem">${d.error}</p>`;
      return;
    }
    const snaps = (d.snapshots || []).sort((a, b) => new Date(b.created) - new Date(a.created));
    const liveChecked = !prevSelected || !snaps.find(s => s.name === prevSelected);
    let html = '<div class="radio-list">'
      + `<label class="radio-item radio-item-live"><input type="radio" name="snapshot" value=""${liveChecked ? ' checked' : ''}><span class="snap-name">Live-Daten (aktuell)</span></label>`
      + snaps.map(s => {
          const checked = s.name === prevSelected ? ' checked' : '';
          return `<label class="radio-item"><input type="radio" name="snapshot" value="${s.name||''}"${checked}><span class="snap-name">${s.name||''}</span><span class="snap-date">${fmtDate(s.created)}</span></label>`;
        }).join('')
      + '</div>';
    if (!snaps.length) html += '<p style="color:#aaa;font-size:.85rem;margin-top:.4rem">Keine Snapshots vorhanden</p>';
    el.innerHTML = html;
  } catch(e) {
    el.innerHTML = `<div class="radio-list"><label class="radio-item radio-item-live"><input type="radio" name="snapshot" value="" checked><span class="snap-name">Live-Daten (aktuell)</span></label></div><p style="color:red;font-size:.85rem;margin-top:.5rem">Fehler: ${e}</p>`;
  }
}

async function triggerBackup() {
  if (!confirm('Backup jetzt manuell starten?')) return;
  const d = await fetch(base + '/api/backup', {method:'POST'}).then(r => r.json());
  showMsg(d.message, 4000);
  setTimeout(loadStatus, 1000);
}

async function triggerRecovery(action) {
  const snapshot = getSelectedSnapshot();
  const label = action === 'start' ? 'starten' : 'beenden';
  const src = (action === 'start') ? (snapshot ? `Snapshot: ${snapshot}` : 'Live-Daten') : '';
  const msg = src ? `BackupPC Recovery Umgebung ${label}?\n\nDatenquelle: ${src}` : `BackupPC Recovery Umgebung ${label}?`;
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

function openRecoveryUI() {
  const url = document.getElementById('recovery-open-btn').dataset.url;
  if (url) window.open(url, '_blank');
}

async function saveKeys() {
  const storage = document.getElementById('key-storage').value.trim();
  const offsite = document.getElementById('key-offsite').value.trim();
  if (!storage && !offsite) { showMsg('Keine Änderungen (Felder leer)', 2000); return; }
  const payload = {};
  if (storage) payload.ssh_key_storage = storage.replace(/\n/g, '\\n');
  if (offsite) payload.ssh_key_offsite = offsite.replace(/\n/g, '\\n');
  try {
    const d = await fetch(base + '/api/options', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    }).then(r => r.json());
    showMsg(d.message || (d.ok ? 'Gespeichert' : 'Fehler'), 4000);
    if (d.ok) { document.getElementById('key-storage').value = ''; document.getElementById('key-offsite').value = ''; }
  } catch(e) { showMsg('Fehler: ' + e, 5000); }
}

loadStatus(); loadLog(); loadSnapshots();
setInterval(loadStatus, 15000);
setInterval(() => loadLog(false), 30000);
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
            _hidden = {"offsite_user", "offsite_host", "offsite_box_id",
                       "ssh_key_storage", "ssh_key_offsite",
                       "offsite_token", "mqtt_password"}
            safe = {k: v for k, v in opts.items() if k not in _hidden}
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
        elif path == "/api/options":
            _allowed = {"ssh_key_storage", "ssh_key_offsite"}
            updates = {k: v for k, v in body.items() if k in _allowed}
            if not updates:
                self._json({"ok": False, "message": "Keine erlaubten Felder übergeben"}, 400)
                return
            try:
                current = read_options()
                current.update(updates)
                _supervisor_request("POST", "/addons/self/options", {"options": current})
                self._json({"ok": True, "message": f"Gespeichert: {', '.join(updates.keys())}"})
            except Exception as e:
                self._json({"ok": False, "message": str(e)}, 500)
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
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"API läuft auf Port {PORT} (ingress: '{INGRESS_PATH}')", flush=True)
    server.serve_forever()
