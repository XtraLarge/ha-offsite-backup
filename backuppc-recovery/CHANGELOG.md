## 2.0.0

**Kompletter Umbau** — Basis-Image gewechselt auf `adferrand/backuppc:4.4.0-12`

- Basis: `adferrand/backuppc:4.4.0-12` (Alpine + lighttpd) statt Debian-Paket — gleiche Basis wie die produktive BackupPC4-Umgebung
- Port: **8080** statt 8900
- Web-UI URL: `/BackupPC_Admin` statt `/BackupPC/`
- lighttpd statt Apache — kein Auth-Problem, kein `a2enconf`-Problem
- `REMOTE_USER=backuppc` via lighttpd `setenv.add-environment`
- `BACKUPPC_HOME` korrigiert: `/home/backuppc` statt `/var/lib/backuppc`
- BackupPC-Daemon: `/usr/local/BackupPC/bin/BackupPC` via supervisord
- Import-Flag umbenannt auf `config-imported-v2` (erzwingt frischen Import bei Upgrade)
- `ENTRYPOINT ["/run.sh"]` überschreibt adferrand-Entrypoint explizit

## 1.0.8

- Apache: Authentifizierung entfernt (`htpasswd` fehlt im Image, `apache2-utils` nicht installiert → 403)
- `SetEnv REMOTE_USER backuppc` setzt Admin-User direkt im CGI-Environment — kein Login nötig
- Port 8900 ist nur im lokalen Netz erreichbar (kein Passwort-Schutz erforderlich)

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
