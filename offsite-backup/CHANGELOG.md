# Changelog

## 1.2.19 - 2026-05-27

### Behoben
- BackupPC starten/beenden: Fallback auf HA Long-Lived Access Token (LLAT) wenn `SUPERVISOR_TOKEN` nicht injiziert wird
- Neues Config-Feld `ha_token` (HA → Profil → Langfristige Zugriffstoken)
- `homeassistant_api: true` ergänzt

### Geändert
- Alle Debug-Logs entfernt

## 1.2.18 - 2026-05-27

### Behoben
- `hassio_role: manager` ergänzt — nur damit injiziert der Supervisor `SUPERVISOR_TOKEN` in den Container (mit `default`-Rolle wurde der Token nicht gesetzt, BackupPC starten/beenden schlug fehl)

## 1.2.12 - 2026-05-27

### Behoben
- `HASSIO_TOKEN` als Fallback für ältere HA-Versionen ergänzt (war vorher nur `SUPERVISOR_TOKEN`)
- Startup-Log zeigt ob Supervisor-Token verfügbar ist

## 1.2.11 - 2026-05-27

### Geändert
- Zeitangaben (Letzter Lauf, Nächster Backup, Snapshots) in lesbares deutsches Format `27.05.2026, 20:00` umgewandelt — Zeitzone des Browsers wird automatisch berücksichtigt
- Snapshots chronologisch sortiert (neuester zuerst), auch im Dropdown

## 1.2.10 - 2026-05-27

### Geändert
- Hetzner Snapshots werden beim Seitenaufruf automatisch geladen
- Button umbenannt zu "Snapshots aktualisieren"

## 1.2.9 - 2026-05-27

### Behoben
- Dashboard JavaScript und CSS komplett defekt wegen Python-Format-String-Escaping (`{{`/`}}` nie aufgelöst → JS-Syntaxfehler → kein einziger API-Call lief)
- Fix: DASHBOARD_HTML nutzt jetzt normale `{`/`}` statt Python-Format-Escaping

## 1.2.8 - 2026-05-27

### Behoben
- Dashboard-Basis-Pfad wird jetzt vom Server via `X-Ingress-Path`-Header injiziert (statt `window.location.pathname`) — behebt leere Werte im HA App-WebView und im Browser

## 1.2.7 - 2026-05-27

### Hinzugefügt
- Snapshot-Auswahl direkt im Dashboard: Dropdown "Datenquelle" im BackupPC-Umgebung-Card; wird beim Laden der Hetzner-Snapshots automatisch befüllt
- "BackupPC starten" übergibt die Auswahl per Supervisor API an backuppc-recovery (inkl. Hetzner-Zugangsdaten) — keine separate Konfiguration in backuppc-recovery nötig
- Bestätigungsdialog zeigt gewählte Datenquelle an

## 1.2.6 - 2026-05-27

### Behoben
- `Cache-Control: no-store` auf HTML-Antwort — Browser/App cached die Seite nicht mehr
- `_normalize_path()` matcht API-Routen jetzt als exaktes Suffix statt `/api/`-Split (korrekt auch wenn Ingress-Pfad selbst `/api/` enthält)

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
