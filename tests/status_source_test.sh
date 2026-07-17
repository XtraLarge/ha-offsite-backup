#!/usr/bin/env bash
# status_source_test.sh — Regressionsguard fuer die EINZIGE autoritative
# Erfolgsstatus-Quelle (Wissen #744/#751).
#
# Der HA-seitige Launcher backup.sh ist nur Anstoesser/Mitschreiber; sein
# Exit-Code ist ein PROXY und darf NICHT den Erfolgsstatus schreiben (sonst
# False-Positive "failed"). Autoritativ ist api.py:_finalize_from_nas +
# Recovery-Smoke. Dieser Test verhindert, dass die Proxy-Schreibstelle
# versehentlich wieder eingebaut wird.
set -euo pipefail
cd -- "$(git rev-parse --show-toplevel 2>/dev/null || dirname "$(dirname "$0")")"

BSH="offsite-backup/scripts/backup.sh"
API="offsite-backup/api.py"
fail=0

# 1) backup.sh darf status.json / STATUS_FILE NICHT (mehr) schreiben.
if grep -Eq 'STATUS_FILE|status\.json' "$BSH"; then
  echo "FAIL: $BSH referenziert wieder STATUS_FILE/status.json (Proxy-Statusquelle):" >&2
  grep -nE 'STATUS_FILE|status\.json' "$BSH" >&2
  fail=1
else
  echo "OK: $BSH schreibt keinen Status mehr (Proxy-Quelle entfernt)."
fi

# 2) api.py schreibt den Abschluss-Status ueber die zentrale Funktion.
if grep -q 'def _write_final_status' "$API" && grep -q '_write_final_status(' "$API"; then
  echo "OK: api.py besitzt die zentrale Status-Schreibstelle _write_final_status."
else
  echo "FAIL: api.py:_write_final_status fehlt/ungenutzt." >&2
  fail=1
fi

# 3) Der Erfolgsstatus haengt am Smoke-Ergebnis (nicht mehr blind an rc=0).
if grep -q '_smoke_and_finalize' "$API"; then
  echo "OK: Erfolgsstatus wird ueber den Recovery-Smoke bestimmt."
else
  echo "FAIL: Smoke-gesteuerte Finalisierung (_smoke_and_finalize) fehlt." >&2
  fail=1
fi

[[ "$fail" -eq 0 ]] && echo "PASS: status_source_test" || { echo "status_source_test FEHLGESCHLAGEN" >&2; exit 1; }
