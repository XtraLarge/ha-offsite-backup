# ha-offsite-backup

Ein Home-Assistant-Add-on-Repository für **automatische Offsite-Backups per rsync + ZFS-Snapshot** auf einen externen Storage-Host – mit einer integrierten **BackupPC4-Recovery-Umgebung**, die direkt in Home Assistant läuft.

> **Kurz gesagt:** Dein NAS sichert sich wöchentlich von selbst an einen entfernten Ort. Geht das NAS kaputt, startest du mit einem Klick eine vollständige BackupPC-Oberfläche in Home Assistant und holst dir einzelne Dateien oder ganze Sicherungen zurück – ohne das Original.

---

## Was es macht

- **Automatischer Offsite-Backup** nach Zeitplan (Cron) oder manuell per Web-Dashboard
- **ZFS-Snapshot** auf dem Storage-Host für einen konsistenten Stand, dann **rsync** zum Offsite-Ziel (mit Retry-Logik bei Netzwerkabbrüchen)
- **Storage-Box-Snapshot** per API nach erfolgreichem Lauf (versionierte Wiederherstellungspunkte)
- **Web-Dashboard** mit Status, Live-Log, Snapshot-Liste und Fortschrittsanzeige
- **BackupPC-Recovery auf Knopfdruck** – greift per SSHFS direkt auf das Offsite-Ziel zu, ohne Daten zurückzukopieren
- **MQTT-Auto-Discovery** für Status-Sensoren, Buttons und Switches in Home Assistant

## Enthaltene Add-ons

| Add-on | Funktion | Läuft |
|--------|----------|-------|
| **Offsite Backup** | Steuert Backup-Zeitplan, rsync, Snapshots und das Dashboard | dauerhaft |
| **BackupPC Recovery** | BackupPC4-Weboberfläche mit direktem Offsite-Zugriff (SSHFS) | nur bei Bedarf |

---

## Wie es funktioniert

```
 Home Assistant                  Storage-Host (ZFS)            Offsite-Ziel
 ┌────────────────────┐  SSH     ┌──────────────────┐  rsync   ┌──────────────────┐
 │ Offsite Backup     │─────────▶│ ZFS-Snapshot     │─────────▶│ <offsite_path>/  │
 │  • Cron-Scheduler  │  (Pipe)  │ rsync je Quelle  │          │   ZPool/BackupPC  │
 │  • Web-Dashboard   │          └──────────────────┘   API    │   Docker/...      │
 │  • MQTT-Status     │──────────────────────────────────────▶ │   Snap_YYYY-MM-DD │
 │                    │                                         └──────────────────┘
 │ BackupPC Recovery  │  SSHFS (read-only)                              ▲
 │  (bei Bedarf)      │─────────────────────────────────────────────────┘
 │  Web-UI :8080      │   greift direkt auf die Offsite-Snapshots zu
 └────────────────────┘
```

**Backup-Ablauf:**
1. Das Add-on öffnet eine SSH-Session zum Storage-Host und schiebt das Backup-Skript per Pipe hinüber (der Offsite-Key bleibt dabei im Add-on und wird nur per Agent-Forwarding genutzt – er liegt nie auf dem Storage-Host).
2. Auf dem Storage-Host: ZFS-Snapshot der konfigurierten Datasets für einen konsistenten Stand.
3. Für jede Quelle aus `backup_sources` ein rsync ans Offsite-Ziel (große Pools werden in Shards parallelisiert).
4. ZFS-Snapshot aufräumen, anschließend einen versionierten Storage-Box-Snapshot per API (`Snap_YYYY-MM-DD`).

**Recovery-Ablauf:**
1. Im Dashboard die **Datenquelle** wählen – Live-Stand oder ein älterer Offsite-Snapshot.
2. **BackupPC starten** – die Recovery bekommt Zugangsdaten und `backup_sources` automatisch durchgereicht und mountet das Offsite-Ziel **read-only** per SSHFS.
3. Die BackupPC-Weboberfläche ist nach ~30–60 s unter `http://<HA-IP>:8080/BackupPC_Admin` erreichbar.
4. Dateien wie gewohnt wiederherstellen – es werden **keine neuen Sicherungen** geschrieben (`BackupsDisable=2`).

### Das `backup_sources`-Konzept

Was gesichert und wie es bei einer Recovery eingebunden wird, steuert eine einzige Liste – `backup_sources`. Beide Add-ons teilen dieselbe Struktur, sodass die Recovery sich aus genau den Pfaden bedient, die das Backup geschrieben hat (1:1-Mapping über `dest` relativ zu `offsite_path`):

| Feld | Bedeutung |
|------|-----------|
| `dest` | Zielpfad am Offsite-Ziel, relativ zu `offsite_path` (z. B. `ZPool/BackupPC`) |
| `dataset` | ZFS-Dataset, von dem ein Snapshot gezogen wird (leer = kein Snapshot) |
| `path` | Quellpfad auf dem Storage-Host, wenn kein Dataset (z. B. ein Verzeichnis) |
| `snapshot` | `true` = vor rsync ZFS-Snapshot ziehen |
| `parallel` | `true` = großen Pool in Shards parallel übertragen |
| `recovery` | `topdir` (BackupPC-Pool) · `import` (in Container kopieren) · `none` (nur Backup) |
| `container_mount` | Zielpfad im Recovery-Container für `recovery: import` |
| `recovery_clean` | `true` = Ziel vor dem Import leeren |

> Neue Quelle sichern? Einen Eintrag zur Liste hinzufügen – Backup **und** Recovery ziehen automatisch nach. Keine geteilte Datei, keine Code-Änderung.

---

## Installation

### 1. Repository hinzufügen

In Home Assistant: **Einstellungen → Add-ons → Add-on Store → ⋮ (oben rechts) → Repositories**

```
https://github.com/XtraLarge/ha-offsite-backup
```

### 2. Add-ons installieren

Im Store erscheinen zwei Add-ons:
- **Offsite Backup** – zuerst installieren und konfigurieren
- **BackupPC Recovery** – installieren, aber **nicht** manuell starten (das Dashboard übernimmt das)

### 3. SSH-Schlüssel vorbereiten (einmalig)

Du brauchst zwei Ed25519-Schlüsselpaare:

```bash
ssh-keygen -t ed25519 -f storage_key  -C "ha-offsite-storage"   # Storage-Host
ssh-keygen -t ed25519 -f offsite_key  -C "ha-offsite-target"    # Offsite-Ziel
```

**Public Key des Storage-Keys** auf dem Storage-Host in `/root/.ssh/authorized_keys` eintragen – mit `command=`-Einschränkung, damit über diesen Schlüssel **nur** das Backup-Skript läuft, keine interaktive Shell:

```
command="bash -s",no-pty,no-port-forwarding,no-X11-forwarding ssh-ed25519 AAAA... ha-offsite-storage
```

**Public Key des Offsite-Keys** beim Offsite-Anbieter (z. B. Storage-Box-Verwaltung) hinterlegen.

### 4. Add-on konfigurieren

Im **Offsite Backup** Add-on unter **Konfiguration**:

| Feld | Beschreibung |
|------|--------------|
| `zfs_storage_host` | Hostname/IP des Storage-Hosts mit dem ZFS-Pool |
| `zfs_storage_user` | SSH-Benutzer dort (Standard `root`) |
| `offsite_user` / `offsite_host` / `offsite_port` | Zugang zum Offsite-Ziel (Storage-Box-Port ist oft 23) |
| `offsite_box_id` | Numerische ID des Offsite-Ziels (für API-Snapshots) |
| `offsite_token` | API-Token des Offsite-Anbieters |
| `offsite_path` | Wurzelpfad am Offsite-Ziel (Standard `/home`) |
| `backup_schedule` | Cron-Ausdruck – **Container läuft in UTC** (z. B. `0 18 * * 3` = Mi 20:00 CEST) |
| `ssh_key_storage` / `ssh_key_offsite` | Die beiden privaten Schlüssel (als `password`-Feld) |
| `backup_sources` | Liste der Quellen (siehe oben) – kommt mit sinnvollen Defaults |
| `mqtt_*`, `loki_url` | Optional: HA-Sensoren bzw. Remote-Logging |

> SSH-Keys können mehrzeilig (mit Enter) oder einzeilig mit `\n`-Trennzeichen eingegeben werden. Sie werden als `password`-Felder gespeichert und tauchen nicht im Log oder in der Dashboard-API auf.

### 5. Starten

Das **Offsite Backup** Add-on starten. Den ersten Lauf kannst du im Dashboard mit **„Backup jetzt starten"** auslösen und im Live-Log mitverfolgen. Die Recovery-Umgebung steuerst du komplett über das Dashboard.

---

## Generisches Beispiel

Angenommen, dein Setup sieht so aus:

- **Storage-Host:** `nas.example.local`, ZFS-Pool `ZPool`, BackupPC-Daten unter `ZPool/BackupPC`
- **Offsite-Ziel:** eine Storage Box `u123456.your-storagebox.de`, Port 23, ID `123456`, Wurzel `/home`

**Schritt 1 – Schlüssel auf dem NAS eintragen** (`nas.example.local`, als root):

```
command="bash -s",no-pty,no-port-forwarding,no-X11-forwarding ssh-ed25519 AAAAC3Nz...storage ha-offsite-storage
```

**Schritt 2 – Add-on-Konfiguration** (gekürzt):

```yaml
zfs_storage_host: "nas.example.local"
zfs_storage_user: "root"
offsite_user: "u123456"
offsite_host: "u123456.your-storagebox.de"
offsite_port: 23
offsite_box_id: 123456
offsite_path: "/home"
backup_schedule: "0 18 * * 3"        # Mittwoch 20:00 CEST (18:00 UTC)
backup_sources:
  - dest: "ZPool/BackupPC"           # → /home/ZPool/BackupPC am Ziel
    dataset: "ZPool/BackupPC"
    snapshot: true
    parallel: true
    recovery: "topdir"
    container_mount: "/data/backuppc"
  - dest: "ZPool/Docker/backuppc/config"
    path: "/ZPool/Docker/backuppc/config"
    recovery: "import"
    container_mount: "/etc/backuppc"
    recovery_clean: true
```

**Ergebnis:** Jeden Mittwoch 20:00 zieht das Add-on einen Snapshot von `ZPool/BackupPC`, überträgt ihn (parallel in Shards) nach `/home/ZPool/BackupPC` auf der Storage Box, kopiert die BackupPC-Config dazu und legt am Ende einen `Snap_2026-…`-Snapshot an. Fällt das NAS aus, startest du im Dashboard die Recovery, wählst Live oder einen Snapshot, und arbeitest direkt in der BackupPC-Oberfläche.

---

## Sicherheitshinweise

- Alle Schlüssel und Tokens sind `password`-Felder – nicht im Log, nicht in der Dashboard-API (`/api/options` blendet sensible Felder aus).
- Der Offsite-Key verlässt das Add-on **nur per Agent-Forwarding** und wird nie auf den Storage-Host kopiert.
- Die Storage-Host-Verbindung ist per `command="bash -s"` auf das Backup-Skript beschränkt – keine interaktive Shell über diesen Schlüssel.
- Die Recovery mountet das Offsite-Ziel **read-only**, um laufende Übertragungen nicht zu stören.
- `AppArmor` ist deaktiviert und `SYS_ADMIN` gesetzt – ausschließlich für SSHFS (FUSE) in der Recovery nötig.

---

## Danksagung

Dieses Projekt steht auf den Schultern von zwei großartigen Open-Source-Arbeiten – und ich möchte mich bei den Menschen dahinter ganz herzlich bedanken:

- **[BackupPC](https://backuppc.github.io/backuppc/)** von **Craig Barratt** – die geniale, über viele Jahre gereifte Backup-Engine, die das ganze Konzept der deduplizierten Pool-Sicherungen erst möglich macht. Ohne dieses Fundament gäbe es hier nichts wiederherzustellen.
- **[adferrand/docker-backuppc](https://github.com/adferrand/docker-backuppc)** von **Adrien Ferrand** – das wunderbar gepflegte BackupPC-Docker-Image (`adferrand/backuppc`), das die Recovery-Umgebung direkt nutzt. Es hat mir unzählige Stunden Bastelarbeit erspart und war die Inspiration, das Ganze überhaupt in Home Assistant zu bringen.

Vielen Dank für eure Arbeit – sie wird hier täglich produktiv eingesetzt und hat mir mein Backup-Setup massiv erleichtert. Dieses Add-on-Paar ist mein Versuch, auf eurer Arbeit aufzubauen und sie mit einem komfortablen Offsite-Workflow zu ergänzen. Entwickelt habe ich es gemeinsam mit **[Claude](https://www.anthropic.com/claude)** (Anthropic) als Pair-Programming-Partner.

---

## Weiterführende Dokumentation

- [Offsite Backup – DOCS.md](offsite-backup/DOCS.md) – Konfiguration, Dashboard, Backup-Ablauf, Troubleshooting
- [BackupPC Recovery – DOCS.md](backuppc-recovery/DOCS.md) – Recovery-Umgebung, Startvorgang, technische Details

## Lizenz

MIT – siehe Add-on-Labels. BackupPC und das adferrand-Image stehen unter ihren jeweiligen eigenen Lizenzen.
