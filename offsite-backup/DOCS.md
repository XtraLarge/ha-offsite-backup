# Offsite Backup – Dokumentation

## Übersicht

Das Add-on läuft auf Home Assistant und steuert den wöchentlichen Offsite-Backup des NAS auf die Hetzner Storage Box. Es verbindet sich per SSH zur NAS und führt dort `backup_nas.sh` aus, das einen ZFS-Snapshot erstellt und per rsync nach Hetzner überträgt.

**Was dieses Add-on macht:**
- Automatischer Backup nach Zeitplan (Cron) oder manuell via Dashboard
- ZFS-Snapshot auf der NAS → rsync zum Hetzner-Account mit Retry-Logik
- Hetzner Storage Box Snapshot per API nach erfolgreichem Backup
- Web-Dashboard mit Status, Log und Snapshot-Übersicht
- BackupPC Recovery Add-on starten/stoppen direkt aus dem Dashboard
- MQTT-Sensoren für HA-Integration (Status, Zeitstempel, Fortschritt)

---

## 1. Voraussetzungen

### ZFS-Storage-Setup

Der ZFS-Storage-Host (auf den das Add-on per SSH verbindet) muss folgendes haben:

- ZFS-Pool `ZPool` mit Dataset `ZPool/BackupPC` (BackupPC-Daten)
- Optional: `/ZPool/Docker/backuppc/` und `/ZPool/Docker/_DockerCreate/` (Docker-Config)
- SSH-Server auf Port 22 (Standard)
- `rsync`, `openssh-client`, `jq`, `zfsutils-linux` installiert (wird vom Skript geprüft/nachinstalliert)

### Hetzner Storage Box

- SSH-Zugang mit Ed25519-Key aktiv
- Port 23 (Standard für Hetzner Storage Boxes)
- Zieldirektorie: `/home/ZPool/BackupPC/` und `/home/ZPool/Docker/`

### SSH-Key auf dem ZFS-Storage-Host eintragen

Den **Public Key** von `ssh_key_storage` in `/root/.ssh/authorized_keys` des ZFS-Storage-Hosts eintragen:

```
command="bash -s",no-pty,no-port-forwarding,no-X11-forwarding ssh-ed25519 AAAA... hassio-offsite-backup
```

Den Public Key erzeugt man aus dem privaten Key:
```bash
ssh-keygen -y -f id_ed25519_storage
```

---

## 2. Konfiguration

Alle Felder werden in der HA-Oberfläche unter **Add-on → Konfiguration** eingetragen.

### Pflichtfelder

| Feld | Beschreibung | Beispiel |
|------|-------------|---------|
| `zfs_storage_host` | Hostname oder IP des ZFS-Storage-Hosts | `nas.example.local` |
| `zfs_storage_user` | SSH-Benutzer auf dem ZFS-Storage-Host | `root` |
| `offsite_user` | Offsite Storage Box Benutzername | `u123456` |
| `offsite_host` | Offsite Storage Box Hostname | `u123456.your-storagebox.de` |
| `offsite_port` | SSH-Port der Storage Box | `23` |
| `offsite_box_id` | Numerische Storage Box ID | `123456` |
| `backup_schedule` | Cron-Ausdruck (Container-Zeit = UTC) | `0 18 * * 3` |
| `ssh_key_storage` | Privater SSH-Key für ZFS-Storage-Verbindung | (mehrzeilig) |
| `ssh_key_offsite` | Privater SSH-Key für Offsite Storage Box | (mehrzeilig) |
| `offsite_token` | Offsite Storage API Token | `hGsX7...` |

> **Achtung Zeitzone:** Der Container läuft in UTC. `0 18 * * 3` entspricht Mittwoch 20:00 CEST (UTC+2). Cron-Zeit entsprechend anpassen.

### Optionale Felder

| Feld | Beschreibung | Standard |
|------|-------------|---------|
| `loki_url` | Loki Push-URL für Remote-Logging | leer (deaktiviert) |
| `backuppc_port` | Port des BackupPC Recovery Add-ons | `8080` |
| `mqtt_host` | MQTT-Broker IP/Hostname | leer |
| `mqtt_port` | MQTT-Broker Port | `1883` |
| `mqtt_user` | MQTT-Benutzer | leer |
| `mqtt_password` | MQTT-Passwort | leer |

### SSH-Keys eintragen

SSH-Keys (`ssh_key_storage`, `ssh_key_offsite`) werden als `password`-Felder eingetragen (in der HA-UI mit `*` maskiert). Zwei Formate werden akzeptiert:

**Mehrzeilig** (direkt einfügen mit Enter):
```
-----BEGIN OPENSSH PRIVATE KEY-----
b3BlbnNzaC1rZXktdjEA...
-----END OPENSSH PRIVATE KEY-----
```

**Einzeilig** (mit `\n` als Zeilentrennzeichen):
```
-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEA...\n-----END OPENSSH PRIVATE KEY-----\n
```

---

## 3. Web-Dashboard

Das Dashboard ist über den HA-Sidebar-Eintrag "Offsite Backup" erreichbar (Ingress auf Port 8099).

### Status-Karte
- **Letzter Lauf:** Zeitstempel des letzten Backup-Starts
- **Ergebnis:** `success` (grün), `failed` (rot), `unbekannt`
- **NAS:** Aktuell konfigurierter NAS-Host
- **Zeitplan:** Cron-Ausdruck
- **Nächster Backup:** Berechnete nächste Ausführungszeit
- **BackupPC:** Ob die Recovery-Umgebung läuft

### Aktionen
- **Backup jetzt starten:** Manueller Backup-Start (mit Bestätigungsdialog)
- **Log aktualisieren:** Log-Bereich sofort neu laden (Toast-Bestätigung + Auto-Scroll)

### BackupPC Umgebung-Karte
- **Datenquelle:** Dropdown für Live-Daten oder Hetzner-Snapshot
- **BackupPC starten:** Startet das Recovery Add-on mit den gewählten Daten
- **BackupPC beenden:** Stoppt das Recovery Add-on
- **BackupPC UI öffnen:** Öffnet `http://<HA-IP>:8080` in neuem Tab (nur wenn aktiv)

### Hetzner Snapshots-Karte
- Listet alle Snapshots der Storage Box (Name, Datum, Beschreibung)
- Snapshots können direkt im Dropdown der BackupPC-Karte ausgewählt werden

### Log-Karte
- Zeigt die letzten 100 Zeilen des Backup-Logs
- Aktualisiert automatisch alle 30 Sekunden
- Scrollt automatisch nach unten wenn man am Ende war

---

## 4. Backup-Ablauf im Detail

### Was `backup.sh` tut (läuft im Add-on-Container):

1. SSH-Agent starten und Hetzner-Key laden
2. `backup_nas.sh` via SSH-Pipe an die NAS senden (mit Agent Forwarding)
3. Nach Abschluss: Loki-Log senden, Status in `/data/logs/status.json` schreiben

### Was `backup_nas.sh` tut (läuft auf der NAS via SSH):

1. Abhängigkeiten prüfen/installieren (rsync, jq, zfsutils-linux)
2. Offsite-API-Token validieren
3. Alte `pre_rsync_*`-Snapshots löschen
4. Für jede Quelle mit `snapshot: true` einen ZFS-Snapshot erstellen: `<dataset>@pre_rsync_YYYY-MM-DD_HH-MM-SS`
5. Für **jede** Quelle aus `backup_sources` ein rsync an `<offsite_path>/<dest>/` (mit bis zu 5 Retries; `parallel: true` überträgt große Pools in Shards)
6. ZFS-Snapshots wieder löschen
7. Storage-Box-Snapshot via API erstellen (`Snap_YYYY-MM-DD`)

> Welche Quellen gesichert werden, steuert die Option `backup_sources` – siehe das `backup_sources`-Konzept in der [README](../README.md). Frühere Versionen hatten die drei rsync-Ziele fest verdrahtet; seit 1.3.0 ist die Liste konfigurierbar.

### Retry-Logik

rsync versucht bei Netzwerkfehlern automatisch neu (rc 10, 11, 12, 30, 35, 255):
- Standard: 5 Retries mit 120 Sekunden Pause
- IO-Timeout: 600 Sekunden ohne Datentransfer → Fehler

---

## 5. MQTT-Integration

Bei konfiguriertem MQTT werden folgende Entitäten als Auto-Discovery publiziert:

| Entität | Typ | Beschreibung |
|---------|-----|-------------|
| `sensor.backup_status` | Sensor | `success` / `failed` / `unbekannt` |
| `sensor.letzter_backup` | Timestamp-Sensor | Letzter Backup-Zeitstempel |
| `sensor.nachster_backup` | Timestamp-Sensor | Nächster geplanter Backup |
| `sensor.backup_fortschritt` | Sensor | Aktueller Schritt während Backup |
| `binary_sensor.backup_lauft` | Binary-Sensor | Läuft gerade ein Backup? |
| `binary_sensor.recovery_aktiv` | Binary-Sensor | Ist BackupPC Recovery aktiv? |
| `button.backup_starten` | Button | Backup manuell triggern |
| `switch.recovery_umgebung` | Switch | Recovery starten/stoppen |

---

## 6. Troubleshooting

### Backup schlägt fehl: `error in libcrypto`
SSH-Key ist beschädigt. Schlüssel neu generieren und in der HA-Konfiguration eintragen.

### Backup schlägt fehl: `Permission denied (publickey)`
Public Key des `ssh_key_storage` nicht in der `authorized_keys` des ZFS-Storage-Hosts. Eintrag prüfen:
```bash
grep 'hassio' /root/.ssh/authorized_keys
```

### Backup schlägt fehl: `dataset does not exist`
`zfs_storage_host` zeigt auf den falschen Host oder `ZPool/BackupPC` existiert nicht.
Prüfen: `zfs list ZPool/BackupPC` auf dem konfigurierten ZFS-Storage-Host.

### Backup schlägt fehl: `dataset is busy`
Ein alter rsync-Prozess hält den ZFS-Snapshot belegt.
```bash
fuser /ZPool/BackupPC/.zfs/snapshot/
# Prozess identifizieren und ggf. beenden:
kill -9 <PID>
zfs destroy ZPool/BackupPC@pre_rsync_...
```

### Cron-Backup läuft nicht zur erwarteten Zeit
Der Container läuft in UTC. Beispiel: `0 18 * * 3` = Mittwoch 18:00 UTC = 20:00 CEST.

### Dashboard leer / API antwortet nicht
Add-on neu starten. Log im HA Add-on-Log prüfen (nicht das Backup-Log im Dashboard).

### SUPERVISOR_TOKEN nicht verfügbar
Add-on nicht mit `hassio_role: manager` konfiguriert. Recovery-Steuerung funktioniert nicht.
Sicherstellen, dass die `config.yaml` des Add-ons `hassio_role: manager` und `hassio_api: true` enthält.
