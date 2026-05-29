#!/usr/bin/env bash
# Läuft IN der detached screen-Session auf der NAS (gestartet von backup.sh).
# Zweck: die Offsite-Authentifizierung lebt NUR im RAM.
#   1. ssh-agent starten  -> privater Key liegt ausschließlich im Agent-Speicher
#   2. Key/Token aus tmpfs (/dev/shm) laden und die Dateien sofort schreddern
#   3. backup_nas.sh ausführen – SSH_AUTH_SOCK und OFFSITE_API_TOKEN werden als
#      Umgebungsvariablen an alle Kindprozesse (rsync/ssh) vererbt.
# Dadurch landet kein Geheimnis je auf der Platte und der Lauf ist von der
# SSH-Pipe zum HA-Add-on entkoppelt (Netzwerk-/Container-Probleme stören nicht).
set -uo pipefail

RUNDIR="${RUNDIR:-/dev/shm/offsite-backup}"
exec >>"$RUNDIR/run.log" 2>&1

echo "$(date '+%F %T'): Bootstrap gestartet – ssh-agent (RAM-only) initialisieren"
eval "$(ssh-agent -s)" >/dev/null

if ssh-add "$RUNDIR/offsite_key" 2>&1; then
  echo "$(date '+%F %T'): Offsite-Key in ssh-agent geladen (nur RAM)"
else
  echo "$(date '+%F %T'): FEHLER: Offsite-Key konnte nicht geladen werden"
fi
# Key-Datei aus tmpfs entfernen – der Key lebt jetzt nur noch im Agent.
shred -u "$RUNDIR/offsite_key" 2>/dev/null || rm -f "$RUNDIR/offsite_key"

if [[ -f "$RUNDIR/token" ]]; then
  OFFSITE_API_TOKEN="$(base64 -d < "$RUNDIR/token")"
  export OFFSITE_API_TOKEN
  shred -u "$RUNDIR/token" 2>/dev/null || rm -f "$RUNDIR/token"
fi

export RUNNING_IN_SCREEN=1
bash "$RUNDIR/backup_nas.sh"
rc=$?

# Agent (und damit der Key) endgültig aus dem RAM entfernen.
ssh-agent -k >/dev/null 2>&1 || true
echo "$rc" > "$RUNDIR/exit_code"
echo "$(date '+%F %T'): Bootstrap beendet (rc=$rc)"
