# BackupPC Recovery – Dokumentation

## Übersicht

Das BackupPC Recovery Add-on startet eine vollständige **BackupPC4-Umgebung** direkt auf Home Assistant. Es greift per SSHFS auf die Hetzner Storage Box zu und macht alle Sicherungen über die gewohnte BackupPC-Weboberfläche zugänglich.

**Wichtig:** Dieses Add-on ist **nur bei Bedarf zu starten** – typischerweise nach einem NAS-Ausfall, zur Dateiwiederherstellung oder zur Sicherungsüberprüfung. Im normalen Betrieb läuft es nicht.

**Keine neuen Sicherungen:** `BackupsDisable=2` ist immer gesetzt – es werden keine automatischen Backups gestartet.

---

## Steuerung

### Über das Offsite Backup Dashboard (empfohlen)

Das **Offsite Backup** Add-on startet und stoppt dieses Add-on automatisch:

1. Im Offsite Backup Dashboard die **Datenquelle** wählen:
   - **Live-Daten (aktuell):** Aktueller Stand der Storage Box
   - **Snapshot auswählen:** Älterer Zeitpunkt (Dropdown mit allen Hetzner-Snapshots)

2. **BackupPC starten** klicken – das Add-on konfiguriert sich automatisch mit den Hetzner-Zugangsdaten aus dem Offsite Backup Add-on

3. Warten bis der Status auf "läuft" wechselt (ca. 30–60 Sekunden)

4. **BackupPC UI öffnen** – Button erscheint automatisch wenn das Add-on läuft

5. Nach der Recovery: **BackupPC beenden** klicken

### Manuell (ohne Offsite Backup Add-on)

Das Add-on kann auch eigenständig konfiguriert und gestartet werden. Alle Felder in der HA-Konfiguration eintragen (siehe unten).

---

## Konfiguration

| Feld | Beschreibung | Beispiel |
|------|-------------|---------|
| `offsite_user` | Offsite Storage Box Benutzername | `u123456` |
| `offsite_host` | Offsite Storage Box Hostname | `u123456.your-storagebox.de` |
| `offsite_port` | SSH-Port (Standard: 23) | `23` |
| `snapshot_name` | Snapshot-Name für Datenzugriff (leer = Live) | `Snap_YYYY-MM-DD` |
| `ssh_key_offsite` | Privater SSH-Key für Offsite Storage Box | (mehrzeilig) |
| `mqtt_host` | MQTT-Broker (optional) | `192.168.1.10` |
| `mqtt_port` | MQTT-Port | `1883` |
| `mqtt_user` | MQTT-Benutzer | |
| `mqtt_password` | MQTT-Passwort | |

> Wenn das Add-on über das Offsite Backup Dashboard gestartet wird, werden `offsite_user`, `offsite_host`, `offsite_port`, `snapshot_name`, `ssh_key_offsite` und MQTT-Daten automatisch übertragen – kein manuelles Eintragen nötig.

---

## Web-UI

Nach dem Start ist die BackupPC-Oberfläche erreichbar unter:

```
http://<HA-IP>:8080/BackupPC_Admin
```

Oder über den **"BackupPC UI öffnen"**-Button im Offsite Backup Dashboard.

- **Port:** 8080 (nicht über HA-Ingress – direkter Zugriff)
- **Kein Login nötig:** `REMOTE_USER=backuppc` ist automatisch gesetzt
- **Read-only-Modus:** Keine neuen Sicherungen werden gestartet

---

## Startvorgang

Beim Start des Add-ons passiert folgendes:

1. **Benutzer anlegen:** `backuppc` (UID/GID 1000) wird erstellt
2. **BackupPC einrichten** (nur beim ersten Start): `configure.pl` läuft gegen `/data/backuppc` (lokal, nicht SSHFS – vermeidet `chown`-Probleme)
3. **SSH-Key schreiben:** `ssh_key_offsite` wird nach `/data/secrets/id_ed25519_offsite` geschrieben
4. **SSHFS mounten:** `<offsite_user>@<offsite_host>:/home/.snapshots/<snapshot>/ZPool` oder `/home/ZPool` (Live)
5. **BackupPC-Config importieren** (einmalig pro Snapshot): Config wird von `<mount>/Docker/backuppc/config/` nach `/etc/backuppc/` kopiert
6. **TopDir setzen:** `$Conf{TopDir}` wird auf `<sshfs-mount>/BackupPC` gesetzt
7. **lighttpd + BackupPC** via supervisord starten

Der Config-Import passiert **einmalig** pro Snapshot (erkannt per Flag-Datei). Bei Snapshot-Wechsel wird neu importiert.

---

## MQTT-Status

Bei konfiguriertem MQTT werden folgende Entitäten publiziert:

| Entität | Typ | Beschreibung |
|---------|-----|-------------|
| `binary_sensor.backuppc_lauft` | Binary-Sensor | Add-on gestartet/gestoppt |
| `sensor.backuppc_url` | Sensor | URL der Web-UI |
| `sensor.backuppc_datenquelle` | Sensor | `Live` oder Snapshot-Name |

---

## Troubleshooting

### SSHFS-Mount fehlgeschlagen

```
FEHLER: SSHFS-Mount fehlgeschlagen (rc=...)
```

Ursachen und Prüfungen:
- SSH-Key ungültig: Beginnt der Key mit `-----BEGIN OPENSSH PRIVATE KEY-----`?
- Offsite-Host nicht erreichbar: `ping <offsite_host>` und Port 23 erreichbar?
- Add-on hat `SYS_ADMIN`-Capability und `/dev/fuse` – läuft es als privileged?

### BackupPC startet nicht: `can't find command BackupPC`

Erste-Start-Einrichtung (`configure.pl`) ist fehlgeschlagen. Add-on-Log prüfen:
```
HA → Add-ons → BackupPC Recovery → Log
```

Lösung: Add-on stoppen, `/data/firstrun` (im Container) zurücksetzen und neu starten. Oder: Add-on deinstallieren und neu installieren (löscht `/data/`).

### Web-UI zeigt leere Seite oder 404

- URL prüfen: `http://<HA-IP>:8080/BackupPC_Admin` (nicht `/BackupPC/`)
- Kurz warten – BackupPC braucht 30–60 Sekunden zum Starten
- Add-on-Log auf Fehler prüfen

### Config-Import schlägt fehl

```
cp: cannot overwrite non-directory ...
```

Alter Config-Import-Konflikt. Flag-Datei löschen:
```bash
# Im SSH-Terminal auf HA:
rm /data/addon_configs/3e98a749_backuppc_recovery/config-imported-v2*
```
Dann Add-on neu starten – Config wird neu importiert.

### lighttpd startet nicht: `Opening errorlog failed`

```bash
mkdir -p /var/log/lighttpd
```
(Normalerweise durch `run.sh` automatisch erstellt – tritt nur bei beschädigtem `/data` auf)

---

## Technische Details

**Basis-Image:** `adferrand/backuppc:4.4.0-12` (Alpine Linux + lighttpd + supervisord)

**Prozessmanagement via supervisord:**
- `backuppc`: `/usr/local/BackupPC/bin/BackupPC`
- `lighttpd`: `/usr/sbin/lighttpd`
- `watchmails`: Überwacht msmtp-Maillog

**Auth-Bypass für Recovery:**
lighttpd `auth.conf` wird ersetzt durch:
```nginx
setenv.add-environment = ("REMOTE_USER" => "backuppc")
```
Kein Passwort nötig – das Add-on ist nur im lokalen Netz erreichbar (Port 8080 nicht über HA-Ingress).

**Datenverzeichnisse im Container:**
| Pfad | Inhalt |
|------|--------|
| `/mnt/hetzner` | SSHFS-Mount der Hetzner Storage Box |
| `/mnt/hetzner/BackupPC` | BackupPC-Datenbasis (TopDir) |
| `/etc/backuppc` | BackupPC-Konfiguration (von Hetzner importiert) |
| `/data/backuppc` | BackupPC-Laufzeitdaten (lokal, persistent) |
| `/data/secrets` | SSH-Keys |
| `/usr/local/BackupPC` | BackupPC4-Installation |
