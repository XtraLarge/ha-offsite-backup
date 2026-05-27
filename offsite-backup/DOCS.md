# Offsite Backup – Add-on Dokumentation

## Übersicht

Dieses Add-on ersetzt einen dedizierten rsyncos-Host (Raspberry Pi). Es läuft direkt auf
Home Assistant und übernimmt:

- **Wöchentlicher Offsite-Backup** (rsync, ZFS-Snapshot → Hetzner Storage Box)
- **Hetzner Storage Box Snapshot** nach jedem Backup
- **BackupPC-Recovery** direkt auf dem HA-RPi (kein extra Docker-Host nötig)
- **Loki-Logging** (optional)

---

## 1. Secrets einrichten (vor dem ersten Start)

Alle SSH-Keys und Tokens werden in `/data/secrets/` gespeichert (nur für das Add-on zugänglich).
Über das **Terminal & SSH Add-on** oder **SSH Server Add-on** einrichten:

```bash
# Verzeichnis anlegen
mkdir -p /addon_configs/offsite_backup/secrets   # falls du es von außen bearbeiten willst
# oder direkt ins Add-on-Data-Verzeichnis schreiben:
# Dateimanager Add-on → /data/offsite_backup/secrets/

# Benötigte Dateien:
# 1. SSH-Key für Verbindung rsyncos → NAS
cat > /data/offsite_backup/secrets/id_ed25519_nas << 'EOF'
-----BEGIN OPENSSH PRIVATE KEY-----
<deinen Key hier einfügen>
-----END OPENSSH PRIVATE KEY-----
EOF

# 2. SSH-Key für rsync → Hetzner Storage Box (wird per Agent Forwarding zur NAS geleitet)
cat > /data/offsite_backup/secrets/id_ed25519_hetzner << 'EOF'
-----BEGIN OPENSSH PRIVATE KEY-----
<deinen Key hier einfügen>
-----END OPENSSH PRIVATE KEY-----
EOF

# 3. Hetzner API Token (für Snapshot-Erstellung)
echo "dein-hetzner-api-token" > /data/offsite_backup/secrets/hetzner_token

# 4. (Optional) SSH-Key für Remote-Recovery (nur wenn recovery_target != "local")
cat > /data/offsite_backup/secrets/id_ed25519_recovery << 'EOF'
-----BEGIN OPENSSH PRIVATE KEY-----
<deinen Key hier einfügen>
-----END OPENSSH PRIVATE KEY-----
EOF

# Berechtigungen setzen
chmod 600 /data/offsite_backup/secrets/*
```

### NAS: authorized_keys eintragen

Der Public Key von `id_ed25519_nas` muss auf der NAS in `/root/.ssh/authorized_keys`
eingetragen sein (mit Befehlseinschränkung):

```
command="bash -s",no-pty,no-port-forwarding,no-X11-forwarding ssh-ed25519 AAAA... ha_offsite_backup
```

---

## 2. Add-on konfigurieren

In der HA-Oberfläche unter **Add-on → Konfiguration**:

| Option | Beschreibung | Beispiel |
|--------|-------------|---------|
| `nas_host` | IP/Hostname der NAS | `10.10.5.10` |
| `nas_user` | SSH-Benutzer auf NAS | `root` |
| `hetzner_user` | Hetzner Storage Box Benutzername | `u123456` |
| `hetzner_host` | Hetzner Storage Box Hostname | `u123456.your-storagebox.de` |
| `hetzner_port` | SSH-Port der Storage Box | `23` |
| `hetzner_box_id` | Hetzner Storage Box ID (für API) | `510043` |
| `backup_schedule` | Cron-Ausdruck für automatischen Backup | `0 20 * * 3` (Mittwoch 20:00) |
| `loki_url` | Loki Push-URL (leer = deaktiviert) | `http://10.10.10.4:3100/loki/api/v1/push` |
| `recovery_target` | `local` oder IP eines Docker-Hosts | `local` |

---

## 3. Backup-Ablauf

```
HA-RPi (Add-on)
  │  Lädt Hetzner-Key in SSH-Agent
  │  SSH mit Agent-Forwarding
  ▼
NAS (10.10.5.x)
  │  Empfängt backup_nas.sh via stdin
  │  Erstellt ZFS-Snapshot (ZPool/BackupPC)
  │  rsync → Hetzner (via Agent-forwarded Key)
  │  rsync Docker/backuppc, Docker/_DockerCreate
  │  Erstellt Hetzner Storage Box Snapshot via API
  ▼
Hetzner Storage Box
  Snap_YYYY-MM-DD
```

---

## 4. Recovery

### Lokal auf HA-RPi (Standardmodus: `recovery_target: local`)

Benötigt: `id_ed25519_hetzner` in `/data/secrets/`

1. Im Dashboard auf **Recovery starten** klicken
2. BackupPC-UI öffnen: `http://<HA-IP>:8900`
3. Nach der Recovery: **Recovery beenden** klicken

Das Add-on:
- Mountet Hetzner Storage Box via SSHFS
- Kopiert BackupPC-Config lokal (SSHFS unterstützt kein `chown`)
- Startet BackupPC-Container via Docker-Socket (zweiphasig wegen chown-Problem)
- Setzt `BackupsDisable=2` (keine automatischen Sicherungen während Recovery)

### Remote (anderer Docker-Host)

`recovery_target` auf IP des Ziel-Hosts setzen. Benötigt `id_ed25519_recovery`
für SSH-Zugang zu diesem Host als root.

---

## 5. Troubleshooting

**Backup schlägt fehl mit "Secret fehlt"**
→ Secrets prüfen: alle 4 Dateien in `/data/secrets/` vorhanden?

**SSH zur NAS: Permission denied**
→ Public Key von `id_ed25519_nas` in NAS `/root/.ssh/authorized_keys` eingetragen?

**Recovery: SSHFS-Mount fehlgeschlagen**
→ Add-on hat `devices: /dev/fuse` – läuft das Add-on als privileged?
→ `id_ed25519_hetzner` vorhanden und korrekt?

**Cron läuft nicht**
→ Backup-Zeitplan im Log prüfen: Dashboard → Log-Bereich
→ Manuell testen: Dashboard → "Backup jetzt starten"
