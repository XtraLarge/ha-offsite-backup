## 2.0.12

- SSHFS als read-only (`ro`) gemountet — verhindert dass Recovery-BackupPC LOCK-Dateien und andere Schreiboperationen auf Hetzner ausführt und dabei laufende rsync-Transfers stört

## 2.0.11

- `$Conf{LogDir}` auf `/data/backuppc/log` (lokal) gesetzt — verhindert dass Recovery-BackupPC seine Logs auf Hetzner (SSHFS/TopDir) schreibt und dort Produktionslogs überschreibt

## 2.0.10

- TopDir-Setzen: Perl-Regex `^\$Conf{TopDir}` matcht nicht wenn config.pl eingerückte Zeilen verwendet (adferrand/BackupPC-Standard) → TopDir zeigte auf NAS-Pfad der im Container nicht existiert → BackupPC las falschen Pool
- Fix: Gleiches Muster wie BackupsDisable/CgiAdminUsers: erst alle `$Conf{TopDir}`-Zeilen entfernen, dann korrekte Zeile anhängen

## 2.0.9

- Datenstand (neuestes Host-Backup) beim Start ermitteln + in `/data/datastand` schreiben
- state.py: Datenstand per HTTP auf Port 9080 exponieren + in MQTT-Sensor einbinden

## 2.0.8

- Config-Import-Flag komplett entfernt — Config wird bei jedem Start frisch von SSHFS importiert
- Grund: Flag überlebt Container-Rebuilds (Docker-Volume persistent), `/etc/backuppc` im Container aber nicht → nach Add-on-Update lief BackupPC mit Default-Config ohne Hosts
- Für eine Recovery-Umgebung ist "immer frisch importieren" korrekt: kein veralteter Zustand möglich

## 2.0.7

- `BackupsDisable=2` jetzt zuverlässig gesetzt: vorhandene Einträge in importierter Config werden vor dem Anhängen entfernt — bisher wurde `= 2` übersprungen wenn Hetzner-Config bereits `BackupsDisable = 0` enthielt

## 2.0.6

- SSH-Key-Schreiben: `printf '%b\n'` statt `jq -r ... > file` — HA-UI speichert Keys einzeilig mit `\n`-Literalen statt echten Newlines; `printf '%b'` konvertiert diese korrekt, `jq -r` ließ sie unverändert → libcrypto-Fehler / SSHFS "Connection reset by peer"

## 2.0.5

- `aarch64` (ARM64/Raspberry Pi) als unterstützte Architektur ergänzt

## 2.0.4

- Optionsnamen umbenannt: `hetzner_user/host/port` → `offsite_user/host/port`, `ssh_key_hetzner` → `ssh_key_offsite`
- Interne Variablen (`HETZNER_*`) entsprechend auf `OFFSITE_*` angepasst

## 2.0.3

- (kein separater Eintrag — build-fix intern)

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
