FROM debian:trixie-slim

RUN apt-get update -qq && \
    apt-get install -y --no-install-recommends curl gnupg ca-certificates && \
    curl -fsSL https://enterprise.proxmox.com/debian/proxmox-release-trixie.gpg \
        -o /etc/apt/trusted.gpg.d/proxmox-release-trixie.gpg && \
    echo 'deb http://download.proxmox.com/debian/pbs trixie pbs-no-subscription' \
        > /etc/apt/sources.list.d/pbs.list && \
    apt-get update -qq && \
    apt-get install -y --no-install-recommends \
        proxmox-backup-server \
        proxmox-backup-client \
        rsync \
        python3 && \
    rm -rf /var/lib/apt/lists/*

# PBS braucht /run/proxmox-backup (backup:backup)
RUN mkdir -p /run/proxmox-backup && \
    chown backup:backup /run/proxmox-backup

EXPOSE 8007
