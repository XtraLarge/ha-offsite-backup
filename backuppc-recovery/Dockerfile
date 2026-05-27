ARG BUILD_FROM
FROM ${BUILD_FROM}

LABEL \
    io.hass.name="BackupPC Recovery" \
    io.hass.description="BackupPC4 Recovery via Hetzner Storage Box" \
    io.hass.type="addon" \
    io.hass.version="1.0.0"

RUN apt-get update && apt-get install -y --no-install-recommends \
    backuppc \
    apache2 \
    sshfs \
    fuse3 \
    python3 \
    python3-paho-mqtt \
    jq \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Apache auf Port 8900, BackupPC CGI aktivieren
RUN sed -i 's/^Listen 80$/Listen 8900/' /etc/apache2/ports.conf \
    && sed -i 's/<VirtualHost \*:80>/<VirtualHost *:8900>/' /etc/apache2/sites-available/000-default.conf \
    && a2enmod cgi 2>/dev/null || true \
    && a2enconf backuppc 2>/dev/null || true

COPY run.sh /
COPY state.py /

RUN chmod +x /run.sh
RUN mkdir -p /data/logs /data/secrets /mnt/hetzner

EXPOSE 8900

CMD ["/run.sh"]
