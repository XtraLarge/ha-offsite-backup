#!/usr/bin/env bash
# subshell_httpcode_test.sh — Regressionstest fuer den Subshell-Rueckgabefehler
# in hetzner_api() (Vorfall 2026-10-30, eingefuehrt mit v1.14.1).
#
# FEHLERBILD
# hetzner_api() setzte HTTP_CODE und gab den Body auf stdout aus. Aufgerufen
# wurde sie in einer Command Substitution:
#     body="$(hetzner_api GET ...)"
#     if [[ "$HTTP_CODE" != "200" ]]; then
# Eine Command Substitution laeuft in einer SUBSHELL: die Zuweisung an
# HTTP_CODE ist im Aufrufer nie sichtbar. Unter "set -u" bricht der folgende
# Lesezugriff den Lauf ab:
#     backup_nas.sh: Zeile 418: HTTP_CODE ist nicht gesetzt.
#
# FOLGE
# Der Abbruch kam NACH dem erfolgreichen POST — der Offsite-Snapshot entstand,
# der Lauf meldete trotzdem rc=1. Ein erfolgreiches Backup sah aus wie ein
# Fehlschlag, und der naechste Schritt (reset_ssh_master, sauberer Exit-Code)
# lief nicht mehr.
#
# DIESER TEST PRUEFT
#   1. POSITIV:          Rueckgabe ueber globale Variablen ueberlebt den Aufruf
#   2. KONTROLL-NEGATIV: die alte Form bricht unter set -u ab
#   3. QUELLCODE-GUARD:  kein hetzner_api-Aufruf in einer Command Substitution
#   4. INIT-GUARD:       HTTP_CODE/HTTP_BODY sind vorinitialisiert
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="$REPO_ROOT/offsite-backup/scripts/backup_nas.sh"
fails=0

# --- 1. POSITIV -------------------------------------------------------------
positive="$(
  bash -c '
    set -euo pipefail
    HTTP_CODE="000"; HTTP_BODY=""
    api() { HTTP_CODE="200"; HTTP_BODY="{\"ok\":true}"; return 0; }
    api GET /x
    echo "code=$HTTP_CODE"
  ' 2>/dev/null || echo "ABBRUCH"
)"

if [[ "$positive" == "code=200" ]]; then
  echo "OK   [positiv]          globale Rueckgabe ueberlebt den Funktionsaufruf"
else
  echo "FAIL [positiv]          erwartet 'code=200', erhalten '$positive'"
  fails=$(( fails + 1 ))
fi

# --- 2. KONTROLL-NEGATIV ----------------------------------------------------
# Exakt die alte Form: Funktion in $( ), danach HTTP_CODE lesen. Unter set -u
# muss das abbrechen - sonst trifft der Test das echte Fehlerbild nicht.
negative="$(
  bash -c '
    set -euo pipefail
    api() { HTTP_CODE="200"; printf "%s" "{\"ok\":true}"; }
    body="$(api GET /x)"
    echo "code=$HTTP_CODE"
  ' 2>/dev/null || echo "ABBRUCH"
)"

if [[ "$negative" == "ABBRUCH" ]]; then
  echo "OK   [kontroll-negativ] \$( )-Form bricht unter set -u ab — Test ist aussagekraeftig"
else
  echo "FAIL [kontroll-negativ] erwartet 'ABBRUCH', erhalten '$negative'"
  echo "     Der Test trifft das echte Fehlerbild nicht mehr und schuetzt nicht."
  fails=$(( fails + 1 ))
fi

# --- 3. QUELLCODE-GUARD -----------------------------------------------------
if [[ ! -f "$TARGET" ]]; then
  echo "FAIL [guard]            $TARGET nicht gefunden"
  fails=$(( fails + 1 ))
else
  # Kommentarzeilen ausnehmen: die Doku zeigt die falsche Form bewusst.
  hits="$(grep -nE '\$\(hetzner_api' "$TARGET" | grep -vE '^[0-9]+:[[:space:]]*#' || true)"
  if [[ -z "$hits" ]]; then
    echo "OK   [guard]            kein hetzner_api-Aufruf in einer Command Substitution"
  else
    echo "FAIL [guard]            hetzner_api in Command Substitution — HTTP_CODE geht verloren:"
    echo "$hits" | sed 's/^/                        /'
    echo "                        Stattdessen: hetzner_api GET \"/pfad\"  und danach \$HTTP_CODE / \$HTTP_BODY lesen"
    fails=$(( fails + 1 ))
  fi

  # --- 4. INIT-GUARD --------------------------------------------------------
  missing=""
  grep -qE '^HTTP_CODE=' "$TARGET" || missing="$missing HTTP_CODE"
  grep -qE '^HTTP_BODY=' "$TARGET" || missing="$missing HTTP_BODY"
  if [[ -z "$missing" ]]; then
    echo "OK   [init]             HTTP_CODE und HTTP_BODY sind vorinitialisiert (set -u sicher)"
  else
    echo "FAIL [init]             nicht vorinitialisiert:$missing — unter set -u bricht ein Lesezugriff ab"
    fails=$(( fails + 1 ))
  fi
fi

echo
if (( fails == 0 )); then
  echo "subshell_httpcode_test: BESTANDEN"
  exit 0
fi
echo "subshell_httpcode_test: $fails FEHLER"
exit 1
