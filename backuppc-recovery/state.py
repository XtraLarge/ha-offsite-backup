#!/usr/bin/env python3
"""Publiziert BackupPC-Recovery-Status via MQTT Auto-Discovery."""
import json
import logging
import os
import subprocess
import time

import urllib.request

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
        result = subprocess.run(
            ["pgrep", "-f", "BackupPC"],
            capture_output=True,
        )
        return result.returncode == 0
    except Exception:
        return False


DEVICE = {
    "identifiers": ["backuppc_recovery"],
    "name": "BackupPC Recovery",
    "model": "HA Add-on v1.0",
    "manufacturer": "XtraLarge",
}

DISCOVERY_ENTITIES = [
    ("binary_sensor", "backuppc_recovery_running", {
        "name": "BackupPC Recovery aktiv",
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

        # Auto-Discovery
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


def publish_state(client, url):
    if not client:
        return
    state = {
        "running": backuppc_running(),
        "url": url,
    }
    client.publish("backuppc_recovery/state",
                   json.dumps(state, ensure_ascii=False), retain=True)


if __name__ == "__main__":
    time.sleep(5)
    opts = read_options()

    # Host-IP für die URL ermitteln
    try:
        import socket
        hostname = socket.gethostname()
        url = f"http://<HA-IP>:8900/BackupPC/"
    except Exception:
        url = "http://<HA-IP>:8900/BackupPC/"

    client = start_mqtt(opts)

    while True:
        try:
            publish_state(client, url)
        except Exception as e:
            log.warning("State-Publish Fehler: %s", e)
        time.sleep(30)
