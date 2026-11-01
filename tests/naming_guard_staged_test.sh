#!/usr/bin/env bash
# naming_guard_staged_test.sh — Regressionstest fuer den Index-vs-Working-Tree-
# Bypass im Pre-Commit-Hook (Codex-Review auf PR #10, P1).
#
# FEHLERBILD
# naming-guard.sh listete die getrackten Pfade mit "git ls-files", las deren
# Inhalt aber aus dem WORKING TREE. Der Pre-Commit-Hook prueft damit nicht, was
# tatsaechlich committed wird:
#
#     1. host=10.24.3.4 stagen
#     2. im Working Tree auf example.net aendern, NICHT erneut stagen
#     3. committen -> Hook laeuft durch, die sensible Version wird committed
#
# Bei einem OEFFENTLICHEN Repo heisst das: der Wert ist nach dem Push oeffentlich,
# und nur ein History-Rewrite bekommt ihn wieder heraus - veroeffentlicht bleibt
# er trotzdem.
#
# DIESER TEST PRUEFT
#   1. POSITIV:          --staged findet den Leak im Index trotz sauberem Working Tree
#   2. KONTROLL-NEGATIV: der Default-Modus findet ihn NICHT (Test trifft das Fehlerbild)
#   3. ALLOWLIST:        ein nur im Working Tree ergaenzter Allow-Eintrag greift nicht
#   3b. SKIP-WORKTREE:   Eintraege mit skip-worktree-Bit werden mitgeprueft
#   4. HOOK-GUARD:       .githooks/pre-commit ruft den Guard mit --staged auf
#   5. QUELLCODE-GUARD:  keine Lesestelle greift direkt auf den Working Tree zu
#
# Der Test arbeitet in einem EIGENEN Wegwerf-Repo unter $TMPDIR. Er veraendert
# weder Index noch Working Tree des echten Repos.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GUARD="$REPO_ROOT/scripts/naming-guard.sh"
HOOK="$REPO_ROOT/.githooks/pre-commit"
fails=0

for f in "$GUARD" "$HOOK"; do
  if [[ ! -f "$f" ]]; then
    echo "FAIL [setup]            $f nicht gefunden"
    exit 1
  fi
done

# --- Wegwerf-Repo mit dem echten Guard ---------------------------------------
SANDBOX="$(mktemp -d)"
trap 'rm -rf -- "$SANDBOX"' EXIT

mkdir -p "$SANDBOX/scripts"
cp "$GUARD" "$SANDBOX/scripts/naming-guard.sh"
: > "$SANDBOX/.naming-guard-allow"

(
  cd "$SANDBOX" || exit 1
  git init -q .
  git config user.email "test@example.net"
  git config user.name "naming-guard test"
  git add -A
  git commit -q -m "basis"
) >/dev/null 2>&1 || { echo "FAIL [setup] Sandbox-Repo liess sich nicht anlegen"; exit 1; }

# Genau das Szenario aus dem Review: sensibler Wert gestaged, Working Tree sauber.
printf 'host=10.24.3.4\n' > "$SANDBOX/leak.txt"
git -C "$SANDBOX" add leak.txt
printf 'host=example.net\n' > "$SANDBOX/leak.txt"

run_guard() { ( cd "$SANDBOX" && bash scripts/naming-guard.sh "$@" >/dev/null 2>&1 ); echo "$?"; }

# --- 1. POSITIV --------------------------------------------------------------
rc_staged="$(run_guard --staged)"
if [[ "$rc_staged" == "1" ]]; then
  echo "OK   [positiv]          --staged findet den Leak im Index (Working Tree ist sauber)"
else
  echo "FAIL [positiv]          --staged lieferte rc=$rc_staged, erwartet 1"
  echo "     Der gestagete Stand wird nicht geprueft - der Hook laesst den Leak durch."
  fails=$(( fails + 1 ))
fi

# --- 2. KONTROLL-NEGATIV -----------------------------------------------------
# Der Default-Modus MUSS hier durchlaufen. Tut er das nicht, prueft er etwas
# anderes als den Working Tree und der Positivtest beweist nichts mehr.
rc_worktree="$(run_guard)"
if [[ "$rc_worktree" == "0" ]]; then
  echo "OK   [kontroll-negativ] Default-Modus sieht nur den Working Tree — Test ist aussagekraeftig"
else
  echo "FAIL [kontroll-negativ] Default-Modus lieferte rc=$rc_worktree, erwartet 0"
  echo "     Beide Modi verhalten sich gleich; der Positivtest zeigt den Unterschied nicht mehr."
  fails=$(( fails + 1 ))
fi

# --- 3. ALLOWLIST ------------------------------------------------------------
# Die Ausnahme muss aus dem Index kommen, sonst liesse sich der staged-Modus
# mit einer nachtraeglich im Working Tree ergaenzten Regex aushebeln.
printf '^leak\.txt:\n' >> "$SANDBOX/.naming-guard-allow"
rc_allow_unstaged="$(run_guard --staged)"
git -C "$SANDBOX" add .naming-guard-allow
rc_allow_staged="$(run_guard --staged)"

if [[ "$rc_allow_unstaged" == "1" && "$rc_allow_staged" == "0" ]]; then
  echo "OK   [allowlist]        Ausnahme wirkt erst gestaged (ungestaged: rc=1, gestaged: rc=0)"
else
  echo "FAIL [allowlist]        ungestaged rc=$rc_allow_unstaged (erwartet 1), gestaged rc=$rc_allow_staged (erwartet 0)"
  [[ "$rc_allow_unstaged" == "0" ]] && echo "     Eine nur lokal ergaenzte Ausnahme haebelt den staged-Modus aus."
  fails=$(( fails + 1 ))
fi

# --- 3b. SKIP-WORKTREE -------------------------------------------------------
# git checkout-index laesst Eintraege mit gesetztem skip-worktree-Bit still weg,
# obwohl ihr Inhalt im Commit landet. Ohne --ignore-skip-worktree-bits macht ein
# einzelnes "git update-index --skip-worktree <datei>" den Guard blind
# (Codex-Review PR #12).
printf 'host=10.24.3.4\n' > "$SANDBOX/skip.txt"
git -C "$SANDBOX" add skip.txt
git -C "$SANDBOX" update-index --skip-worktree skip.txt
rc_skip="$(run_guard --staged)"
git -C "$SANDBOX" update-index --no-skip-worktree skip.txt
git -C "$SANDBOX" rm -q --cached skip.txt
rm -f "$SANDBOX/skip.txt"

if [[ "$rc_skip" == "1" ]]; then
  echo "OK   [skip-worktree]    Eintraege mit skip-worktree-Bit werden mitgeprueft"
else
  echo "FAIL [skip-worktree]    --staged lieferte rc=$rc_skip, erwartet 1"
  echo "     checkout-index braucht --ignore-skip-worktree-bits - sonst macht ein"
  echo "     einzelnes 'git update-index --skip-worktree <datei>' den Guard blind."
  fails=$(( fails + 1 ))
fi

# --- 4. HOOK-GUARD -----------------------------------------------------------
if grep -qE '^[[:space:]]*if[[:space:]]+bash[[:space:]]+"\$guard"[[:space:]]+--staged;' "$HOOK"; then
  echo "OK   [hook]             pre-commit ruft den Guard mit --staged auf"
else
  echo "FAIL [hook]             pre-commit ruft den Guard OHNE --staged auf"
  echo "     Erwartet:  if bash \"\$guard\" --staged; then"
  echo "     Gefunden:  $(grep -nE 'bash[[:space:]]+"\$guard"' "$HOOK" || echo '(kein Aufruf gefunden)')"
  fails=$(( fails + 1 ))
fi

# --- 5. QUELLCODE-GUARD ------------------------------------------------------
# Jede Lesestelle muss ueber list_files laufen. Ein direkter git-ls-files-Zugriff
# liest wieder den Working Tree und stellt den Bypass still wieder her.
direct="$(grep -nE 'git ls-files -z[[:space:]]*\|[[:space:]]*xargs|< <\(git ls-files -z\)' "$GUARD" || true)"
if [[ -z "$direct" ]]; then
  echo "OK   [quellcode]        alle Lesestellen laufen ueber list_files"
else
  echo "FAIL [quellcode]        Lesestelle greift direkt auf den Working Tree zu:"
  echo "$direct" | sed 's/^/                        /'
  echo "                        Stattdessen: list_files | ...   (respektiert den Modus)"
  fails=$(( fails + 1 ))
fi

echo
if (( fails == 0 )); then
  echo "naming_guard_staged_test: BESTANDEN"
  exit 0
fi
echo "naming_guard_staged_test: $fails FEHLER"
exit 1
