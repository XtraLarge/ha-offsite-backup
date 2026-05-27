# Changelog

## 1.2.5 - 2026-05-27

### Behoben
- Dashboard: Alle API-Aufrufe schlugen fehl wenn `INGRESS_PATH` nicht gesetzt war (fetch-Basis dynamisch aus `window.location` abgeleitet, Server-Routing via `_normalize_path()` robust gemacht)
- Dashboard: "Snapshots laden" in den Hetzner-Snapshots-Abschnitt verschoben

### Geändert
- "Recovery (BackupPC)" → "BackupPC Umgebung" (Karte, Status-Zeile, Buttons, Dialoge)

## 1.2.4 - 2026-05-27

### Geändert
- Recovery-Steuerung nutzt jetzt Supervisor API (`/addons/3e98a749_backuppc_recovery/start|stop`) statt lokalem Shell-Script
- Recovery-Status wird direkt vom Supervisor abgefragt (kein Lock-File mehr)
- `hassio_api: true` ergänzt, damit der Supervisor-Endpunkt erreichbar ist

## 1.2.3 - 2026-05-27

### Behoben
- AppArmor deaktiviert (`apparmor: false`) — blockierte SSHFS-Mount für Recovery

## 1.2.2 - 2026-05-27

### Behoben
- `SYS_ADMIN` Capability ergänzt — ermöglicht SSHFS-Mount für Recovery

## 1.2.1 - 2026-05-27

### Behoben
- `next_run` Zeitstempel jetzt mit Zeitzone (ISO 8601) — behebt "unbekannt" in HA timestamp-Sensor

## 1.2.0 - 2026-05-27

### Hinzugefügt
- Externe MQTT-Verbindung konfigurierbar (`mqtt_host`, `mqtt_port`, `mqtt_user`, `mqtt_password`)
- MQTT-Credentials aus Add-on-Optionen haben Vorrang vor Supervisor-Discovery

## 1.1.0 - 2026-05-27

### Hinzugefügt
- SSH-Keys und Hetzner-Token als `password`-Felder in der Add-on-Konfiguration
- `run.sh` schreibt Secrets beim Start als Dateien nach `/data/secrets/`
- MQTT Auto-Discovery: Sensoren, Binary-Sensoren, Button und Switch für Home Assistant
- `next_run` Berechnung via `croniter` (nächste geplante Ausführung)
- Fortschrittsanzeige via Log-Parsing (`ZFS Snapshot`, `rsync BackupPC Pool (1/3)` etc.)
- GitHub Actions: Add-on-Linter und ShellCheck

### Geändert
- `repository.yaml`: Korrektes Format (`name`, `url`, `maintainer`)
- `config.yaml`: Schema bereinigt, ungültiges `map: ssl:false` entfernt

## 1.0.0 - 2026-05-27

### Erstveröffentlichung
- Offsite Backup via rsync + ZFS Snapshot → Hetzner Storage Box
- Hetzner API Snapshot nach jedem Backup
- BackupPC Recovery lokal (Docker-Socket) oder remote (SSH)
- Web-Dashboard mit Status, Log und Snapshot-Übersicht
- Cron-Scheduler konfigurierbar
- Loki-Logging optional
