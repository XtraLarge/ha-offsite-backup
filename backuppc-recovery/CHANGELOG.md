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
