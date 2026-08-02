#!/usr/bin/env bash
# rsync_mkpath_test.sh — Regressionstest fuer die --mkpath-Absicherung des
# Offsite-rsync (#1054).
#
# Neue Offsite-Quellen koennen verschachtelte Ziel-dests haben, deren
# Eltern-Pfad auf der Hetzner-Box noch nicht existiert (z. B. ZPool/VMGuest,
# ZPool/PBS/NAS: /home/ZPool existiert, /home/ZPool/VMGuest bzw. /home/ZPool/PBS
# nicht). Ohne --mkpath legt rsync verschachtelte Eltern-Pfade NICHT an und
# scheitert mit: mkdir "<dest>" failed: No such file or directory (code 11).
#
# --mkpath (rsync >=3.2.3) erzeugt die fehlenden Pfadkomponenten. Dieser Test
# beweist lokal (ohne Netz/SSH) genau das reale Fehlerbild:
#   POSITIV:  mit --mkpath gelingt der Sync in einen tief verschachtelten,
#             nicht existierenden dest; die Dateien landen korrekt.
#   KONTROLL-NEGATIV: OHNE --mkpath scheitert derselbe Sync mit rc!=0 und
#             "No such file or directory" — d.h. der Test ist aussagekraeftig
#             (er wuerde ohne die Absicherung fehlschlagen).
set -euo pipefail

command -v rsync >/dev/null 2>&1 || { echo "SKIP: rsync nicht verfuegbar"; exit 0; }

# --mkpath gibt es erst ab rsync 3.2.3.
if ! rsync --help 2>&1 | grep -q -- '--mkpath'; then
  echo "SKIP: rsync ohne --mkpath (<3.2.3)"; exit 0
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
SRC="$TMP/src"
mkdir -p "$SRC/VMBackup"
printf 'guestdata' > "$SRC/VMBackup/disk.img"

BASE_OPTS=(-aHAX -W --numeric-ids)

fail() { echo "FAIL: $*" >&2; exit 1; }

# ---- POSITIV: mit --mkpath ----
# Ziel-Elternpfad .../ZPool/VMGuest existiert NICHT (nur TMP existiert).
DST_OK="$TMP/box/home/ZPool/VMGuest/"
rsync "${BASE_OPTS[@]}" --mkpath "$SRC/" "$DST_OK" \
  || fail "mit --mkpath muss der Sync in einen verschachtelten neuen dest gelingen"
[[ -f "$DST_OK/VMBackup/disk.img" ]] \
  || fail "Inhalt muss unter dem angelegten Pfad liegen ($DST_OK/VMBackup/disk.img)"
[[ "$(cat "$DST_OK/VMBackup/disk.img")" == "guestdata" ]] \
  || fail "Inhalt der uebertragenen Datei stimmt nicht"

# ---- KONTROLL-NEGATIV: OHNE --mkpath ----
DST_BAD="$TMP/box2/home/ZPool/VMGuest/"
set +e
err="$(rsync "${BASE_OPTS[@]}" "$SRC/" "$DST_BAD" 2>&1)"
rc=$?
set -e
[[ "$rc" -ne 0 ]] \
  || fail "Kontrolle: OHNE --mkpath MUSS der Sync in einen verschachtelten neuen dest fehlschlagen (Test sonst nicht aussagekraeftig)"
echo "$err" | grep -qiE 'No such file or directory|mkdir' \
  || fail "Kontrolle: erwartetes Fehlerbild (No such file or directory/mkdir) fehlt, war:\n$err"

echo "PASS: rsync_mkpath_test — --mkpath legt verschachtelte dest-Elternpfade an, Kontrolle bestaetigt Aussagekraft."
