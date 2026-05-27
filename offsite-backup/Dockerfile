ARG BUILD_FROM
FROM ${BUILD_FROM}

LABEL \
    io.hass.name="Offsite Backup" \
    io.hass.description="rsync-basierter Offsite Backup via Hetzner Storage Box mit BackupPC-Recovery" \
    io.hass.type="addon" \
    io.hass.version="1.0.0"

RUN apt-get update && apt-get install -y --no-install-recommends \
    rsync \
    openssh-client \
    curl \
    python3 \
    python3-paho-mqtt \
    python3-croniter \
    jq \
    cron \
    sshfs \
    fuse3 \
    docker.io \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /data/logs /data/secrets /data/backuppc-recovery

COPY run.sh /
COPY api.py /
COPY scripts/ /scripts/

RUN chmod +x /run.sh /scripts/*.sh

EXPOSE 8099

CMD ["/run.sh"]
