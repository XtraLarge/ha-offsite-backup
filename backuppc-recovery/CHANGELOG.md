## 1.0.7

- Apache BackupPC-Route explizit in `run.sh` konfiguriert — `a2enconf backuppc` schlug im Dockerfile-Build lautlos fehl, `/BackupPC/` lieferte 404
- Login: `backuppc` / `backuppc` (htpasswd wird beim Start erzeugt)
- `$Conf{CgiAdminUsers}` auf `"backuppc"` gesetzt — Hetzner-Config konnte abweichenden Admin-User haben

## 1.0.6

- Config-Import: `/etc/backuppc/` vor `cp -a` leeren — Container-Default hatte `/etc/backuppc/pc` als Datei, Hetzner-Quelle hat `pc` als Verzeichnis → `cp` Typ-Konflikt → Abbruch

## 1.0.5

- SSH-Key-Schreiben: `jq -r ... > file` statt `printf '%b'` — Command-Substitution `$()` schneidet abschließenden Zeilenumbruch ab, wodurch libcrypto den Key nicht laden konnte (`error in libcrypto`)
- SSHFS-Debug-Flags (`sshfs_debug`, `loglevel=DEBUG3`) entfernt
- Validierung: PEM-Header-Check nach Key-Schreiben

## 1.0.3

- `GlobalKnownHostsFile=/dev/null` ergänzt — System-known_hosts blockierte SSHFS-Verbindung zu Hetzner (Host key verification failed)

## 1.0.2

- `#!/usr/bin/with-contenv bash` Shebang (s6-overlay v3 Kompatibilität)
- SFTP-Verbindungstest vor SSHFS-Mount mit detaillierter Fehlerausgabe
- SSHFS-Optionen vereinfacht (kein `reconnect`, kein `uid/gid`)

## 1.0.1

- Snapshot-Modus: `snapshot_name` Option; leer = Live-Daten, sonst Zugriff auf `/home/.snapshots/<name>/ZPool`
- Config-Import-Flag jetzt snapshot-spezifisch (Neuimport bei Snapshot-Wechsel)
- MQTT-Sensor "BackupPC Datenquelle" zeigt ob Live oder welcher Snapshot aktiv
- Logos und Icons hinzugefügt

## 1.0.0

- Erstveröffentlichung: BackupPC4 Recovery-Umgebung als eigenständiges HA Add-on
- SSHFS-Mount auf Hetzner Storage Box
- BackupPC-Config-Import beim ersten Start (einmalig)
- BackupsDisable=2 (kein neuer Backup, nur Wiederherstellung)
- Apache auf Port 8900, BackupPC Web-UI unter `/BackupPC/`
- MQTT Auto-Discovery: binary_sensor (running) + sensor (URL)
