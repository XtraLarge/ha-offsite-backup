# ha-offsite-backup

**English** | [Deutsch](README.de.md)

A Home Assistant add-on repository for **automatic offsite backups via rsync + ZFS snapshot** to a remote storage host – with an integrated **BackupPC4 recovery environment** that runs directly inside Home Assistant.

> **In short:** Your NAS backs itself up to a remote location every week. If the NAS ever dies, you start a full BackupPC interface inside Home Assistant with a single click and restore individual files or whole backups – without touching the original.

---

## What it does

- **Automatic offsite backup** on a schedule (cron) or manually via the web dashboard
- **ZFS snapshot** on the storage host for a consistent state, then **rsync** to the offsite target (with retry logic for network drops)
- **Storage-box snapshot** via API after a successful run (versioned restore points)
- **Web dashboard** with status, live log, snapshot list and progress indicator
- **BackupPC recovery at the push of a button** – accesses the offsite target directly via SSHFS, without copying data back
- **MQTT auto-discovery** for status sensors, buttons and switches in Home Assistant

## Included add-ons

| Add-on | Function | Runs |
|--------|----------|------|
| **Offsite Backup** | Drives the backup schedule, rsync, snapshots and the dashboard | continuously |
| **BackupPC Recovery** | BackupPC4 web interface with direct offsite access (SSHFS) | only when needed |

---

## How it works

```
 Home Assistant                  Storage host (ZFS)            Offsite target
 ┌────────────────────┐  SSH     ┌──────────────────┐  rsync   ┌──────────────────┐
 │ Offsite Backup     │─────────▶│ ZFS snapshot     │─────────▶│ <offsite_path>/  │
 │  • cron scheduler  │  (pipe)  │ rsync per source │          │   ZPool/BackupPC  │
 │  • web dashboard   │          └──────────────────┘   API    │   Docker/...      │
 │  • MQTT status     │──────────────────────────────────────▶ │   Snap_YYYY-MM-DD │
 │                    │                                         └──────────────────┘
 │ BackupPC Recovery  │  SSHFS (read-only)                              ▲
 │  (on demand)       │─────────────────────────────────────────────────┘
 │  web UI :8080      │   accesses the offsite snapshots directly
 └────────────────────┘
```

**Backup flow:**
1. The add-on opens an SSH session to the storage host and pushes the backup script over via a pipe (the offsite key stays inside the add-on and is only used via agent forwarding – it never lands on the storage host).
2. On the storage host: a ZFS snapshot of the configured datasets for a consistent state.
3. For each source in `backup_sources`, one rsync to the offsite target (large pools are parallelised in shards).
4. Clean up the ZFS snapshot, then create a versioned storage-box snapshot via API (`Snap_YYYY-MM-DD`).

**Recovery flow:**
1. In the dashboard, pick the **data source** – the live state or an older offsite snapshot.
2. **Start BackupPC** – the recovery add-on automatically receives the credentials and `backup_sources` and mounts the offsite target **read-only** via SSHFS.
3. The BackupPC web interface is reachable after ~30–60 s at `http://<HA-IP>:8080/BackupPC_Admin`.
4. Restore files as usual – **no new backups** are written (`BackupsDisable=2`).

### The `backup_sources` concept

What gets backed up and how it is mounted during recovery is driven by a single list – `backup_sources`. Both add-ons share the same structure, so recovery serves itself from exactly the paths the backup wrote (1:1 mapping via `dest` relative to `offsite_path`):

| Field | Meaning |
|-------|---------|
| `dest` | Target path on the offsite target, relative to `offsite_path` (e.g. `ZPool/BackupPC`) |
| `dataset` | ZFS dataset to snapshot (empty = no snapshot) |
| `path` | Source path on the storage host when there is no dataset (e.g. a directory) |
| `snapshot` | `true` = take a ZFS snapshot before rsync |
| `parallel` | `true` = transfer a large pool in parallel shards |
| `recovery` | `topdir` (BackupPC pool) · `import` (copy into container) · `none` (backup only) |
| `container_mount` | Target path inside the recovery container for `recovery: import` |
| `recovery_clean` | `true` = empty the target before importing |

> Want to back up a new source? Add one entry to the list – backup **and** recovery follow automatically. No shared file, no code change.

---

## Installation

### 1. Add the repository

In Home Assistant: **Settings → Add-ons → Add-on Store → ⋮ (top right) → Repositories**

```
https://github.com/XtraLarge/ha-offsite-backup
```

### 2. Install the add-ons

Two add-ons appear in the store:
- **Offsite Backup** – install and configure this first
- **BackupPC Recovery** – install it, but do **not** start it manually (the dashboard handles that)

### 3. Prepare SSH keys (once)

You need two Ed25519 key pairs:

```bash
ssh-keygen -t ed25519 -f storage_key  -C "ha-offsite-storage"   # storage host
ssh-keygen -t ed25519 -f offsite_key  -C "ha-offsite-target"    # offsite target
```

Add the **public part of the storage key** to `/root/.ssh/authorized_keys` on the storage host – with a `command=` restriction so that this key can **only** run the backup script, never an interactive shell:

```
command="bash -s",no-pty,no-port-forwarding,no-X11-forwarding ssh-ed25519 AAAA... ha-offsite-storage
```

Register the **public part of the offsite key** with your offsite provider (e.g. the storage-box management panel).

### 4. Configure the add-on

In the **Offsite Backup** add-on under **Configuration**:

| Field | Description |
|-------|-------------|
| `zfs_storage_host` | Hostname/IP of the storage host with the ZFS pool |
| `zfs_storage_user` | SSH user there (default `root`) |
| `offsite_user` / `offsite_host` / `offsite_port` | Access to the offsite target (storage-box port is often 23) |
| `offsite_box_id` | Numeric ID of the offsite target (for API snapshots) |
| `offsite_token` | API token of the offsite provider |
| `offsite_path` | Root path on the offsite target (default `/home`) |
| `backup_schedule` | Cron expression – **the container runs in UTC** (e.g. `0 18 * * 3` = Wed 20:00 CEST) |
| `ssh_key_storage` / `ssh_key_offsite` | The two private keys (as `password` fields) |
| `backup_sources` | List of sources (see above) – ships with sensible defaults |
| `mqtt_*`, `loki_url` | Optional: HA sensors / remote logging |

> SSH keys can be entered multi-line (with Enter) or single-line with `\n` separators. They are stored as `password` fields and never appear in the log or the dashboard API.

### 5. Start

Start the **Offsite Backup** add-on. You can trigger the first run in the dashboard with **"Start backup now"** and follow it in the live log. The recovery environment is controlled entirely through the dashboard.

---

## Generic example

Suppose your setup looks like this:

- **Storage host:** `nas.example.local`, ZFS pool `ZPool`, BackupPC data under `ZPool/BackupPC`
- **Offsite target:** a storage box `u123456.your-storagebox.de`, port 23, ID `123456`, root `/home`

**Step 1 – register the key on the NAS** (`nas.example.local`, as root):

```
command="bash -s",no-pty,no-port-forwarding,no-X11-forwarding ssh-ed25519 AAAAC3Nz...storage ha-offsite-storage
```

**Step 2 – add-on configuration** (abridged):

```yaml
zfs_storage_host: "nas.example.local"
zfs_storage_user: "root"
offsite_user: "u123456"
offsite_host: "u123456.your-storagebox.de"
offsite_port: 23
offsite_box_id: 123456
offsite_path: "/home"
backup_schedule: "0 18 * * 3"        # Wednesday 20:00 CEST (18:00 UTC)
backup_sources:
  - dest: "ZPool/BackupPC"           # → /home/ZPool/BackupPC on the target
    dataset: "ZPool/BackupPC"
    snapshot: true
    parallel: true
    recovery: "topdir"
    container_mount: "/data/backuppc"
  - dest: "ZPool/Docker/backuppc/config"
    path: "/ZPool/Docker/backuppc/config"
    recovery: "import"
    container_mount: "/etc/backuppc"
    recovery_clean: true
```

**Result:** every Wednesday at 20:00 the add-on takes a snapshot of `ZPool/BackupPC`, transfers it (in parallel shards) to `/home/ZPool/BackupPC` on the storage box, copies the BackupPC config alongside it, and finally creates a `Snap_2026-…` snapshot. If the NAS fails, you start recovery from the dashboard, choose live or a snapshot, and work directly in the BackupPC interface.

---

## Security notes

- All keys and tokens are `password` fields – not in the log, not in the dashboard API (`/api/options` hides sensitive fields).
- The offsite key leaves the add-on **only via agent forwarding** and is never copied to the storage host.
- The storage-host connection is restricted to the backup script via `command="bash -s"` – no interactive shell through this key.
- Recovery mounts the offsite target **read-only** so it cannot disturb ongoing transfers.
- `AppArmor` is disabled and `SYS_ADMIN` is set – needed exclusively for SSHFS (FUSE) in recovery.

---

## Acknowledgements

This project stands on the shoulders of two wonderful pieces of open-source work – and I want to thank the people behind them from the bottom of my heart:

- **[BackupPC](https://backuppc.github.io/backuppc/)** by **Craig Barratt** – the brilliant, decades-matured backup engine that makes the whole concept of deduplicated pool backups possible in the first place. Without this foundation there would be nothing here to restore.
- **[adferrand/docker-backuppc](https://github.com/adferrand/docker-backuppc)** by **Adrien Ferrand** – the beautifully maintained BackupPC Docker image (`adferrand/backuppc`) that the recovery environment uses directly. It saved me countless hours of tinkering and was the very inspiration to bring all of this into Home Assistant.

Thank you for your work – it runs in production here every single day and has made my backup setup so much easier. This add-on pair is my attempt to build on your work and complement it with a comfortable offsite workflow. I developed it together with **[Claude](https://www.anthropic.com/claude)** (Anthropic) as my pair-programming partner.

---

## Further documentation

- [Offsite Backup – DOCS.md](offsite-backup/DOCS.md) – configuration, dashboard, backup flow, troubleshooting
- [BackupPC Recovery – DOCS.md](backuppc-recovery/DOCS.md) – recovery environment, startup sequence, technical details

## License

MIT – see the add-on labels. BackupPC and the adferrand image are under their own respective licenses.
