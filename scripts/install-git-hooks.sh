#!/usr/bin/env bash
# Aktiviert die im Repo mitgelieferten Git-Hooks aus .githooks/.
#
# Git-Hooks werden nicht mitgecheckt-out und nicht automatisch aktiv. Ohne
# diesen einen Befehl laeuft der Leak-Guard erst in der CI - also erst, wenn
# die Daten bereits auf GitHub stehen.
#
#     bash scripts/install-git-hooks.sh
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

if [[ ! -d .githooks ]]; then
  echo "FEHLER: .githooks/ nicht gefunden - falsches Verzeichnis?" >&2
  exit 1
fi

chmod +x .githooks/* 2>/dev/null || true
git config core.hooksPath .githooks

echo "OK: core.hooksPath = .githooks"
echo "Aktive Hooks:"
for h in .githooks/*; do
  [[ -f "$h" ]] && echo "  - $(basename "$h")"
done
echo
echo "Selbsttest (sollte ohne Befund durchlaufen):"
bash scripts/naming-guard.sh
