# BackupPC Recovery – Documentation

**English** | [Deutsch](DOCS.de.md)

## Overview

The BackupPC Recovery add-on starts a full **BackupPC4 environment** directly on Home Assistant. It accesses the offsite storage box via SSHFS and makes all backups available through the familiar BackupPC web interface.

**Important:** this add-on is **only meant to be started when needed** – typically after a NAS failure, to restore files, or to verify backups. It does not run during normal operation.

**No new backups:** `BackupsDisable=2` is always set – no automatic backups are started.

---

## Control

### Via the Offsite Backup dashboard (recommended)

The **Offsite Backup** add-on starts and stops this add-on automatically:

1. In the Offsite Backup dashboard, choose the **data source**:
   - **Live data (current):** the current state of the storage box
   - **Select a snapshot:** an earlier point in time (dropdown with all storage-box snapshots)

2. Click **Start BackupPC** – the add-on configures itself automatically with the offsite credentials from the Offsite Backup add-on

3. Wait until the status switches to "running" (about 30–60 seconds)

4. **Open BackupPC UI** – the button appears automatically once the add-on is running

5. After recovery: click **Stop BackupPC**

### Manually (without the Offsite Backup add-on)

The add-on can also be configured and started on its own. Enter all fields in the HA configuration (see below).

---

## Configuration

| Field | Description | Example |
|-------|-------------|---------|
| `offsite_user` | Offsite storage-box username | `u123456` |
| `offsite_host` | Offsite storage-box hostname | `u123456.your-storagebox.de` |
| `offsite_port` | SSH port (default: 23) | `23` |
| `snapshot_name` | Snapshot name for data access (empty = live) | `Snap_YYYY-MM-DD` |
| `ssh_key_offsite` | Private SSH key for the offsite storage box | (multi-line) |
| `mqtt_host` | MQTT broker (optional) | `192.168.1.10` |
| `mqtt_port` | MQTT port | `1883` |
| `mqtt_user` | MQTT user | |
| `mqtt_password` | MQTT password | |

> When the add-on is started via the Offsite Backup dashboard, `offsite_user`, `offsite_host`, `offsite_port`, `snapshot_name`, `ssh_key_offsite` and the MQTT data are transferred automatically – no manual entry needed.

---

## Web UI

After startup the BackupPC interface is reachable at:

```
http://<HA-IP>:8080/BackupPC_Admin
```

Or via the **"Open BackupPC UI"** button in the Offsite Backup dashboard.

- **Port:** 8080 (not via HA ingress – direct access)
- **No login needed:** `REMOTE_USER=backuppc` is set automatically
- **Read-only mode:** no new backups are started

---

## Startup sequence

On add-on startup the following happens:

1. **Create user:** `backuppc` (UID/GID 1000) is created
2. **Set up BackupPC** (first start only): `configure.pl` runs against `/data/backuppc` (local, not SSHFS – avoids `chown` issues)
3. **Write SSH key:** `ssh_key_offsite` is written to `/data/secrets/id_ed25519_offsite`
4. **Mount SSHFS:** `<offsite_user>@<offsite_host>:/home/.snapshots/<snapshot>/ZPool` or `/home/ZPool` (live)
5. **Import BackupPC config** (once per snapshot): the config is copied from `<mount>/Docker/backuppc/config/` to `/etc/backuppc/`
6. **Set TopDir:** `$Conf{TopDir}` is set to `<sshfs-mount>/BackupPC`
7. **Start lighttpd + BackupPC** via supervisord

The config import happens **once** per snapshot (detected via a flag file). When the snapshot changes, it is re-imported.

---

## MQTT status

When MQTT is configured, the following entities are published:

| Entity | Type | Description |
|--------|------|-------------|
| `binary_sensor.backuppc_lauft` | Binary sensor | Add-on started/stopped |
| `sensor.backuppc_url` | Sensor | URL of the web UI |
| `sensor.backuppc_datenquelle` | Sensor | `Live` or snapshot name |

---

## Troubleshooting

### SSHFS mount failed

```
ERROR: SSHFS mount failed (rc=...)
```

Causes and checks:
- Invalid SSH key: does the key start with `-----BEGIN OPENSSH PRIVATE KEY-----`?
- Offsite host unreachable: is `ping <offsite_host>` working and port 23 reachable?
- Does the add-on have the `SYS_ADMIN` capability and `/dev/fuse` – is it running as privileged?

### BackupPC does not start: `can't find command BackupPC`

The first-start setup (`configure.pl`) failed. Check the add-on log:
```
HA → Add-ons → BackupPC Recovery → Log
```

Fix: stop the add-on, reset `/data/firstrun` (in the container) and restart. Or: uninstall and reinstall the add-on (this deletes `/data/`).

### Web UI shows a blank page or 404

- Check the URL: `http://<HA-IP>:8080/BackupPC_Admin` (not `/BackupPC/`)
- Wait a moment – BackupPC needs 30–60 seconds to start
- Check the add-on log for errors

### Config import fails

```
cp: cannot overwrite non-directory ...
```

An old config-import conflict. Delete the flag file:
```bash
# In the SSH terminal on HA:
rm /data/addon_configs/3e98a749_backuppc_recovery/config-imported-v2*
```
Then restart the add-on – the config is re-imported.

### lighttpd does not start: `Opening errorlog failed`

```bash
mkdir -p /var/log/lighttpd
```
(Normally created automatically by `run.sh` – only occurs with a corrupted `/data`.)

---

## Technical details

**Base image:** `adferrand/backuppc:4.4.0-12` (Alpine Linux + lighttpd + supervisord)

**Process management via supervisord:**
- `backuppc`: `/usr/local/BackupPC/bin/BackupPC`
- `lighttpd`: `/usr/sbin/lighttpd`
- `watchmails`: watches the msmtp mail log

**Auth bypass for recovery:**
the lighttpd `auth.conf` is replaced with:
```nginx
setenv.add-environment = ("REMOTE_USER" => "backuppc")
```
No password needed – the add-on is only reachable on the local network (port 8080 not via HA ingress).

**Data directories in the container:**
| Path | Content |
|------|---------|
| `/mnt/hetzner` | SSHFS mount of the offsite storage box |
| `/mnt/hetzner/BackupPC` | BackupPC data base (TopDir) |
| `/etc/backuppc` | BackupPC configuration (imported from offsite) |
| `/data/backuppc` | BackupPC runtime data (local, persistent) |
| `/data/secrets` | SSH keys |
| `/usr/local/BackupPC` | BackupPC4 installation |
