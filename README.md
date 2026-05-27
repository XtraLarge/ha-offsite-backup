# ha-offsite-backup

Home Assistant Custom Add-on Repository für **Offsite Backup via Hetzner Storage Box** mit integrierter **BackupPC4 Recovery-Umgebung**.

## Enthaltene Add-ons

| Add-on | Funktion |
|--------|---------|
| **Offsite Backup** | Automatischer Offsite-Backup (rsync, ZFS-Snapshot → Offsite Storage) mit Web-Dashboard |
| **BackupPC Recovery** | BackupPC4-Oberfläche mit direktem Offsite-Zugriff via SSHFS – nur bei Bedarf starten |

## Architektur

```
Home Assistant (Add-on)         ZFS-Storage (nas.example.local)  Offsite Storage Box
┌──────────────────────┐  SSH   ┌──────────────────┐  rsync  ┌──────────────────┐
│  Offsite Backup      │───────▶│  backup_nas.sh   │────────▶│  /home/ZPool/    │
│  • Cron-Scheduler    │  Pipe  │  • ZFS Snapshot  │         │  BackupPC/       │
│  • Web-Dashboard     │  Agent-│  • rsync →       │   API   │  Docker/         │
│  • MQTT-Status       │  Fwd   │    Offsite       │────────▶│  Snap_YYYY-MM-DD │
│                      │        └──────────────────┘          └──────────────────┘
│  BackupPC Recovery   │  SSHFS ┌──────────────────┐
│  (bei Bedarf)        │───────▶│  Offsite Storage │
│  Port 8080           │        │  Box (Snapshots) │
└──────────────────────┘        └──────────────────┘
```

**Backup-Ablauf:**
1. HA-Add-on startet SSH-Session zur NAS, übergibt das Backup-Script per Pipe
2. Auf der NAS: ZFS-Snapshot von `ZPool/BackupPC`, rsync zum Hetzner-Account
3. Zusätzlich rsync der Docker-Config/Daten von BackupPC
4. Hetzner Storage Box Snapshot via API erstellen

**Recovery-Ablauf:**
1. Offsite Backup Dashboard → BackupPC Umgebung starten (Snapshot oder Live-Daten wählen)
2. BackupPC Recovery Add-on startet, mountet Hetzner Storage Box via SSHFS
3. BackupPC Web-UI erreichbar unter `http://<HA-IP>:8080/BackupPC_Admin`
4. Datei-Recovery wie gewohnt, keine neuen Sicherungen (BackupsDisable=2)

---

## Installation

### 1. Repository hinzufügen

In Home Assistant: **Einstellungen → Add-ons → Add-on Store → ⋮ → Repositories**

```
https://github.com/XtraLarge/ha-offsite-backup
```

### 2. Add-ons installieren

Im Add-on Store erscheinen:
- **Offsite Backup** – zuerst installieren und konfigurieren
- **BackupPC Recovery** – wird automatisch vom Offsite Backup Dashboard gesteuert

### 3. SSH-Setup (einmalig, vor dem ersten Start)

#### HA-Schlüssel auf dem ZFS-Storage eintragen

Den Public Key des `ssh_key_storage` in `/root/.ssh/authorized_keys` des ZFS-Storage-Hosts eintragen:

```
command="bash -s",no-pty,no-port-forwarding,no-X11-forwarding ssh-ed25519 AAAA... hassio-offsite-backup
```

> Der Eintrag beschränkt die Verbindung auf `bash -s` – keine interaktive Shell möglich.

### 4. Add-on konfigurieren

Im **Offsite Backup** Add-on unter Konfiguration alle Felder ausfüllen:

| Feld | Beschreibung |
|------|-------------|
| `zfs_storage_host` | Hostname/IP des ZFS-Storage-Hosts mit ZPool/BackupPC |
| `zfs_storage_user` | SSH-Benutzer auf dem ZFS-Storage-Host (Standard: `root`) |
| `offsite_user` | Offsite Storage Box Benutzername (`u123456`) |
| `offsite_host` | Offsite Storage Box Hostname |
| `offsite_port` | SSH-Port (Standard: 23) |
| `offsite_box_id` | Storage Box ID (für API-Snapshots) |
| `backup_schedule` | Cron-Ausdruck, z. B. `0 20 * * 3` (Mittwoch 20 Uhr) |
| `ssh_key_storage` | Privater SSH-Key für ZFS-Storage-Verbindung |
| `ssh_key_offsite` | Privater SSH-Key für Offsite Storage Box |
| `offsite_token` | Offsite Storage API Token (für Storage Box Snapshots) |
| `mqtt_host/port/user/password` | Optional: MQTT für HA-Sensoren |

SSH-Keys können mehrzeilig (in der HA-UI mit Enter) oder einzeilig mit `\n`-Trennzeichen eingegeben werden.

### 5. Add-on starten

Das **Offsite Backup** Add-on starten. Das **BackupPC Recovery** Add-on wird automatisch über das Dashboard des Offsite Backup Add-ons gestartet und gestoppt.

---

## Sicherheitshinweise

- Alle SSH-Keys und Tokens werden als `password`-Felder gespeichert (nicht im Log sichtbar)
- Der Offsite-SSH-Key verlässt das Add-on **nur per Agent Forwarding** – er wird nicht auf den ZFS-Storage kopiert
- Die ZFS-Storage-Verbindung ist auf `bash -s` beschränkt (`command=`-Einschränkung in authorized_keys)
- `/api/options` im Dashboard gibt **keine** sensitiven Felder zurück (SSH-Keys, Token, MQTT-Passwort)
- AppArmor deaktiviert und `SYS_ADMIN`-Capability gesetzt – nur für SSHFS (Recovery) notwendig

---

Detaillierte Dokumentation:
- [Offsite Backup DOCS.md](offsite-backup/DOCS.md)
- [BackupPC Recovery DOCS.md](backuppc-recovery/DOCS.md)
