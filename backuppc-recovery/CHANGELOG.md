## 2.2.0

- **Recovery-Smoke-Test (Wissen #751).** Neues Modul `smoke.py`: prüft nach dem
  Start die Offsite-Kopie auf (1) erwartete Hosts (pc/ ↔ BackupPC-hosts-Konfig),
  (2) plausible jüngste Backup-Zeitpunkte je Host, (3) Mini-Restore einer kleinen
  Datei aus dem jüngsten Backup via `BackupPC_tarCreate` (echter Durchstich
  Offsite→Pool→lesbar). Das Ergebnis wird nach `/data/smoke.json` geschrieben und
  über den HTTP-Status (`:9080/smoke`) ausgeliefert. Das Offsite-Backup-Add-on holt
  es dort ab und leitet daraus den Erfolgsstatus der Sicherung ab (nur 3/3 grün →
  `success`). Der Smoke läuft im Hintergrund und stört die manuelle Recovery-Nutzung
  nicht.

## 2.1.2

- `squash: false` aus `build.yaml` entfernt — vom Supervisor (Docker Buildkit) nicht mehr unterstützt (Konsistenz mit dem Offsite-Add-on, das deswegen im CI-Linter fehlschlug).

## 2.1.1

- `backup_sources`-Schema umsortiert: `dest` zuerst. Die HA-Options-UI nimmt das erste Feld als Zeilen-Titel — vorher `dataset` (nur bei der Snapshot-Quelle gesetzt), wodurch die vier `path`-basierten Einträge ohne Titel erschienen. `dest` ist gesetzt in jedem Eintrag → jede Zeile zeigt nun ihren Pfad. Rein kosmetisch, identisch zum Offsite-Add-on gehalten.

## 2.1.0

- **Recovery-Mounts werden aus `backup_sources` abgeleitet** — dieselbe Liste wie im Offsite-Backup-Add-on (identische Defaults, keine geteilte Datei). Über das 1:1-Mapping auf Hetzner (`dest` relativ zu `offsite_path`) bedient sich die Recovery aus genau den Pfaden, die das Backup geschrieben hat:
  - `recovery: topdir` → `$Conf{TopDir}` + Datenstand-Ermittlung (statt fest `/mnt/hetzner/BackupPC`)
  - `recovery: import` → kopiert `dest` nach `container_mount`; Verzeichnisse rekursiv, Dateien einzeln (neu: `ssh_config` → `/etc/ssh/ssh_config`, damit SSH-Zugriffskeys/Host-Registrierung erhalten bleiben und beim Recovery keine Neuregistrierung der Geräte nötig ist). `recovery_clean: true` leert das Ziel vorher.
  - `recovery: none` → reine Backup-Quelle (z. B. `_DockerCreate`), bei Recovery ignoriert.
- `offsite_path` als eigene Option (Default `/home`); SSHFS-Mount-Wurzel statt fest `/home/ZPool`.
- LogDir bleibt lokal (`/data/backuppc/log`) — Arbeitsdaten weichen bewusst vom Mapping ab.
- SSHFS bleibt read-only (`ro`).

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
