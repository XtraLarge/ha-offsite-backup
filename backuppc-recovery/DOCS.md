# BackupPC Recovery

Startet eine vollständige BackupPC4-Umgebung auf Home Assistant, die direkt auf die Hetzner Storage Box zugreift (via SSHFS). Nur bei Bedarf starten – z. B. nach einem Ausfall des primären NAS.

**Hinweis:** Dieses Add-on ist unabhängig vom *Offsite Backup* Add-on konfigurierbar. Beide Add-ons können mit denselben Hetzner-Zugangsdaten betrieben werden.

## Einrichtung

1. **SSH-Key hinterlegen:** Den privaten SSH-Key für die Hetzner Storage Box unter `ssh_key_hetzner` eintragen. Der Key muss Zugriff auf `<user>@<host>:/home/ZPool` haben.
2. **Hetzner-Zugangsdaten** eintragen: `hetzner_user`, `hetzner_host` (z. B. `u527284.your-storagebox.de`), `hetzner_port` (Standard: 23).
3. **MQTT** optional: Host, Port, User und Passwort für MQTT-Status-Reporting.

## Nutzung

Nach dem Start des Add-ons:

- BackupPC Web-UI: `http://<HA-IP>:8900/BackupPC/`
- BackupsDisable=2 ist automatisch gesetzt (kein neuer Backup, nur Wiederherstellung)
- Der SSHFS-Mount liegt unter `/mnt/hetzner`, die BackupPC-Daten unter `/mnt/hetzner/BackupPC`

Das Add-on kann über den MQTT-Schalter *Recovery Umgebung* im Offsite-Backup-Dashboard gestartet und gestoppt werden.

## Sicherheit

- Der SSH-Key wird unter `/data/secrets/id_ed25519_hetzner` gespeichert und ist nur für Root lesbar.
- BackupPC läuft im Read-only-Modus (BackupsDisable=2) – es werden keine neuen Sicherungen erstellt.
- `StrictHostKeyChecking=no` ist für den SSHFS-Mount gesetzt; die Hetzner-Hostkeys sind beim ersten Mount unbekannt.
