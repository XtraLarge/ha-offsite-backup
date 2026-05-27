#!/usr/bin/env bash
# Liest LOG_FILE, anonymisiert Credentials und sendet an Loki.
LOKI_URL="${LOKI_URL:-$(jq -r '.loki_url // ""' /data/options.json 2>/dev/null)}"
LOG_FILE="${1:-/data/logs/backup.log}"
JOB_STATUS="${2:-unknown}"

[[ -f "$LOG_FILE" ]]           || exit 0
[[ -n "$LOKI_URL" ]]           || exit 0
command -v curl   >/dev/null   || exit 0
command -v python3 >/dev/null  || exit 0

PAYLOAD=$(python3 <<PYEOF
import json, time, re, sys

log_file   = "$LOG_FILE"
job_status = "$JOB_STATUS"

try:
    with open(log_file) as f:
        raw = f.read()
except Exception:
    sys.exit(0)

raw = re.sub(r'u\d{6,}\.your-storagebox\.de', 'storagebox.hetzner', raw)
raw = re.sub(r'u\d{6,}@',                     'storageuser@',       raw)
raw = re.sub(r'\bu\d{6,}\b',                  'storageuser',        raw)
raw = re.sub(r'Bearer [A-Za-z0-9._-]+',        'Bearer [REDACTED]',  raw)
raw = re.sub(r'token=[A-Za-z0-9._-]+',         'token=[REDACTED]',   raw)

lines = [l for l in raw.splitlines() if l.strip()]
if not lines:
    sys.exit(0)

base_ns = int(time.time() * 1e9)
values  = [[str(base_ns + i), line] for i, line in enumerate(lines)]

print(json.dumps({
    "streams": [{
        "stream": {"job": "rsync-backup", "host": "ha-offsite-backup", "status": job_status},
        "values": values
    }]
}))
PYEOF
)

[[ -n "$PAYLOAD" ]] || exit 0

http_code=$(curl -s -o /dev/null -w "%{http_code}" \
  -X POST "$LOKI_URL" \
  -H "Content-Type: application/json" \
  --connect-timeout 10 \
  -d "$PAYLOAD")

if [[ "$http_code" =~ ^2 ]]; then
  echo "$(date '+%F %T'): Loki-Log gesendet (status=$JOB_STATUS)"
else
  echo "$(date '+%F %T'): Loki-Push fehlgeschlagen HTTP $http_code (nicht kritisch)"
fi
