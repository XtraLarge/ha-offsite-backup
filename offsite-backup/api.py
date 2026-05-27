#!/usr/bin/env python3
"""HTTP-API und Web-Dashboard für das HA Offsite Backup Add-on."""
import json
import os
import subprocess
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
import urllib.request

PORT = 8099
INGRESS_PATH = os.environ.get("INGRESS_PATH", "")
OPTIONS_FILE = "/data/options.json"
LOG_FILE = "/data/logs/backup.log"
STATUS_FILE = "/data/logs/status.json"
BACKUP_LOCK = "/tmp/backup-running"
RECOVERY_LOCK = "/tmp/recovery-running"


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


def is_recovery_running():
    return os.path.exists(RECOVERY_LOCK)


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


def trigger_recovery(action, target=None):
    if action == "start" and is_recovery_running():
        return False, "Recovery läuft bereits"
    t = threading.Thread(target=_run_recovery, args=(action, target), daemon=True)
    t.start()
    return True, f"Recovery {action} gestartet"


def _run_recovery(action, target):
    if action == "start":
        open(RECOVERY_LOCK, "w").close()
    try:
        cmd = ["/scripts/recovery.sh", f"--{action}"]
        if target:
            cmd.append(target)
        subprocess.run(cmd, capture_output=False)
    finally:
        if action == "stop":
            try:
                os.unlink(RECOVERY_LOCK)
            except OSError:
                pass


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


DASHBOARD_HTML = """\
<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Offsite Backup</title>
  <style>
    :root {{ --ok:#4CAF50; --err:#f44336; --run:#2196F3; --warn:#FF9800; }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: -apple-system, sans-serif; background: #f0f2f5; color: #333; }}
    header {{ background: #1976D2; color: #fff; padding: 1rem 1.5rem; display: flex; align-items: center; gap: .75rem; }}
    header h1 {{ font-size: 1.2rem; font-weight: 600; }}
    main {{ max-width: 960px; margin: 1.5rem auto; padding: 0 1rem; display: grid; gap: 1rem; }}
    .card {{ background: #fff; border-radius: 8px; padding: 1.25rem; box-shadow: 0 1px 4px rgba(0,0,0,.08); }}
    .card h2 {{ font-size: .95rem; font-weight: 600; color: #555; margin-bottom: .75rem; text-transform: uppercase; letter-spacing: .04em; }}
    .row {{ display: flex; align-items: center; gap: .5rem; margin: .35rem 0; font-size: .9rem; }}
    .label {{ color: #888; min-width: 110px; }}
    .badge {{ padding: .2em .6em; border-radius: 4px; font-weight: 600; font-size: .82rem; }}
    .badge-ok    {{ background: #e8f5e9; color: var(--ok); }}
    .badge-failed {{ background: #ffebee; color: var(--err); }}
    .badge-running {{ background: #e3f2fd; color: var(--run); }}
    .badge-unbekannt {{ background: #f5f5f5; color: #999; }}
    .actions {{ display: flex; gap: .5rem; flex-wrap: wrap; margin-top: .75rem; }}
    button {{ border: none; padding: .55rem 1.2rem; border-radius: 6px; cursor: pointer; font-size: .88rem; font-weight: 500; transition: opacity .15s; }}
    button:hover {{ opacity: .85; }}
    .btn-primary {{ background: #1976D2; color: #fff; }}
    .btn-success {{ background: #388E3C; color: #fff; }}
    .btn-danger  {{ background: #c62828; color: #fff; }}
    .btn-secondary {{ background: #eee; color: #333; }}
    pre {{ background: #1a1a2e; color: #a0d0a0; padding: 1rem; border-radius: 6px; font-size: .78rem; line-height: 1.45; overflow: auto; max-height: 420px; white-space: pre-wrap; word-break: break-word; }}
    table {{ width: 100%; border-collapse: collapse; font-size: .88rem; }}
    th {{ background: #f5f5f5; padding: .5rem .75rem; text-align: left; font-weight: 600; color: #555; }}
    td {{ padding: .45rem .75rem; border-top: 1px solid #eee; }}
    .spinner {{ display: inline-block; width: 14px; height: 14px; border: 2px solid #ccc; border-top-color: var(--run); border-radius: 50%; animation: spin .8s linear infinite; vertical-align: middle; margin-right: 4px; }}
    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
    code {{ background: #f5f5f5; padding: .1em .35em; border-radius: 3px; font-size: .85em; }}
    #msg {{ position: fixed; bottom: 1.5rem; right: 1.5rem; background: #333; color: #fff; padding: .7rem 1.2rem; border-radius: 8px; display: none; font-size: .88rem; z-index: 999; }}
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
    <div class="row"><span class="label">Recovery</span><span id="recovery-status">—</span></div>
    <div class="actions">
      <button class="btn-primary" onclick="triggerBackup()">&#9654; Backup jetzt starten</button>
      <button class="btn-secondary" onclick="loadLog()">&#8635; Log aktualisieren</button>
      <button class="btn-secondary" onclick="loadSnapshots()">&#128190; Snapshots laden</button>
    </div>
  </div>

  <div class="card">
    <h2>Recovery (BackupPC)</h2>
    <p style="font-size:.88rem;color:#666;margin-bottom:.75rem">
      Startet BackupPC mit Hetzner-Daten via SSHFS.<br>
      Modus <code>local</code>: direkt auf diesem HA-RPi (kein extra Host nötig).
    </p>
    <div class="actions">
      <button class="btn-success" onclick="triggerRecovery('start')">&#9654; Recovery starten</button>
      <button class="btn-danger"  onclick="triggerRecovery('stop')">&#9632; Recovery beenden</button>
    </div>
    <div id="recovery-url" style="margin-top:.75rem;font-size:.88rem;display:none">
      BackupPC-UI: <a id="recovery-link" href="#" target="_blank"></a>
    </div>
  </div>

  <div class="card">
    <h2>Hetzner Snapshots</h2>
    <div id="snapshots-content"><em style="color:#aaa;font-size:.88rem">Klicke "Snapshots laden"</em></div>
  </div>

  <div class="card">
    <h2>Log (letzte 100 Zeilen)</h2>
    <pre id="log-content">Lade...</pre>
  </div>

</main>
<div id="msg"></div>

<script>
const base = "{INGRESS_PATH}";

function showMsg(text, dur=3000) {{
  const el = document.getElementById('msg');
  el.textContent = text;
  el.style.display = 'block';
  clearTimeout(el._t);
  el._t = setTimeout(() => el.style.display = 'none', dur);
}}

function statusBadgeClass(s) {{
  const map = {{ success:'ok', failed:'failed', running:'running', unbekannt:'unbekannt' }};
  return 'badge badge-' + (map[s] || 'unbekannt');
}}

async function loadStatus() {{
  try {{
    const [s, o] = await Promise.all([
      fetch(base + '/api/status').then(r => r.json()),
      fetch(base + '/api/options').then(r => r.json()),
    ]);
    document.getElementById('last-run').textContent = s.last_run || '—';
    const badge = document.getElementById('status-badge');
    badge.textContent = s.status || '—';
    badge.className = statusBadgeClass(s.status);
    document.getElementById('nas-host').textContent = o.nas_host || '?';
    document.getElementById('schedule').textContent = o.backup_schedule || '?';

    const rec = document.getElementById('recovery-status');
    if (s.recovery_running) {{
      rec.innerHTML = '<span class="badge badge-running"><span class="spinner"></span>läuft</span>';
      const url = `http://${{location.hostname}}:8900`;
      document.getElementById('recovery-url').style.display = 'block';
      document.getElementById('recovery-link').href = url;
      document.getElementById('recovery-link').textContent = url;
    }} else {{
      rec.innerHTML = '<span class="badge badge-unbekannt">inaktiv</span>';
      document.getElementById('recovery-url').style.display = 'none';
    }}
  }} catch(e) {{ console.error(e); }}
}}

async function loadLog() {{
  try {{
    const d = await fetch(base + '/api/log').then(r => r.json());
    document.getElementById('log-content').textContent = d.lines.join('') || '(kein Log)';
  }} catch(e) {{ document.getElementById('log-content').textContent = 'Fehler beim Laden'; }}
}}

async function loadSnapshots() {{
  const el = document.getElementById('snapshots-content');
  el.innerHTML = '<em>Lade...</em>';
  try {{
    const d = await fetch(base + '/api/backups').then(r => r.json());
    if (d.error) {{ el.innerHTML = `<span style="color:red">${{d.error}}</span>`; return; }}
    const snaps = d.snapshots || [];
    if (!snaps.length) {{ el.innerHTML = '<em style="color:#aaa">Keine Snapshots vorhanden</em>'; return; }}
    el.innerHTML = '<table><thead><tr><th>Name</th><th>Erstellt</th><th>Beschreibung</th></tr></thead><tbody>'
      + snaps.map(s => `<tr><td><code>${{s.name||''}}</code></td><td>${{(s.created||'').slice(0,19)}}</td><td>${{s.description||''}}</td></tr>`).join('')
      + '</tbody></table>';
  }} catch(e) {{ el.innerHTML = `<span style="color:red">Fehler: ${{e}}</span>`; }}
}}

async function triggerBackup() {{
  if (!confirm('Backup jetzt manuell starten?')) return;
  const d = await fetch(base + '/api/backup', {{method:'POST'}}).then(r => r.json());
  showMsg(d.message, 4000);
  setTimeout(loadStatus, 1000);
}}

async function triggerRecovery(action) {{
  const label = action === 'start' ? 'starten' : 'beenden';
  if (!confirm(`Recovery ${{label}}?`)) return;
  const d = await fetch(base + `/api/recovery/${{action}}`, {{method:'POST'}}).then(r => r.json());
  showMsg(d.message, 4000);
  setTimeout(loadStatus, 2000);
}}

// Initial laden
loadStatus(); loadLog();
setInterval(loadStatus, 15000);
setInterval(loadLog, 30000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.removeprefix(INGRESS_PATH).split("?")[0].rstrip("/") or "/"

        if path == "/":
            html = DASHBOARD_HTML.replace("{INGRESS_PATH}", INGRESS_PATH)
            self._html(html)
        elif path == "/api/status":
            s = read_status()
            s["backup_running"] = is_backup_running()
            s["recovery_running"] = is_recovery_running()
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
        path = self.path.removeprefix(INGRESS_PATH).split("?")[0].rstrip("/")
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        if path == "/api/backup":
            ok_flag, msg = trigger_backup()
            self._json({"ok": ok_flag, "message": msg})
        elif path == "/api/recovery/start":
            target = body.get("target")
            ok_flag, msg = trigger_recovery("start", target)
            self._json({"ok": ok_flag, "message": msg})
        elif path == "/api/recovery/stop":
            target = body.get("target")
            ok_flag, msg = trigger_recovery("stop", target)
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
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    os.makedirs("/data/logs", exist_ok=True)
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"API läuft auf Port {PORT} (ingress: '{INGRESS_PATH}')", flush=True)
    server.serve_forever()
