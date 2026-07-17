ARG BUILD_FROM
FROM ${BUILD_FROM}

LABEL \
    io.hass.name="BackupPC Recovery" \
    io.hass.description="BackupPC4 Recovery via Hetzner Storage Box" \
    io.hass.type="addon" \
    io.hass.version="2.0.0"

# Zusätzliche Pakete auf Basis des adferrand-Images (Alpine)
RUN apk add --no-cache \
    sshfs \
    fuse3 \
    jq \
    python3 \
    py3-pip \
    && pip3 install --break-system-packages paho-mqtt 2>/dev/null \
    || pip3 install paho-mqtt

COPY run.sh /
COPY state.py /
COPY smoke.py /

RUN chmod +x /run.sh \
    && mkdir -p /data/logs /data/secrets /mnt/hetzner

EXPOSE 8080

ENTRYPOINT ["/run.sh"]
