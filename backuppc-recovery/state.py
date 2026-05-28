#!/usr/bin/env python3
"""Publiziert BackupPC-Umgebungs-Status via MQTT Auto-Discovery + HTTP-Status."""
import json
import logging
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

OPTIONS_FILE = "/data/options.json"
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("backuppc-recovery")


def read_options():
    try:
        with open(OPTIONS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def backuppc_running():
    try:
        result = subprocess.run(["pgrep", "-f", "BackupPC"], capture_output=True)
        return result.returncode == 0
    except Exception:
        return False


DEVICE = {
    "identifiers": ["backuppc_recovery"],
    "name": "BackupPC Umgebung",
    "model": "HA Add-on v1.0",
    "manufacturer": "XtraLarge",
}

DISCOVERY_ENTITIES = [
    ("binary_sensor", "backuppc_recovery_running", {
        "name": "BackupPC Umgebung aktiv",
        "state_topic": "backuppc_recovery/state",
        "value_template": "{{ value_json.running }}",
        "payload_on": "True",
        "payload_off": "False",
        "device_class": "running",
        "icon": "mdi:hospital-box",
    }),
    ("sensor", "backuppc_recovery_url", {
        "name": "BackupPC URL",
        "state_topic": "backuppc_recovery/state",
        "value_template": "{{ value_json.url }}",
        "icon": "mdi:web",
    }),
    ("sensor", "backuppc_recovery_source", {
        "name": "BackupPC Datenquelle",
        "state_topic": "backuppc_recovery/state",
        "value_template": "{{ value_json.source }}",
        "icon": "mdi:database-clock",
    }),
]


def start_mqtt(opts):
    host = opts.get("mqtt_host", "").strip()
    if not host:
        log.warning("MQTT nicht konfiguriert, State wird nicht publiziert.")
        return None
    try:
        import paho.mqtt.client as mqtt
        client = mqtt.Client(client_id="backuppc_recovery_addon")
        user = opts.get("mqtt_user", "")
        pw = opts.get("mqtt_password", "")
        if user:
            client.username_pw_set(user, pw)
        client.reconnect_delay_set(min_delay=5, max_delay=60)
        client.connect(host, int(opts.get("mqtt_port", 1883)), keepalive=60)
        client.loop_start()

        for entity_type, uid, config in DISCOVERY_ENTITIES:
            payload = dict(config)
            payload["unique_id"] = uid
            payload["device"] = DEVICE
            topic = f"homeassistant/{entity_type}/{uid}/config"
            client.publish(topic, json.dumps(payload, ensure_ascii=False), retain=True)
        log.info("MQTT Auto-Discovery veröffentlicht (%s)", host)
        return client
    except Exception as e:
        log.warning("MQTT-Start fehlgeschlagen: %s", e)
        return None


def publish_state(client, url, source):
    if not client:
        return
    state = {
        "running": str(backuppc_running()),
        "url": url,
        "source": source,
    }
    client.publish("backuppc_recovery/state",
                   json.dumps(state, ensure_ascii=False), retain=True)


def read_datastand():
    try:
        with open("/data/datastand") as f:
            return f.read().strip()
    except Exception:
        return ""


def start_status_server(get_state_fn):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(get_state_fn(), ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *a):
            pass

    server = HTTPServer(("", 9080), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()


if __name__ == "__main__":
    time.sleep(5)
    opts = read_options()

    snapshot_name = opts.get("snapshot_name", "").strip()
    datastand = read_datastand()
    source = f"Snapshot: {snapshot_name}" if snapshot_name else "Live-Daten"
    if datastand:
        source += f" · Backups bis {datastand}"

    try:
        import socket
        ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        ip = "<HA-IP>"
    url = f"http://{ip}:8080/BackupPC_Admin"

    state_cache = {"datastand": datastand, "source": source, "url": url}
    start_status_server(lambda: state_cache)

    client = start_mqtt(opts)

    while True:
        try:
            publish_state(client, url, source)
        except Exception as e:
            log.warning("State-Publish Fehler: %s", e)
        time.sleep(30)
