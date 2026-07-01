#!/usr/bin/env bash
# naming-guard.sh — Leak-Guard fuer dieses OEFFENTLICHE Repo.
# Prueft NUR git-getrackte Dateien (genau das, was tatsaechlich ins oeffentliche
# origin gelangt) auf drei Leak-Klassen:
#   1) interne Netz-/Hostnamen DIESER Umgebung,
#   2) ECHTE private SSH-/PEM-Keys (Header + realer base64-Body),
#   3) Klartext-Secret-Zuweisungen (Variablen-/Env-Referenzen ausgenommen).
# Exit 1 + Trefferliste bei Fund, sonst 0.
#
# Bewusst NICHT erfasst (generische Doku-Platzhalter — KEIN Leak):
#   - RFC1918-Beispiele 192.168.x / 172.16-31.x / 10.0.0.x
#   - Platzhalter-Hostnamen (*.example.*), truncierte Public-Keys (ssh-ed25519 AAAA...)
#   - PEM-Header OHNE echten base64-Body (Doku-Codebloecke mit "...")
#   - Secret-Zuweisungen auf Variablen/Env-Refs ($VAR, os.environ, getenv, process.env)
#
# Ausnahmen (bewusst, dokumentiert):
#   - Inline:  am Zeilenende den Marker  naming-guard:allow  setzen.
#   - Regex:   eine erweiterte Regex je Zeile in .naming-guard-allow ablegen
#              (matcht gegen 'datei:zeile:inhalt'); '#'-Kommentare/Leerzeilen erlaubt.
set -euo pipefail

cd -- "$(git rev-parse --show-toplevel)"

ALLOW_FILE='.naming-guard-allow'
SELF='scripts/naming-guard.sh'

# 1) Interne Netz-/Hostnamen: 10.<VLAN>.x.x (2. Oktett nonzero) + Exposed-Net
#    10.0.<nonzero>.x + interne Domains. Generische RFC1918-Beispiele
#    (192.168 / 172.16-31 / 10.0.0.x) bleiben bewusst UNERFASST.
NET_PATTERN='10\.[1-9][0-9]?\.[0-9]{1,3}\.[0-9]{1,3}|10\.0\.[1-9][0-9]{0,2}\.[0-9]{1,3}|fritz\.box|derwerres\.de'

# 2) Klartext-Secret-Zuweisung mit Literalwert (>=12 Zeichen). Nur '='-Zuweisung
#    (YAML-Schema 'key: type' bleibt aussen vor). Variablen-/Env-Refs ausgenommen.
SECRET_PATTERN='(password|passwd|secret|token|api[_-]?key)[[:space:]]*=[[:space:]]*["'\'']?[A-Za-z0-9+/._-]{12,}'
SECRET_EXCLUDE='=[[:space:]]*["'\'']?(\$|os\.(environ|getenv)|getenv|System\.getenv|process\.env)'

allow_filter() {
  if [[ -s "$ALLOW_FILE" ]]; then
    grep -vEf <(grep -vE '^[[:space:]]*(#|$)' "$ALLOW_FILE")
  else
    cat
  fi
}
common_filter() {
  grep -v 'naming-guard:allow' \
    | grep -v "^${ALLOW_FILE}:" \
    | grep -v "^${SELF}:" \
    | allow_filter
}

net_hits="$(git ls-files -z | xargs -0 grep -HnIE "$NET_PATTERN" -- 2>/dev/null | common_filter || true)"
secret_hits="$(git ls-files -z | xargs -0 grep -HnIE "$SECRET_PATTERN" -- 2>/dev/null | grep -vE "$SECRET_EXCLUDE" | common_filter || true)"

# 3) ECHTE private Keys: PEM-Header + realer base64-Body (>=40 Zeichen, ohne "...")
#    NACH dem Header, im selben File. Doku-Platzhalter (Body mit "..." oder <40)
#    werden ignoriert.
key_hits="$(
  while IFS= read -r -d '' f; do
    awk -v F="$f" '
      /-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----/ && !have_hdr { hdr=NR; hdrline=$0; have_hdr=1; next }
      have_hdr && $0 !~ /\.\.\./ && $0 ~ /^[[:space:]]*[A-Za-z0-9+\/]{40,}={0,2}[[:space:]]*$/ { body=1 }
      END { if (have_hdr && body) printf "%s:%d:%s\n", F, hdr, hdrline }
    ' "$f" 2>/dev/null
  done < <(git ls-files -z) | common_filter || true
)"

hits=""
[[ -n "$net_hits" ]]    && hits+="[interne Netz-/Hostnamen]"$'\n'"$net_hits"$'\n'
[[ -n "$key_hits" ]]    && hits+="[echte private Keys]"$'\n'"$key_hits"$'\n'
[[ -n "$secret_hits" ]] && hits+="[Klartext-Secrets]"$'\n'"$secret_hits"$'\n'

if [[ -n "$hits" ]]; then
  {
    echo "naming-guard: moegliches Leak in getrackten Dateien gefunden"
    echo "             (OEFFENTLICHES Repo — interne Werte/Keys/Secrets gehoeren nicht hierher):"
    echo
    printf '%s' "$hits"
    echo
    echo "Behebung: Wert durch generischen Platzhalter ersetzen (example.net / <host>),"
    echo "          echte Secrets/Keys rotieren und aus dem Repo entfernen."
    echo "Bewusste Ausnahme: Zeilen-Marker  naming-guard:allow  oder Regex in ${ALLOW_FILE}."
  } >&2
  exit 1
fi

echo "naming-guard: ok — keine internen Netz-/Hostnamen, Keys oder Klartext-Secrets in getrackten Dateien."
