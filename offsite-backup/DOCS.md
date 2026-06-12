# Offsite Backup – Documentation

**English** | [Deutsch](DOCS.de.md)

## Overview

This add-on runs on Home Assistant and drives the weekly offsite backup of the NAS to the storage box. It connects to the NAS via SSH and runs `backup_nas.sh` there, which creates a ZFS snapshot and transfers it to the offsite target via rsync.

**What this add-on does:**
- Automatic backup on a schedule (cron) or manually via the dashboard
- ZFS snapshot on the NAS → rsync to the offsite target with retry logic
- Storage-box snapshot via API after a successful backup
- Web dashboard with status, log and snapshot overview
- Start/stop the BackupPC Recovery add-on directly from the dashboard
- MQTT sensors for HA integration (status, timestamps, progress)

---

## 1. Requirements

### Storage-host setup

The storage host (the one the add-on connects to via SSH) must have:

- A ZFS pool `ZPool` with the dataset `ZPool/BackupPC` (BackupPC data)
- Optional: `/ZPool/Docker/backuppc/` and `/ZPool/Docker/_DockerCreate/` (Docker config)
- An SSH server on port 22 (default)
- `rsync`, `openssh-client`, `jq`, `zfsutils-linux` installed (the script checks/installs them)

### Offsite storage box

- SSH access with an Ed25519 key enabled
- Port 23 (the default for storage boxes)
- Target directories: `/home/ZPool/BackupPC/` and `/home/ZPool/Docker/`

### Register the SSH key on the storage host

Add the **public key** of `ssh_key_storage` to `/root/.ssh/authorized_keys` on the storage host:

```
command="bash -s",no-pty,no-port-forwarding,no-X11-forwarding ssh-ed25519 AAAA... ha-offsite-storage
```

Derive the public key from the private key:
```bash
ssh-keygen -y -f id_ed25519_storage
```

---

## 2. Configuration

All fields are entered in the HA interface under **Add-on → Configuration**.

### Required fields

| Field | Description | Example |
|-------|-------------|---------|
| `zfs_storage_host` | Hostname or IP of the storage host | `nas.example.local` |
| `zfs_storage_user` | SSH user on the storage host | `root` |
| `offsite_user` | Offsite storage-box username | `u123456` |
| `offsite_host` | Offsite storage-box hostname | `u123456.your-storagebox.de` |
| `offsite_port` | SSH port of the storage box | `23` |
| `offsite_box_id` | Numeric storage-box ID | `123456` |
| `backup_schedule` | Cron expression (container time = UTC) | `0 18 * * 3` |
| `ssh_key_storage` | Private SSH key for the storage-host connection | (multi-line) |
| `ssh_key_offsite` | Private SSH key for the offsite storage box | (multi-line) |
| `offsite_token` | Offsite storage API token | `hGsX7...` |

> **Time-zone note:** the container runs in UTC. `0 18 * * 3` equals Wednesday 20:00 CEST (UTC+2). Adjust the cron time accordingly.

### Optional fields

| Field | Description | Default |
|-------|-------------|---------|
| `loki_url` | Loki push URL for remote logging | empty (disabled) |
| `backuppc_port` | Port of the BackupPC Recovery add-on | `8080` |
| `mqtt_host` | MQTT broker IP/hostname | empty |
| `mqtt_port` | MQTT broker port | `1883` |
| `mqtt_user` | MQTT user | empty |
| `mqtt_password` | MQTT password | empty |

### Entering SSH keys

SSH keys (`ssh_key_storage`, `ssh_key_offsite`) are entered as `password` fields (masked with `*` in the HA UI). Two formats are accepted:

**Multi-line** (paste directly with Enter):
```
-----BEGIN OPENSSH PRIVATE KEY-----
b3BlbnNzaC1rZXktdjEA...
-----END OPENSSH PRIVATE KEY-----
```

**Single-line** (with `\n` as the line separator):
```
-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEA...\n-----END OPENSSH PRIVATE KEY-----\n
```

---

## 3. Web dashboard

The dashboard is reachable via the HA sidebar entry "Offsite Backup" (ingress on port 8099).

### Status card
- **Last run:** timestamp of the last backup start
- **Result:** `success` (green), `failed` (red), `unknown`
- **NAS:** currently configured NAS host
- **Schedule:** cron expression
- **Next backup:** computed next run time
- **BackupPC:** whether the recovery environment is running

### Actions
- **Start backup now:** manual backup start (with a confirmation dialog)
- **Refresh log:** reload the log area immediately (toast confirmation + auto-scroll)

### BackupPC environment card
- **Data source:** dropdown for live data or a storage-box snapshot
- **Start BackupPC:** starts the recovery add-on with the selected data
- **Stop BackupPC:** stops the recovery add-on
- **Open BackupPC UI:** opens `http://<HA-IP>:8080` in a new tab (only when active)

### Snapshots card
- Lists all snapshots of the storage box (name, date, description)
- Snapshots can be selected directly in the dropdown of the BackupPC card

### Log card
- Shows the last 100 lines of the backup log
- Refreshes automatically every 30 seconds
- Auto-scrolls to the bottom if you were already at the end

---

## 4. Backup flow in detail

### What `backup.sh` does (runs in the add-on container):

1. Start the SSH agent and load the offsite key
2. Send `backup_nas.sh` to the NAS via an SSH pipe (with agent forwarding)
3. On completion: send the Loki log, write status to `/data/logs/status.json`

### What `backup_nas.sh` does (runs on the NAS via SSH):

1. Check/install dependencies (rsync, jq, zfsutils-linux)
2. Validate the offsite API token
3. Delete old `pre_rsync_*` snapshots
4. For each source with `snapshot: true`, create a ZFS snapshot: `<dataset>@pre_rsync_YYYY-MM-DD_HH-MM-SS`
5. For **each** source in `backup_sources`, one rsync to `<offsite_path>/<dest>/` (with up to 5 retries; `parallel: true` transfers large pools in shards)
6. Delete the ZFS snapshots again
7. Create a storage-box snapshot via API (`Snap_YYYY-MM-DD`)

> Which sources are backed up is driven by the `backup_sources` option – see the `backup_sources` concept in the [README](../README.md). Earlier versions hard-wired the three rsync targets; since 1.3.0 the list is configurable.

### Retry logic

rsync automatically retries on network errors (rc 10, 11, 12, 30, 35, 255):
- Default: 5 retries with a 120-second pause
- IO timeout: 600 seconds without data transfer → error

---

## 5. MQTT integration

When MQTT is configured, the following entities are published via auto-discovery:

| Entity | Type | Description |
|--------|------|-------------|
| `sensor.backup_status` | Sensor | `success` / `failed` / `unknown` |
| `sensor.letzter_backup` | Timestamp sensor | Last backup timestamp |
| `sensor.nachster_backup` | Timestamp sensor | Next scheduled backup |
| `sensor.backup_fortschritt` | Sensor | Current step during backup |
| `binary_sensor.backup_lauft` | Binary sensor | Is a backup running? |
| `binary_sensor.recovery_aktiv` | Binary sensor | Is BackupPC Recovery active? |
| `button.backup_starten` | Button | Trigger a backup manually |
| `switch.recovery_umgebung` | Switch | Start/stop recovery |

---

## 6. Troubleshooting

### Dashboard shows "running" but no progress (stall)
Since 1.4.0 the add-on detects this itself: the run state is derived from a probe
(screen + process + run dir + `exit_code` + `run.log` age), no longer from the
mere existence of the `screen` session. A detected stall (`stalled`/`crashed`) is
cleaned up automatically (screen/processes killed, orphaned `pre_rsync` snapshots
destroyed, run dir removed) and **auto-resumed up to 3 times** after a 30 min
backoff. Progress then shows "Hängt – Wiederaufnahme in ~N min (Versuch x/3)". A
**manual** abort click writes the marker `/data/aborted-by-user` and suppresses
resume. Disable globally via the option `auto_resume_backup: false`.

### Backup fails: `error in libcrypto`
The SSH key is corrupted. Regenerate the key and enter it in the HA configuration.

### Backup fails: `Permission denied (publickey)`
The public key of `ssh_key_storage` is not in the `authorized_keys` of the storage host. Check the entry:
```bash
grep 'ha-offsite' /root/.ssh/authorized_keys
```

### Backup fails: `dataset does not exist`
`zfs_storage_host` points at the wrong host, or `ZPool/BackupPC` does not exist.
Check: `zfs list ZPool/BackupPC` on the configured storage host.

### Backup fails: `dataset is busy`
An old rsync process is holding the ZFS snapshot.
```bash
fuser /ZPool/BackupPC/.zfs/snapshot/
# Identify the process and stop it if needed:
kill -9 <PID>
zfs destroy ZPool/BackupPC@pre_rsync_...
```

### The cron backup does not run at the expected time
The container runs in UTC. Example: `0 18 * * 3` = Wednesday 18:00 UTC = 20:00 CEST.

### Dashboard empty / API not responding
Restart the add-on. Check the HA add-on log (not the backup log in the dashboard).

### SUPERVISOR_TOKEN not available
The add-on is not configured with `hassio_role: manager`. Recovery control will not work.
Make sure the add-on's `config.yaml` contains `hassio_role: manager` and `hassio_api: true`.
