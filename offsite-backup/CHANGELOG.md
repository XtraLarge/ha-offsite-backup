# Changelog

## 1.2.42 - 2026-05-29

### Hinzugefügt
- `backup_nas.sh`: Paralleler Pool-Transfer. Der BackupPC-Pool wird in Shards (Verzeichnisse auf Tiefe 2: `cpool/<hex>`, `pc/<host>`) aufgeteilt und mit konfigurierbar `RSYNC_PARALLEL_JOBS` (Default 6) gleichzeitigen rsync-Streams übertragen. Jeder Stream nutzt eine eigene SSH-Verbindung (`SSH_CMD_NOCTL`, kein gemeinsamer ControlMaster) für eigenes Congestion-Window + parallele Verschlüsselung. Vorab ein Struktur-Pass (Tiefe ≤2) für Top-Level-Dateien, Verzeichnisgerüst und `--delete` verwaister Einträge.
- `backup_nas.sh`: `kill_stale_backup_procs` beendet vor dem Snapshot-Cleanup verwaiste rsync/ssh-Prozesse früherer Läufe (z. B. nach abgebrochener SSH-Pipe), die den `pre_rsync`-Snapshot blockieren würden.

### Geändert
- `backup_nas.sh`: rsync nutzt jetzt `--whole-file` (`-W`). cpool-Dateien sind unveränderliche, inhaltsadressierte Chunks → der Delta-Algorithmus bringt nichts, kostet aber CPU/IO; `-W` überträgt geänderte Dateien direkt komplett.

### Hinweis
- Sharding ist verlustfrei: BackupPC v4 nutzt keine FS-Hardlinks (Pool inhaltsadressiert, pc/-Bäume via Referenzzählung), verifiziert auf der NAS (alle Stichproben `nlink=1`). Die parallele Offsite-Kopie ist strukturell identisch zur Einzel-rsync-Kopie und über die Recovery-Umgebung lesbar.

## 1.2.41 - 2026-05-29

### Behoben
- Dashboard war komplett leer: `abortBackup()` enthielt im `confirm()`-Text echte Zeilenumbrüche (`\n\n` im Python-Triple-Quote-String wurde zu echten Newlines), was den JS-String über mehrere Zeilen brach → Syntaxfehler → gesamter `<script>`-Block wurde nicht ausgeführt, kein `loadStatus()`/`loadLog()`. Newlines jetzt als `\\n` escaped, sodass im ausgelieferten JS echte `\n`-Escape-Sequenzen stehen.

## 1.2.40 - 2026-05-29

### Behoben
- `loadStatus()`: null-Checks für alle neuen Element-IDs (`backup-running-row`, `start-btn`, `abort-btn`) — verhindert TypeError wenn Browser eine gecachte ältere HTML-Version hat
- Fehler in `loadStatus()` werden jetzt mit Kontext ins Console-Log geschrieben

## 1.2.39 - 2026-05-29

### Hinzugefügt
- Dashboard: "Läuft seit"-Zeile mit Spinner und Fortschritt sichtbar wenn Backup aktiv
- Dashboard: "Backup abbrechen"-Button (rot) erscheint während eines laufenden Backups, ersetzt den Start-Button
- `POST /api/backup/abort` Endpoint: beendet den laufenden Backup-Prozess (SSH zur NAS)

## 1.2.38 - 2026-05-28

### Behoben
- `backup_nas.sh`: `zfs destroy` bei "dataset is busy" bricht nicht mehr den Backup-Lauf ab
- Neue Funktion `zfs_destroy_retry`: 3 Versuche mit 30s Pause, dann `zfs destroy -d` (deferred) als Fallback
- Snapshot-Diagnose im Log: vorhandene pre_rsync-Snapshots (inkl. defer_destroy-Status) werden zu Beginn aufgelistet

## 1.2.37 - 2026-05-28

### Geändert
- SSH-Key-Karte aus Dashboard entfernt — Keys werden nur noch in der HA Add-on-Konfiguration gesetzt
- `POST /api/options` Endpunkt entfernt (nicht mehr benötigt)
- Recovery-Slug-Erkennung läuft jetzt im Hintergrund-Thread → HTTP-Server startet sofort, kein 10-Sekunden-Block beim Add-on-Start

## 1.2.36 - 2026-05-28

### Geändert
- Snapshot-Auswahl aus Dashboard entfernt — Hetzner API-Snapshots sind per SFTP nicht zugänglich, Recovery läuft immer im Live-Modus

## 1.2.35 - 2026-05-28

### Geändert
- `RECOVERY_ADDON_SLUG` wird jetzt dynamisch via Supervisor API ermittelt — funktioniert sowohl mit lokalen Add-ons als auch mit GitHub-Repository-Installationen

## 1.2.34 - 2026-05-28

### Geändert
- `RECOVERY_ADDON_SLUG` und `RECOVERY_STATUS_URL` auf `local_backuppc_recovery` / `local-backuppc-recovery` aktualisiert (Add-ons jetzt als lokale Add-ons installiert statt aus GitHub-Repository)

## 1.2.33 - 2026-05-28

### Geändert
- Dashboard: SSH-Key-Eingabe als mehrzeilige Textareas (Karte 3 "SSH Keys")
- Keys werden mit `\n`-Kodierung gespeichert (HA-Kompatibilität) — `printf '%b\n'` in run.sh konvertiert korrekt zurück
- Neuer POST `/api/options` Endpoint: aktualisiert `ssh_key_storage`/`ssh_key_offsite` via Supervisor API

## 1.2.32 - 2026-05-28

### Geändert
- Dashboard neu strukturiert: 3 Karten statt 4 (Status, BackupPC Recovery Umgebung, Log)
- "BackupPC Umgebung" + "Hetzner Snapshots" zu "BackupPC Recovery Umgebung" zusammengeführt
- Snapshot-Auswahl: Radio-Buttons statt Dropdown + separater Snapshot-Tabelle
- "Live-Daten (aktuell)" als erste Radio-Option
- Snapshots aktualisieren: kleines ↻-Icon oben rechts im Karten-Header
- Log aktualisieren: kleines ↻-Icon oben rechts im Log-Karten-Header (weg aus Status)
- BackupPC-UI-Link öffnet direkt `/BackupPC_Admin` (korrekter Port 8080)

## 1.2.31 - 2026-05-27

### Geändert
- Optionsnamen rollen-basiert umbenannt (weg von Gerätetyp/Hersteller):
  - `nas_host` → `zfs_storage_host`, `nas_user` → `zfs_storage_user`
  - `hetzner_user/host/port/box_id/token` → `offsite_user/host/port/box_id/token`
  - `ssh_key_nas` → `ssh_key_storage`, `ssh_key_hetzner` → `ssh_key_offsite`
- Tote Optionen `recovery_target` und `ssh_key_recovery` entfernt
- Interne Variablen (`NAS_*`, `TARGET_*`, `HETZNER_*`) entsprechend angepasst
- Secret-Dateien: `id_ed25519_nas` → `id_ed25519_storage`, `id_ed25519_hetzner` → `id_ed25519_offsite`, `hetzner_token` → `offsite_token`

## 1.2.30 - 2026-05-27

### Geändert
- Log-Button "Log aktualisieren" zeigt jetzt Toast-Bestätigung "Log aktualisiert" nach erfolgreichem Refresh
- Log-Bereich scrollt automatisch nach unten wenn man am Ende war (Auto-Scroll während Backup)

## 1.2.29 - 2026-05-27

### Behoben
- `_write_secret` nutzt jetzt `printf '%b\n'` statt `printf '%b'` — Command-Substitution `$()` schneidet abschließenden Zeilenumbruch ab, wodurch libcrypto den SSH-Key ablehnte (`error in libcrypto`)
- `crontab /etc/cron.d/offsite-backup` entfernt — Zeile installierte Cron-Eintrag doppelt als User-Crontab ohne User-Feld, was dazu führte dass `root` als Kommando interpretiert wurde (`/bin/sh: 1: root: not found`)

## 1.2.28 - 2026-05-27

### Geändert
- `backuppc_port` Default von 8900 → **8080** (BackupPC Recovery v2.0 läuft jetzt auf Port 8080)
- Hinweis: Wer bereits `backuppc_port: 8900` konfiguriert hat, muss das in den Add-on-Optionen auf 8080 ändern

## 1.2.27 - 2026-05-27

### Behoben
- `run.sh` Shebang auf `#!/usr/bin/with-contenv bash` geändert — s6-overlay v3 lädt Docker-Umgebungsvariablen (SUPERVISOR_TOKEN etc.) nur wenn `with-contenv` verwendet wird; ohne es waren keine Supervisor-Variablen im Prozess verfügbar

## 1.2.23 - 2026-05-27

### Behoben
- `hassio_api: true` ergänzt (war in v1.2.20 entfernt worden) — ohne dieses Flag injiziert der Supervisor keinen SUPERVISOR_TOKEN, auch wenn `hassio_role: manager` gesetzt ist

## 1.2.22 - 2026-05-27

### Geändert
- BackupPC-Steuerung nutzt jetzt SUPERVISOR_TOKEN direkt (`hassio_role: manager`) mit `http://supervisor/` — LLAT hatte keine Berechtigung für Supervisor-API
- `ha_token`-Option entfernt (nicht mehr benötigt)
- `backuppc_port` (Standard: 8900) konfigurierbar — steuert die URL des "BackupPC UI öffnen"-Buttons
- Dashboard: "BackupPC UI öffnen"-Button erscheint wenn BackupPC läuft (öffnet neues Tab)
- `/api/options` gibt keine sensiblen Felder mehr zurück (SSH-Keys, Tokens, MQTT-Passwort)

## 1.2.21 - 2026-05-27

### Behoben
- Port 8123 in HA-API-URL ergänzt (`http://homeassistant:8123/api/hassio/`) — Port 80 lieferte 404

## 1.2.20 - 2026-05-27

### Geändert
- BackupPC-Steuerung nutzt jetzt direkt einen HA Long-Lived Access Token (`ha_token`) — kein SUPERVISOR_TOKEN mehr
- `homeassistant_api: true` statt `hassio_api`; Supervisor API via `http://homeassistant/api/hassio/`

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
