# Changelog

## 1.7.0 - 2026-07-18

### Behoben / Neu
- **Offsite-Lauf schlug mit rc=1 fehl, sobald das Hetzner-Storage-Box-Snapshot-
  Limit erreicht war.** Der Daten-Transfer (rsync) war vollstaendig erfolgreich;
  nur der letzte Schritt `create_storagebox_snapshot` scheiterte, weil die Box
  (Plan bx31) `snapshot_limit=30` hat und bereits 30 Snapshots existierten. Das
  Add-on hatte bisher KEINE Retention – die woechentlichen `Snap_<Datum>`-Snapshots
  akkumulierten unbegrenzt (verstaerkt durch die in 1.4.1 behobene Auto-Resume-
  Schleife, die mehrere Snapshots/Tag erzeugte).
  Fix: Neue Snapshot-Retention. `backup_nas.sh` loescht vor dem Erstellen eines
  neuen Snapshots die aeltesten EIGENEN Snapshots (Beschreibung `Snap_...`), bis
  weniger als `offsite_snapshot_keep` uebrig sind, und wartet, bis die Loeschung
  wirkt. Fremde Snapshots (andere Beschreibung) werden nie angetastet.
- **Neue Option `offsite_snapshot_keep`** (Default `20`): maximale Zahl der vom
  Add-on gehaltenen Offsite-Snapshots. `0`/ungueltig = Auto-Loeschen deaktiviert.

## 1.6.0 - 2026-07-17

### Neu — Self-validating Offsite-Backup (Wissen #751)
- **Der Erfolgsstatus einer Offsite-Sicherung kommt jetzt aus einem Recovery-
  Smoke-Test, nicht mehr allein aus rc=0.** Nach jedem erfolgreichen Transfer
  startet das Add-on automatisch die BackupPC-Recovery-Umgebung, die die
  Offsite-Kopie verifiziert: (1) sind die erwarteten Hosts sichtbar, (2) sind die
  jüngsten Backup-Zeitpunkte plausibel, (3) lässt sich eine kleine Datei aus dem
  jüngsten Backup wiederherstellen (echter Durchstich Offsite→Pool→lesbar). Nur
  wenn ALLE drei grün sind → `success`; sonst `failed` mit Ursache. Während der
  Prüfung steht der Status auf `verifying`.
- **Neue Option `smoke_test_after_backup`** (Default `true`): schaltet den
  automatischen Post-Backup-Smoke ein/aus.
- Läuft die Recovery-Umgebung bereits (manuelle Nutzung), wird der Smoke
  übersprungen statt die Sitzung zu stören.

### Behoben
- **False-Positive `failed` nach erfolgreichem Lauf (Wissen #744).** Der HA-seitige
  Launcher (`backup.sh`) schrieb den Erfolgsstatus aus seinem eigenen Exit-Code —
  einem Proxy, der bei SSH-Pipe-Abriss / Container-Neustart fehlschlägt, obwohl der
  Lauf auf der NAS sauber weiterläuft. Diese widersprüchliche Statusquelle ist
  entfernt; autoritativ ist jetzt allein `api.py:_finalize_from_nas` (NAS-Exit-Code)
  plus der nachgelagerte Smoke-Test.

### Härtung (Wissen #747/#748)
- **Offsite-rsync des BackupPC-Pools maskiert das transiente `pending-delete`-
  Markerbit** (`--chmod=Fo-x`, nur auf dem Pool-Pfad). Das other-execute-Bit
  (S_IXOTH → Mode 0445) wechselt im Normalbetrieb ständig; mit `-p`+`-W` erzeugte
  jeder Wechsel einen Perm-Diff und damit CoW-Churn auf der Hetzner-Box (bis zum
  Box-Überlauf). Die Offsite-Kopie bekommt nun stets den kanonischen Pool-Mode
  0444; der Inhalt bleibt unverändert (BackupPC leitet die Marker selbst wieder ab),
  die Modes der übrigen Quellen bleiben 1:1.

### Behoben — Host-OOM auf HA-Pi (Wissen #1497)
- **Das Dashboard-Backend lud die gesamte `backup.log` in den RAM.** `api.py`
  `read_log()`/`read_finished_log()` nutzten `f.readlines()`, um nur die letzten
  N Zeilen zu liefern — bei einem unrotiert auf 372 MB gewachsenen Log blähte das
  den RSS auf ~2,6 GB und löste auf dem 3,7-GB-Pi einen globalen Kernel-OOM aus
  (killte `python3`). Ersetzt durch einen bounded Tail (`list(deque(f, maxlen=N))`)
  — konstanter Speicher unabhängig von der Dateigröße.
- **`backup.log` wird jetzt vor jedem Lauf hart auf 5 MiB begrenzt** (Size-Cap-
  Rotation in `backup.sh`). Die vollständige Historie je Lauf bleibt archiviert
  unter `/data/logs/runs/`; der Live-Mirror bleibt dauerhaft klein.
- **Auto-Resume stoppt bei permanentem Fehlerbild dauerhaft.** Bei einem
  permanenten Offsite-Fehler (z. B. Storagebox-Quota voll: `Disk quota exceeded`)
  wird ein PERSISTENTER Marker (`/data/permanent-fail`) gesetzt, der den
  automatischen 30-min-Resume unterbindet. Bisher lag der Versuchszähler nur im
  RAM — ein OOM-Neustart setzte ihn zurück, wodurch der 3-Versuche-Deckel nie
  griff und getaktete Volllast-/OOM-Läufe gegen die volle Quota liefen. Der
  Marker überlebt den Neustart und wird bei einem erfolgreichen Lauf oder einem
  manuellen/geplanten Start automatisch wieder aufgehoben.


## 1.4.1 - 2026-06-30

### Behoben
- **Endlose „stalled → Auto-Resume (Versuch 1/3)"-Schleife (False Positive).**
  Nach einem erfolgreichen Lauf löschte der Watcher das RunDir, doch die bereits
  beendete NAS-`screen`-Session blieb als „Dead" sichtbar; die Zustandssonde sah
  dann `screen=1 / RunDir=0 / exit=–` und stufte das als `stalled` ein → unnötiger
  Auto-Resume eines kompletten Voll-Backups. Da der Versuchszähler bei jedem
  Finalisieren genullt wird, lief das endlos (immer „Versuch 1/3"), 24/7.
  Fix: (1) `_classify_state` wertet screen/proc-lebt-aber-RunDir-weg als `idle`
  (Post-Finalize-Zombie), NICHT als `stalled`; ein ECHTER Hänger (RunDir vorhanden,
  `run.log` stale) wird unverändert als `stalled` erkannt. (2) `_finalize_from_nas`
  räumt die beendete `screen`-Session ab (`-X quit` + `-wipe`), damit keine
  „Dead"-Session zurückbleibt, die den nächsten geplanten Lauf blockieren könnte.
  Nebeneffekt behoben: `status.json` meldet nicht mehr fälschlich `failed`, während
  ein gesunder Lauf abgeschlossen wurde. Regressionstest: `tests/classify_state_test.py`.

## 1.4.0 - 2026-06-12

### Neu
- **Hänger-Erkennung + Auto-Resume.** Der Lauf-Status wird nicht mehr an der
  bloßen Existenz der NAS-`screen`-Session festgemacht (ein toter/idle screen ohne
  RunDir wurde sonst dauerhaft als „läuft" gemeldet), sondern an einer
  Zustandssonde aus `screen` + Prozess + RunDir + `exit_code` + `run.log`-Alter.
  Zustände: `running | stalled | crashed | finished | idle`.
- Hängt oder crasht ein Lauf, wird aufgeräumt (screen/Prozesse beendet, verwaiste
  `pre_rsync`-Snapshots gelöscht, RunDir entfernt) und nach Backoff (30 min)
  **automatisch wiederaufgenommen** — bis zu 3 Versuche.
- **Manueller Abbruch ≠ Crash:** „Abbrechen" setzt einen Marker
  (`/data/aborted-by-user`) → **keine** automatische Wiederaufnahme. Nur Crash/
  Hänger lösen Resume aus.
- Neue Option `auto_resume_backup` (Default `true`) zum globalen Abschalten der
  automatischen Wiederaufnahme.
- Dashboard-Fortschritt zeigt den neuen Zustand: „Hängt – Wiederaufnahme in
  ~N min (Versuch x/3)".

## 1.3.4 - 2026-05-30

### Behoben

- **„BackupPC starten" im Dashboard schlug fehl** mit `HTTP Error 400: Bad Request` (seit 2.1.0). Der Supervisor-`/options`-Endpoint ersetzt die Optionen vollständig und validiert gegen das ganze Schema — die Recovery verlangt seit 2.1.0 `offsite_path` und `backup_sources` als Pflichtfelder, der Start-Payload in `trigger_recovery()` reichte beide aber nicht mit (`Missing option 'offsite_path' / 'backup_sources'`). Beide werden jetzt aus den Offsite-Optionen durchgereicht — fixt den Start und spiegelt zugleich das Backup-Mapping 1:1 an die Recovery.

## 1.3.3 - 2026-05-30

### Behoben

- CI „Validate Add-on" wird wieder grün (schlug bei jedem Push fehl):
  - `squash: false` aus `build.yaml` entfernt — vom Supervisor (Docker Buildkit) nicht mehr unterstützt.
  - Deprecated Architekturen `armhf`/`armv7` aus `build.yaml` (`build_from`) und `config.yaml` (`arch`) entfernt — seit HA 2025.12 nicht mehr unterstützt. Bleibt `aarch64` (Raspberry Pi 4) + `amd64`.
  - `ingress_port: 8099` aus `config.yaml` entfernt — entspricht dem Default, der Linter beanstandet redundante Defaults. Verhalten unverändert.
- ShellCheck-Warnungen in `scripts/` beseitigt: ungenutzte `body`-Variable entfernt, `local desc` von der Zuweisung getrennt (SC2155), die zwei bewusst client-seitig expandierenden Heredocs in `backup.sh` mit `# shellcheck disable=SC2087` annotiert.

## 1.3.2 - 2026-05-30

### Geändert

- `backup_sources`-Schema umsortiert: `dest` steht jetzt an erster Stelle. Die HA-Options-UI nutzt das erste Feld als Zeilen-Titel — bisher war das `dataset`, das nur die ZFS-Snapshot-Quelle füllt, sodass die vier `path`-basierten Einträge ohne Titel angezeigt wurden. `dest` ist Pflichtfeld und in jedem Eintrag gesetzt → jede Zeile zeigt nun ihren Ziel-Pfad. Rein kosmetisch, keine Verhaltensänderung (Skripte lesen Felder per Name).

## 1.3.1 - 2026-05-30

### Geändert

- **Fortschrittsanzeige an `backup_sources` angepasst.** Die alten festen Phasen-Muster (`rsync Docker Config (3/3)`, `BackupPC Pool (1/3)`, `Erstelle Hetzner Snapshot`) matchten nach dem 1.3.0-Umbau nicht mehr. `get_progress()` ermittelt den Stand jetzt serverseitig aus dem laufenden NAS-`run.log`:
  - **Quelle X/N** aus dem neuen `Quelle i/NUM_SRC: dest`-Marker im Treiber-Loop.
  - **Pool-%** aus `N Shards zu übertragen` + Zählung der neuen `Shard fertig: <shard>`-Marker (ab der letzten Kopfzeile, damit kein begrenztes Tail-Fenster nötig ist). Beispiel: `Quelle 1/5 · ZPool/BackupPC · Pool 75/159 (47%)`.
  - **Offsite-Snapshot-%** aus `Snapshot-Status: … (NN%)`, danach `Fertig`.
- Ein zusätzlicher, 8 s gecachter NAS-Aufruf (forced `bash -s`) berechnet die kompakte Statuszeile — keine großen Logs werden übertragen.

## 1.3.0 - 2026-05-30

### Hinzugefügt
- **Frei konfigurierbare Backup-Quellen (`backup_sources`).** Die bisher fest verdrahteten Mountpoints (ZFS-Pool + Docker-Verzeichnisse) sind jetzt eine Liste in der Add-on-Konfiguration. Jeder Eintrag hat `dataset`/`path` (Quelle), `dest` (Zielpfad relativ zu `offsite_path` auf Hetzner), `snapshot` (ZFS-Snapshot ja/nein, pro Quelle), `parallel` (sharded rsync), sowie `recovery`/`container_mount`/`recovery_clean` für die Recovery-Umgebung. Die mitgelieferten Defaults reproduzieren exakt das bisherige Verhalten (Parität).
- `offsite_path` (Wurzel auf der Storage Box, Default `/home`) und `snapshot_prefix` (Default `pre_rsync`) als eigene Optionen.

### Geändert
- `backup_nas.sh`: Die fest codierten Snapshot-/rsync-Blöcke sind durch eine Schleife über `backup_sources` ersetzt. Einzeldatei-Quellen (z. B. `ssh_config`) werden erkannt und ohne `--delete*` übertragen.
- `backup.sh`: reicht `backup_sources` (base64), `offsite_path` und `snapshot_prefix` an die NAS-Session weiter.

## 1.2.49 - 2026-05-29

### Behoben
- **Erfolgreiche Backups wurden fälschlich als „failed" markiert.** Ursache (jetzt dank des persistenten Log-Archivs aus 1.2.48 sichtbar): `backup_nas.sh:293` rief `unset OFFSITE_TOKEN_LOCAL` auf, obwohl die Variable in Zeile 87 `readonly` ist — das schlägt fehl und beendet das Skript unter `set -euo pipefail` mit rc=1, obwohl alle Schritte (159 Shards, ZFS-Snapshot-Cleanup, Docker-Dirs, Hetzner-Offsite-Snapshot) erfolgreich waren. Die redundante `unset`-Zeile entfernt (der Prozess endet ohnehin unmittelbar danach, die Variable verschwindet mit ihm).

## 1.2.48 - 2026-05-29

### Hinzugefügt
- **Vollständiges run.log wird nach Abschluss persistent auf hassio archiviert** (`/data/logs/runs/backup-<zeitstempel>.log`, mit Status-/rc-Header; rotierend, letzte 20 Läufe). Bisher lag das NAS-Log nur im tmpfs (`/dev/shm`) und ging beim Aufräumen verloren — fiel mitten im Lauf der Launcher aus (Container-Neustart), war ein fehlgeschlagener Lauf nicht mehr nachvollziehbar.

### Geändert
- `api.py`: `_finalize_from_nas` ist jetzt alleiniger Abschluss-Besitzer und holt das **vollständige run.log von der NAS, BEVOR** das tmpfs-RunDir gelöscht wird. Schlägt der Abruf fehl (NAS kurz nicht erreichbar), bleibt das RunDir stehen und der Watcher versucht es bei den nächsten Ticks erneut, statt das Log zu verwerfen.
- `api.py`: Die Idle-Logansicht (`/api/log`, Dashboard) zeigt jetzt das vollständige archivierte Log des letzten Laufs (`read_finished_log`) statt des evtl. abgeschnittenen Live-Spiegels `backup.log`.
- `backup.sh`: löscht das RunDir nicht mehr selbst — das übernimmt der Finalizer nach dem Log-Abruf. Dadurch wird das Log auch dann gesichert, wenn dieser Launcher mitten im Lauf stirbt.

## 1.2.47 - 2026-05-29

### Hinzugefügt
- `api.py`: NAS-Watcher-Thread (`_nas_watch_loop`/`_finalize_from_nas`). Endet die screen-Session auf der NAS, während der lokale Launcher nicht mehr läuft (z. B. Container-Neustart mitten im Lauf), liest der Watcher den Exit-Code aus dem RunDir, schreibt `status.json` nach und räumt das tmpfs-RunDir auf. Schließt die letzte Lücke der Neustart-Resilienz: ein Lauf wird auch dann korrekt abgeschlossen, wenn das Add-on während des Backups neu startet.

## 1.2.46 - 2026-05-29

### Behoben
- Status/Abbruch/Aufräumen funktionierten nach dem Umbau auf 1.2.44 nicht: Der Storage-Key ist in `authorized_keys` der NAS auf `command="bash -s"` festgenagelt (forced command), wodurch Argument-Befehle (`ssh nas "screen -ls …"`) ignoriert werden und das erzwungene `bash -s` nur leeres stdin liest (rc=0, keine Ausgabe). `is_backup_running()` zeigte dadurch dauerhaft „kein Backup". Alle NAS-Befehle (`_nas_ssh`, Exit-Code-Abruf und Aufräumen in `backup.sh`) werden jetzt über stdin an `bash -s` gepipt. Der Backup-Lauf selbst war nie betroffen (Launcher/Log-Tail nutzten bereits `bash -s` per stdin).

## 1.2.44 - 2026-05-29

### Geändert
- **Architektur: Backup läuft jetzt in einer detached `screen`-Session AUF DER NAS** statt direkt an der SSH-Pipe vom HA-Add-on. Damit überleben Läufe Add-on-/Container-Neustarts und Netzwerkprobleme zwischen RPi und NAS — die SSH-Pipe spiegelt nur noch das Log, das Backup selbst ist davon entkoppelt. Das beseitigt die Hauptursache verwaister rsync-Prozesse (reparented auf init), die den `pre_rsync`-Snapshot blockierten.
- **Offsite-Auth nur noch im RAM der NAS** (`nas_bootstrap.sh`): in der screen-Session wird ein `ssh-agent` gestartet, der private Offsite-Key per stdin (nie als Argument → nicht in `ps`) nach tmpfs (`/dev/shm`) übertragen, in den Agent geladen und die Datei sofort geschreddert. `SSH_AUTH_SOCK` und `OFFSITE_API_TOKEN` werden als Umgebungsvariablen an die rsync-/ssh-Kindprozesse vererbt. Kein Geheimnis landet je auf der NAS-Platte.

### Hinzugefügt
- `nas_bootstrap.sh`: Bootstrap, der in der screen-Session die RAM-only-Auth aufsetzt und `backup_nas.sh` ausführt; schreibt den Exit-Code nach `/dev/shm/offsite-backup/exit_code`.

### Behoben
- `api.py`: `is_backup_running()`/`abort_backup()` fragen jetzt die screen-Session auf der NAS als Quelle der Wahrheit ab (gecacht, SSH via Storage-Key), mit lokaler Lock-Datei als Rückfallebene wenn die NAS nicht erreichbar ist. Abbruch beendet die screen-Session und killt verwaiste rsync-Prozesse auf der NAS. `/api/log` holt das Log während eines Laufs direkt von der NAS.
- `backup_nas.sh`: `kill_stale_backup_procs` brach unter `set -e`/`pipefail` ab, wenn ein bereits beendeter Prozess (Erfolgsfall nach SIGTERM) den `[[ -d /proc/$p ]]`-Test fehlschlagen ließ. Schleifen jetzt mit `|| true` und `return 0` abgesichert.

## 1.2.43 - 2026-05-29

### Geändert
- `backup_nas.sh`: `--no-inc-recursive` entfernt. Auf der NAS wurde verifiziert, dass BackupPC v4 keine FS-Hardlinks nutzt (`nlink=1` überall) — der einzige Vorteil des Flags (vollständige Hardlink-Erkennung) ist damit gegenstandslos. Inkrementelle Rekursion (Default) startet den Transfer sofort beim Scannen statt erst nach komplettem Dateilisten-Aufbau je Shard und braucht deutlich weniger RAM.

## 1.2.42 - 2026-05-29

### Hinzugefügt
- `backup_nas.sh`: Paralleler Pool-Transfer. Der BackupPC-Pool wird in Shards (Verzeichnisse auf Tiefe 2: `cpool/<hex>`, `pc/<host>`) aufgeteilt und mit konfigurierbar `RSYNC_PARALLEL_JOBS` (Default 6) gleichzeitigen rsync-Streams übertragen. Jeder Stream nutzt eine eigene SSH-Verbindung (`SSH_CMD_NOCTL`, kein gemeinsamer ControlMaster) für eigenes Congestion-Window + parallele Verschlüsselung. Vorab ein Struktur-Pass (Tiefe ≤2) für Top-Level-Dateien, Verzeichnisgerüst und `--delete` verwaister Einträge.
- `backup_nas.sh`: `kill_stale_backup_procs` beendet vor dem Snapshot-Cleanup verwaiste rsync/ssh-Prozesse früherer Läufe (z. B. nach abgebrochener SSH-Pipe), die den `pre_rsync`-Snapshot blockieren würden.

### Geändert
- `backup_nas.sh`: rsync nutzt jetzt `--whole-file` (`-W`). cpool-Dateien sind unveränderliche, inhaltsadressierte Chunks → der Delta-Algorithmus bringt nichts, kostet aber CPU/IO; `-W` überträgt geänderte Dateien direkt komplett.

### Hinweis
- Sharding ist verlustfrei: BackupPC v4 nutzt keine FS-Hardlinks (Pool inhaltsadressiert, pc/-Bäume via Referenzzählung), verifiziert auf der NAS (alle Stichproben `nlink=1`). Die parallele Offsite-Kopie ist strukturell identisch zur Einzel-rsync-Kopie und über die Recovery-Umgebung lesbar.

## 1.2.41 - 2026-05-29

### Behoben
- Dashboard war komplett leer: `abortBackup()` enthielt im `confirm()`-Text echte Zeilenumbrüche (`\n\n` im Python-Triple-Quote-String wurde zu echten Newlines), was den JS-String über mehrere Zeilen brach → Syntaxfehler → gesamter `<script>`-Block wurde nicht ausgeführt, kein `loadStatus()`/`loadLog()`. Newlines jetzt als `\\n` escaped, sodass im ausgelieferten JS echte `\n`-Escape-Sequenzen stehen.

## 1.2.40 - 2026-05-29

### Behoben
- `loadStatus()`: null-Checks für alle neuen Element-IDs (`backup-running-row`, `start-btn`, `abort-btn`) — verhindert TypeError wenn Browser eine gecachte ältere HTML-Version hat
- Fehler in `loadStatus()` werden jetzt mit Kontext ins Console-Log geschrieben

## 1.2.39 - 2026-05-29

### Hinzugefügt
- Dashboard: "Läuft seit"-Zeile mit Spinner und Fortschritt sichtbar wenn Backup aktiv
- Dashboard: "Backup abbrechen"-Button (rot) erscheint während eines laufenden Backups, ersetzt den Start-Button
- `POST /api/backup/abort` Endpoint: beendet den laufenden Backup-Prozess (SSH zur NAS)

## 1.2.38 - 2026-05-28

### Behoben
- `backup_nas.sh`: `zfs destroy` bei "dataset is busy" bricht nicht mehr den Backup-Lauf ab
- Neue Funktion `zfs_destroy_retry`: 3 Versuche mit 30s Pause, dann `zfs destroy -d` (deferred) als Fallback
- Snapshot-Diagnose im Log: vorhandene pre_rsync-Snapshots (inkl. defer_destroy-Status) werden zu Beginn aufgelistet

## 1.2.37 - 2026-05-28

### Geändert
- SSH-Key-Karte aus Dashboard entfernt — Keys werden nur noch in der HA Add-on-Konfiguration gesetzt
- `POST /api/options` Endpunkt entfernt (nicht mehr benötigt)
- Recovery-Slug-Erkennung läuft jetzt im Hintergrund-Thread → HTTP-Server startet sofort, kein 10-Sekunden-Block beim Add-on-Start

## 1.2.36 - 2026-05-28

### Geändert
- Snapshot-Auswahl aus Dashboard entfernt — Hetzner API-Snapshots sind per SFTP nicht zugänglich, Recovery läuft immer im Live-Modus

## 1.2.35 - 2026-05-28

### Geändert
- `RECOVERY_ADDON_SLUG` wird jetzt dynamisch via Supervisor API ermittelt — funktioniert sowohl mit lokalen Add-ons als auch mit GitHub-Repository-Installationen

## 1.2.34 - 2026-05-28

### Geändert
- `RECOVERY_ADDON_SLUG` und `RECOVERY_STATUS_URL` auf `local_backuppc_recovery` / `local-backuppc-recovery` aktualisiert (Add-ons jetzt als lokale Add-ons installiert statt aus GitHub-Repository)

## 1.2.33 - 2026-05-28

### Geändert
- Dashboard: SSH-Key-Eingabe als mehrzeilige Textareas (Karte 3 "SSH Keys")
- Keys werden mit `\n`-Kodierung gespeichert (HA-Kompatibilität) — `printf '%b\n'` in run.sh konvertiert korrekt zurück
- Neuer POST `/api/options` Endpoint: aktualisiert `ssh_key_storage`/`ssh_key_offsite` via Supervisor API

## 1.2.32 - 2026-05-28

### Geändert
- Dashboard neu strukturiert: 3 Karten statt 4 (Status, BackupPC Recovery Umgebung, Log)
- "BackupPC Umgebung" + "Hetzner Snapshots" zu "BackupPC Recovery Umgebung" zusammengeführt
- Snapshot-Auswahl: Radio-Buttons statt Dropdown + separater Snapshot-Tabelle
- "Live-Daten (aktuell)" als erste Radio-Option
- Snapshots aktualisieren: kleines ↻-Icon oben rechts im Karten-Header
- Log aktualisieren: kleines ↻-Icon oben rechts im Log-Karten-Header (weg aus Status)
- BackupPC-UI-Link öffnet direkt `/BackupPC_Admin` (korrekter Port 8080)

## 1.2.31 - 2026-05-27

### Geändert
- Optionsnamen rollen-basiert umbenannt (weg von Gerätetyp/Hersteller):
  - `nas_host` → `zfs_storage_host`, `nas_user` → `zfs_storage_user`
  - `hetzner_user/host/port/box_id/token` → `offsite_user/host/port/box_id/token`
  - `ssh_key_nas` → `ssh_key_storage`, `ssh_key_hetzner` → `ssh_key_offsite`
- Tote Optionen `recovery_target` und `ssh_key_recovery` entfernt
- Interne Variablen (`NAS_*`, `TARGET_*`, `HETZNER_*`) entsprechend angepasst
- Secret-Dateien: `id_ed25519_nas` → `id_ed25519_storage`, `id_ed25519_hetzner` → `id_ed25519_offsite`, `hetzner_token` → `offsite_token`

## 1.2.30 - 2026-05-27

### Geändert
- Log-Button "Log aktualisieren" zeigt jetzt Toast-Bestätigung "Log aktualisiert" nach erfolgreichem Refresh
- Log-Bereich scrollt automatisch nach unten wenn man am Ende war (Auto-Scroll während Backup)

## 1.2.29 - 2026-05-27

### Behoben
- `_write_secret` nutzt jetzt `printf '%b\n'` statt `printf '%b'` — Command-Substitution `$()` schneidet abschließenden Zeilenumbruch ab, wodurch libcrypto den SSH-Key ablehnte (`error in libcrypto`)
- `crontab /etc/cron.d/offsite-backup` entfernt — Zeile installierte Cron-Eintrag doppelt als User-Crontab ohne User-Feld, was dazu führte dass `root` als Kommando interpretiert wurde (`/bin/sh: 1: root: not found`)

## 1.2.28 - 2026-05-27

### Geändert
- `backuppc_port` Default von 8900 → **8080** (BackupPC Recovery v2.0 läuft jetzt auf Port 8080)
- Hinweis: Wer bereits `backuppc_port: 8900` konfiguriert hat, muss das in den Add-on-Optionen auf 8080 ändern

## 1.2.27 - 2026-05-27

### Behoben
- `run.sh` Shebang auf `#!/usr/bin/with-contenv bash` geändert — s6-overlay v3 lädt Docker-Umgebungsvariablen (SUPERVISOR_TOKEN etc.) nur wenn `with-contenv` verwendet wird; ohne es waren keine Supervisor-Variablen im Prozess verfügbar

## 1.2.23 - 2026-05-27

### Behoben
- `hassio_api: true` ergänzt (war in v1.2.20 entfernt worden) — ohne dieses Flag injiziert der Supervisor keinen SUPERVISOR_TOKEN, auch wenn `hassio_role: manager` gesetzt ist

## 1.2.22 - 2026-05-27

### Geändert
- BackupPC-Steuerung nutzt jetzt SUPERVISOR_TOKEN direkt (`hassio_role: manager`) mit `http://supervisor/` — LLAT hatte keine Berechtigung für Supervisor-API
- `ha_token`-Option entfernt (nicht mehr benötigt)
- `backuppc_port` (Standard: 8900) konfigurierbar — steuert die URL des "BackupPC UI öffnen"-Buttons
- Dashboard: "BackupPC UI öffnen"-Button erscheint wenn BackupPC läuft (öffnet neues Tab)
- `/api/options` gibt keine sensiblen Felder mehr zurück (SSH-Keys, Tokens, MQTT-Passwort)

## 1.2.21 - 2026-05-27

### Behoben
- Port 8123 in HA-API-URL ergänzt (`http://homeassistant:8123/api/hassio/`) — Port 80 lieferte 404

## 1.2.20 - 2026-05-27

### Geändert
- BackupPC-Steuerung nutzt jetzt direkt einen HA Long-Lived Access Token (`ha_token`) — kein SUPERVISOR_TOKEN mehr
- `homeassistant_api: true` statt `hassio_api`; Supervisor API via `http://homeassistant/api/hassio/`

## 1.2.18 - 2026-05-27

### Behoben
- `hassio_role: manager` ergänzt — nur damit injiziert der Supervisor `SUPERVISOR_TOKEN` in den Container (mit `default`-Rolle wurde der Token nicht gesetzt, BackupPC starten/beenden schlug fehl)

## 1.2.12 - 2026-05-27

### Behoben
- `HASSIO_TOKEN` als Fallback für ältere HA-Versionen ergänzt (war vorher nur `SUPERVISOR_TOKEN`)
- Startup-Log zeigt ob Supervisor-Token verfügbar ist

## 1.2.11 - 2026-05-27

### Geändert
- Zeitangaben (Letzter Lauf, Nächster Backup, Snapshots) in lesbares deutsches Format `27.05.2026, 20:00` umgewandelt — Zeitzone des Browsers wird automatisch berücksichtigt
- Snapshots chronologisch sortiert (neuester zuerst), auch im Dropdown

## 1.2.10 - 2026-05-27

### Geändert
- Hetzner Snapshots werden beim Seitenaufruf automatisch geladen
- Button umbenannt zu "Snapshots aktualisieren"

## 1.2.9 - 2026-05-27

### Behoben
- Dashboard JavaScript und CSS komplett defekt wegen Python-Format-String-Escaping (`{{`/`}}` nie aufgelöst → JS-Syntaxfehler → kein einziger API-Call lief)
- Fix: DASHBOARD_HTML nutzt jetzt normale `{`/`}` statt Python-Format-Escaping

## 1.2.8 - 2026-05-27

### Behoben
- Dashboard-Basis-Pfad wird jetzt vom Server via `X-Ingress-Path`-Header injiziert (statt `window.location.pathname`) — behebt leere Werte im HA App-WebView und im Browser

## 1.2.7 - 2026-05-27

### Hinzugefügt
- Snapshot-Auswahl direkt im Dashboard: Dropdown "Datenquelle" im BackupPC-Umgebung-Card; wird beim Laden der Hetzner-Snapshots automatisch befüllt
- "BackupPC starten" übergibt die Auswahl per Supervisor API an backuppc-recovery (inkl. Hetzner-Zugangsdaten) — keine separate Konfiguration in backuppc-recovery nötig
- Bestätigungsdialog zeigt gewählte Datenquelle an

## 1.2.6 - 2026-05-27

### Behoben
- `Cache-Control: no-store` auf HTML-Antwort — Browser/App cached die Seite nicht mehr
- `_normalize_path()` matcht API-Routen jetzt als exaktes Suffix statt `/api/`-Split (korrekt auch wenn Ingress-Pfad selbst `/api/` enthält)

## 1.2.5 - 2026-05-27

### Behoben
- Dashboard: Alle API-Aufrufe schlugen fehl wenn `INGRESS_PATH` nicht gesetzt war (fetch-Basis dynamisch aus `window.location` abgeleitet, Server-Routing via `_normalize_path()` robust gemacht)
- Dashboard: "Snapshots laden" in den Hetzner-Snapshots-Abschnitt verschoben

### Geändert
- "Recovery (BackupPC)" → "BackupPC Umgebung" (Karte, Status-Zeile, Buttons, Dialoge)

## 1.2.4 - 2026-05-27

### Geändert
- Recovery-Steuerung nutzt jetzt Supervisor API (`/addons/3e98a749_backuppc_recovery/start|stop`) statt lokalem Shell-Script
- Recovery-Status wird direkt vom Supervisor abgefragt (kein Lock-File mehr)
- `hassio_api: true` ergänzt, damit der Supervisor-Endpunkt erreichbar ist

## 1.2.3 - 2026-05-27

### Behoben
- AppArmor deaktiviert (`apparmor: false`) — blockierte SSHFS-Mount für Recovery

## 1.2.2 - 2026-05-27

### Behoben
- `SYS_ADMIN` Capability ergänzt — ermöglicht SSHFS-Mount für Recovery

## 1.2.1 - 2026-05-27

### Behoben
- `next_run` Zeitstempel jetzt mit Zeitzone (ISO 8601) — behebt "unbekannt" in HA timestamp-Sensor

## 1.2.0 - 2026-05-27

### Hinzugefügt
- Externe MQTT-Verbindung konfigurierbar (`mqtt_host`, `mqtt_port`, `mqtt_user`, `mqtt_password`)
- MQTT-Credentials aus Add-on-Optionen haben Vorrang vor Supervisor-Discovery

## 1.1.0 - 2026-05-27

### Hinzugefügt
- SSH-Keys und Hetzner-Token als `password`-Felder in der Add-on-Konfiguration
- `run.sh` schreibt Secrets beim Start als Dateien nach `/data/secrets/`
- MQTT Auto-Discovery: Sensoren, Binary-Sensoren, Button und Switch für Home Assistant
- `next_run` Berechnung via `croniter` (nächste geplante Ausführung)
- Fortschrittsanzeige via Log-Parsing (`ZFS Snapshot`, `rsync BackupPC Pool (1/3)` etc.)
- GitHub Actions: Add-on-Linter und ShellCheck

### Geändert
- `repository.yaml`: Korrektes Format (`name`, `url`, `maintainer`)
- `config.yaml`: Schema bereinigt, ungültiges `map: ssl:false` entfernt

## 1.0.0 - 2026-05-27

### Erstveröffentlichung
- Offsite Backup via rsync + ZFS Snapshot → Hetzner Storage Box
- Hetzner API Snapshot nach jedem Backup
- BackupPC Recovery lokal (Docker-Socket) oder remote (SSH)
- Web-Dashboard mit Status, Log und Snapshot-Übersicht
- Cron-Scheduler konfigurierbar
- Loki-Logging optional
