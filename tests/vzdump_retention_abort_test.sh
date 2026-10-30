#!/usr/bin/env bash
# vzdump_retention_abort_test.sh — Regressionstest fuer den Abbruch der
# vzdump-Retention (Vorfall 2026-09-25 bis 2026-10-30).
#
# FEHLERBILD
# In prune_vzdump_files() stand als eigenstaendiges Kommando:
#     (( i++ ))
# Die Post-Inkrement-Form liefert den ALTEN Wert als arithmetisches Ergebnis.
# Ist dieser 0, wertet bash das Kommando als "falsch" und setzt den Exitstatus
# auf 1. Unter "set -e" beendet das den gesamten Lauf — und zwar in der ERSTEN
# Schleifeniteration, also genau nach der ersten "loesche"-Zeile.
#
# FOLGE
# backup_nas.sh starb nach dem rsync, bevor create_storagebox_snapshot lief.
# Fuenf Wochen lang entstand kein Offsite-Snapshot mehr, obwohl die Daten
# synchronisiert wurden. Der Lauf meldete nur ein generisches rc=1.
#
# DIESER TEST PRUEFT DREI DINGE
#   1. POSITIV:          die heutige Form i=$(( i + 1 )) laeuft komplett durch
#   2. KONTROLL-NEGATIV: die alte Form (( i++ )) bricht nach 1 Durchlauf ab
#                        (beweist, dass der Test das echte Fehlerbild trifft)
#   3. QUELLCODE-GUARD:  im ausgelieferten Skript darf keine eigenstaendige
#                        (( x++ ))/(( x-- ))-Form mehr auftauchen
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="$REPO_ROOT/offsite-backup/scripts/backup_nas.sh"
ITEMS=13
fails=0

# --- 1. POSITIV -------------------------------------------------------------
# Exakt die Schleifenform aus prune_vzdump_files, mit der korrigierten Zeile.
positive="$(
  bash -c '
    set -euo pipefail
    files=$(seq 1 '"$ITEMS"')
    to_delete='"$ITEMS"'
    i=0
    while IFS= read -r fname; do
      (( i >= to_delete )) && break
      [[ -z "$fname" ]] && continue
      echo "loesche $fname"
      i=$(( i + 1 ))
    done <<< "$files"
  ' 2>/dev/null | grep -c '^loesche '
)"

if [[ "$positive" == "$ITEMS" ]]; then
  echo "OK   [positiv]          i=\$(( i + 1 )): alle $ITEMS Eintraege verarbeitet"
else
  echo "FAIL [positiv]          erwartet $ITEMS verarbeitete Eintraege, erhalten $positive"
  fails=$(( fails + 1 ))
fi

# --- 2. KONTROLL-NEGATIV ----------------------------------------------------
# Dieselbe Schleife mit der alten Zeile muss nach genau 1 Eintrag abbrechen.
negative="$(
  bash -c '
    set -euo pipefail
    files=$(seq 1 '"$ITEMS"')
    to_delete='"$ITEMS"'
    i=0
    while IFS= read -r fname; do
      (( i >= to_delete )) && break
      [[ -z "$fname" ]] && continue
      echo "loesche $fname"
      (( i++ ))
    done <<< "$files"
  ' 2>/dev/null | grep -c '^loesche '
)"

if [[ "$negative" == "1" ]]; then
  echo "OK   [kontroll-negativ] (( i++ )) bricht wie erwartet nach 1 Eintrag ab — Test ist aussagekraeftig"
else
  echo "FAIL [kontroll-negativ] erwartet Abbruch nach 1 Eintrag, erhalten $negative"
  echo "     Der Test trifft das echte Fehlerbild nicht mehr (andere bash-Version?) und schuetzt nicht."
  fails=$(( fails + 1 ))
fi

# --- 3. QUELLCODE-GUARD -----------------------------------------------------
# Verhindert, dass das Muster an anderer Stelle zurueckkehrt (Fix-the-Class).
if [[ ! -f "$TARGET" ]]; then
  echo "FAIL [guard]            $TARGET nicht gefunden"
  fails=$(( fails + 1 ))
else
  hits="$(grep -nE '^[[:space:]]*\(\([[:space:]]*[A-Za-z_][A-Za-z0-9_]*[[:space:]]*(\+\+|--)[[:space:]]*\)\)[[:space:]]*$' "$TARGET" || true)"
  if [[ -z "$hits" ]]; then
    echo "OK   [guard]            keine eigenstaendige (( x++ ))/(( x-- ))-Form in backup_nas.sh"
  else
    echo "FAIL [guard]            eigenstaendige Inkrement-Form gefunden — unter set -e bricht das den Lauf ab:"
    echo "$hits" | sed 's/^/                        /'
    echo "                        Stattdessen: x=\$(( x + 1 ))"
    fails=$(( fails + 1 ))
  fi
fi

echo
if (( fails == 0 )); then
  echo "vzdump_retention_abort_test: BESTANDEN"
  exit 0
fi
echo "vzdump_retention_abort_test: $fails FEHLER"
exit 1
