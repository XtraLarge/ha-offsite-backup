#!/usr/bin/env bash
# pipefail_sigpipe_test.sh — Regressionstest fuer SIGPIPE-Abbrueche unter
# "set -o pipefail" (Vorfall 2026-10-31, BackupPC Recovery 2.2.1).
#
# FEHLERBILD
# backuppc-recovery/run.sh (set -euo pipefail) ermittelte das juengste
# Host-Verzeichnis mit
#     NEWEST_HOST=$(ls -dt "$PC_DIR"/*/ 2>/dev/null | head -1)
# head beendet sich nach der ersten Zeile und schliesst die Pipe. Schreibt ls
# danach noch, bekommt es SIGPIPE; unter pipefail wird der Pipeline-Status 141,
# und set -e beendet das Skript. Ob das passiert, haengt von Ausgabegroesse und
# Timing ab (SSHFS) — der Start lief am 2026-10-24 durch, am 2026-10-31 nach
# einem Reboot nicht:
#     App BackupPC Recovery exited with non-zero exit code 141
#
# GLEICHE FEHLERKLASSE
# Jeder Leser, der vor dem Ende der Eingabe aufhoert (head, grep -q, grep -m),
# macht eine Pipe unter pipefail nicht-deterministisch. In einem if wird so ein
# Treffer faelschlich zu "falsch" (backup.sh: printf | grep -q).
#
# DIESER TEST PRUEFT
#   1. POSITIV:          die neue Schleife findet das juengste Verzeichnis,
#                        auch bei tausenden Eintraegen, ohne Abbruch
#   2. KONTROLL-NEGATIV: die alte Form bricht bei grosser Ausgabe mit 141 ab
#                        (beweist, dass der Test das Fehlerbild abdeckt)
#   3. QUELLCODE-GUARD:  keine vorzeitig endenden Leser in den betroffenen
#                        pipefail-Skripten
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RECOVERY_RUN="$REPO_ROOT/backuppc-recovery/run.sh"
BACKUP_SH="$REPO_ROOT/offsite-backup/scripts/backup.sh"
fails=0

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Viele Verzeichnisse mit langen Namen: die ls-Ausgabe liegt deutlich ueber
# dem Pipe-Puffer (64 KiB), damit SIGPIPE im Negativ-Fall sicher eintritt.
PC_DIR="$WORK/pc"
mkdir -p "$PC_DIR"
for i in $(seq -w 1 3000); do
  mkdir "$PC_DIR/host-$i-padding-padding-padding-padding-padding"
done
touch -d '2020-01-01' "$PC_DIR"/*/
newest="$PC_DIR/host-1500-padding-padding-padding-padding-padding/"
touch -d '2030-01-01' "$newest"

# --- 1. POSITIV -------------------------------------------------------------
positive="$(
  PC_DIR="$PC_DIR" bash -c '
    set -euo pipefail
    NEWEST_HOST=""
    for host_dir in "$PC_DIR"/*/; do
      [[ -d "$host_dir" ]] || continue
      if [[ -z "$NEWEST_HOST" || "$host_dir" -nt "$NEWEST_HOST" ]]; then
        NEWEST_HOST="$host_dir"
      fi
    done
    echo "$NEWEST_HOST"
  ' 2>/dev/null || echo "ABBRUCH"
)"
if [[ "$positive" == "$newest" ]]; then
  echo "OK   1 positiv: juengstes Verzeichnis gefunden"
else
  echo "FAIL 1 positiv: erwartet '$newest', erhalten '$positive'"
  fails=$((fails + 1))
fi

# --- 2. KONTROLL-NEGATIV ----------------------------------------------------
rc=0
PC_DIR="$PC_DIR" bash -c '
  set -euo pipefail
  NEWEST_HOST=$(ls -dt "$PC_DIR"/*/ 2>/dev/null | head -1)
  echo "$NEWEST_HOST" >/dev/null
' 2>/dev/null || rc=$?
if [[ "$rc" == "141" ]]; then
  echo "OK   2 kontroll-negativ: alte Form bricht mit rc=141 ab"
else
  echo "FAIL 2 kontroll-negativ: alte Form lieferte rc=$rc statt 141"
  fails=$((fails + 1))
fi

# --- 3. QUELLCODE-GUARD -----------------------------------------------------
# run.sh laeuft komplett unter pipefail: dort gar keine vorzeitigen Leser.
if grep -n -E '\|[[:space:]]*(head|grep[[:space:]]+(-[a-zA-Z]*[qm]))' "$RECOVERY_RUN" \
     | grep -v -E '^[0-9]+:[[:space:]]*#'; then
  echo "FAIL 3a guard: vorzeitig endender Leser in backuppc-recovery/run.sh"
  fails=$((fails + 1))
else
  echo "OK   3a guard: backuppc-recovery/run.sh sauber"
fi
# backup.sh: der LAUNCHER-Heredoc laeuft remote unter pipefail.
if grep -n -E 'printf .*\|[[:space:]]*grep[[:space:]]+-q' "$BACKUP_SH" \
     | grep -v -E '^[0-9]+:[[:space:]]*#'; then
  echo "FAIL 3b guard: printf | grep -q in offsite-backup/scripts/backup.sh"
  fails=$((fails + 1))
else
  echo "OK   3b guard: offsite-backup/scripts/backup.sh sauber"
fi

if (( fails > 0 )); then
  echo "$fails Pruefung(en) fehlgeschlagen"
  exit 1
fi
echo "Alle Pruefungen bestanden"
