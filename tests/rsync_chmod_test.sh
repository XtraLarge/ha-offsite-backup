#!/usr/bin/env bash
# rsync_chmod_test.sh — Regressionstest fuer die S_IXOTH-Haertung des Offsite-
# rsync (Wissen #747/#748).
#
# BackupPC-4-Pool-Dateien tragen transient das other-execute-Bit (S_IXOTH ->
# Mode 0445 "pending delete"), das im Normalbetrieb staendig wechselt. Der
# Offsite-Sync des Pools laeuft mit -p (perms) + -W (whole-file). Ohne Haertung
# erzeugt jeder Marker-Wechsel einen Perm-Diff -> CoW-Churn auf der Hetzner-Box.
#
# Die Haertung ist --chmod=Fo-x (nur Dateien, nur das o-x-Bit maskieren): die
# Ziel-Kopie bekommt IMMER den kanonischen Mode 0444, unabhaengig vom transienten
# Quell-Marker. Dieser Test beweist:
#   POSITIV: mit --chmod=Fo-x landet 0445 als 0444; ein Marker-Flip 0444<->0445
#            erzeugt KEINE Aenderung mehr (leere Itemize-Ausgabe).
#   KONTROLL-NEGATIV: OHNE die Haertung erzeugt derselbe Flip einen Perm-Diff
#            (Itemize zeigt eine *p-Aenderung) — d.h. der Test wuerde ohne die
#            Haertung fehlschlagen, ist also aussagekraeftig.
set -euo pipefail

command -v rsync >/dev/null 2>&1 || { echo "SKIP: rsync nicht verfuegbar"; exit 0; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
SRC="$TMP/src"; DST="$TMP/dst"; DST2="$TMP/dst2"
mkdir -p "$SRC"
# Repraesentative Pool-Datei mit gesetztem pending-delete-Marker (0445) ...
printf 'poolcontent' > "$SRC/marked"; chmod 0445 "$SRC/marked"
# ... und eine ohne Marker (0444).
printf 'plaincontent' > "$SRC/plain"; chmod 0444 "$SRC/plain"

# Dieselben Kern-Optionen wie der echte Pool-Sync (ohne Netz/SSH).
BASE_OPTS=(-aHAX -W --numeric-ids)
HARDEN=(--chmod=Fo-x)

fail() { echo "FAIL: $*" >&2; exit 1; }

# ---- POSITIV: mit Haertung ----
rsync "${BASE_OPTS[@]}" "${HARDEN[@]}" "$SRC/" "$DST/"
m=$(stat -c '%a' "$DST/marked")
[[ "$m" == "444" ]] || fail "marked sollte offsite 0444 sein, ist $m"
p=$(stat -c '%a' "$DST/plain")
[[ "$p" == "444" ]] || fail "plain sollte 0444 sein, ist $p"

# Marker-Flip an der Quelle: 0445 -> 0444 (Normalbetrieb entmarkiert die Datei).
chmod 0444 "$SRC/marked"
out=$(rsync "${BASE_OPTS[@]}" "${HARDEN[@]}" -i "$SRC/" "$DST/" | grep -vE '^\.d|/$' || true)
[[ -z "$out" ]] || fail "Entmarkierung 0445->0444 darf mit Haertung KEINE Aenderung erzeugen, war:\n$out"

# Und wieder markieren: 0444 -> 0445 (naechster Loeschkandidat).
chmod 0445 "$SRC/marked"
out=$(rsync "${BASE_OPTS[@]}" "${HARDEN[@]}" -i "$SRC/" "$DST/" | grep -vE '^\.d|/$' || true)
[[ -z "$out" ]] || fail "Markierung 0444->0445 darf mit Haertung KEINE Aenderung erzeugen, war:\n$out"
m=$(stat -c '%a' "$DST/marked")
[[ "$m" == "444" ]] || fail "marked muss auf der Kopie kanonisch 0444 bleiben, ist $m"

# ---- KONTROLL-NEGATIV: OHNE Haertung fuehrt derselbe Flip zu einem Perm-Diff ----
chmod 0444 "$SRC/marked"
rsync "${BASE_OPTS[@]}" "$SRC/" "$DST2/"   # Basis-Kopie (0444)
chmod 0445 "$SRC/marked"                    # Marker-Flip
out=$(rsync "${BASE_OPTS[@]}" -i "$SRC/" "$DST2/" | grep '^\.f' || true)
echo "$out" | grep -q 'p' \
  || fail "Kontrolle: OHNE Haertung MUSS ein Marker-Flip einen Perm-Diff zeigen (Itemize .f...p...), war leer -> Test nicht aussagekraeftig"

echo "PASS: rsync_chmod_test — S_IXOTH-Haertung maskiert den transienten Marker, Kontrolle bestaetigt Aussagekraft."
