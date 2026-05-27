# ha-offsite-backup

Home Assistant Custom Add-on Repository für **Offsite Backup via Hetzner Storage Box**.

Ersetzt einen dedizierten rsyncos-Host (Raspberry Pi mit WiFi) durch ein stabiles
HA Add-on, das direkt auf dem Home Assistant RPi (Ethernet) läuft.

## Funktionen

- **Automatischer Offsite-Backup** (rsync, ZFS-Snapshot → Hetzner Storage Box)
- **Konfigurierbarer Zeitplan** (Cron-Ausdruck)
- **Hetzner Snapshot** nach jedem erfolgreichen Backup
- **Web-Dashboard** mit Backup-Status, Log und Snapshot-Liste
- **BackupPC-Recovery** direkt auf HA-RPi via Docker-Socket (kein extra Host nötig)
- **Loki-Logging** (optional)

## Installation

1. **Repository hinzufügen** in HA unter  
   *Einstellungen → Add-ons → Add-on Store → ⋮ → Repositories*:
   ```
   https://github.com/XtraLarge/ha-offsite-backup
   ```

2. **Add-on installieren**: "Offsite Backup" im Store suchen und installieren

3. **Secrets einrichten** (SSH-Keys, Hetzner API Token) – siehe [DOCS.md](offsite-backup/DOCS.md)

4. **Add-on konfigurieren** (NAS-IP, Hetzner Storage Box, Backup-Zeitplan)

5. **Add-on starten**

## Backup-Architektur

```
HA-RPi (Add-on)                 NAS (Proxmox/Docker)           Hetzner
┌─────────────────┐   SSH+Pipe  ┌──────────────────┐  rsync   ┌──────────────┐
│  backup.sh      │ ──────────► │  backup_nas.sh   │ ───────► │ Storage Box  │
│  (Cron / API)   │  Agent-Fwd  │  (ZFS Snapshot   │          │ Snap_YYYY-MM │
│                 │             │   rsync to        │  API     │              │
│  recovery.sh    │   Docker    │   Hetzner)       │ ───────► │ API Snapshot │
│  (BackupPC)     │   Socket    └──────────────────┘          └──────────────┘
└─────────────────┘
```

## Dateien

| Pfad | Beschreibung |
|------|-------------|
| `/data/secrets/id_ed25519_nas` | SSH-Key → NAS |
| `/data/secrets/id_ed25519_hetzner` | SSH-Key → Hetzner (Agent Forwarding) |
| `/data/secrets/hetzner_token` | Hetzner API Token |
| `/data/secrets/id_ed25519_recovery` | SSH-Key für Remote-Recovery (optional) |
| `/data/logs/backup.log` | Backup-Log |
| `/data/logs/status.json` | Letzter Backup-Status |

## Sicherheit

- Alle SSH-Keys und Tokens liegen in `/data/secrets/` (nur für dieses Add-on zugänglich)
- Hetzner API Token wird **nicht** im HA-Config-UI gespeichert
- Hetzner-Key wird per **SSH Agent Forwarding** zur NAS weitergeleitet (kein Copy auf NAS)
- `no-pty,no-port-forwarding,no-X11-forwarding` auf NAS enforced

Detaillierte Einrichtung: [DOCS.md](offsite-backup/DOCS.md)
