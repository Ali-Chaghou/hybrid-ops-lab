#!/usr/bin/env bash
#
# check-local-perms.sh — Read-only Guard fuer lokale Orchestrierungs-Dateien
# (z. B. make.env). Verlangt sichere Rechte OHNE Gruppen-/World-Leserechte
# (erwartet 600/400). Aendert NICHTS, gibt KEINE Werte/Inhalte aus.
#
# usage: check-local-perms.sh <datei>
# exit:  0 sicher | 1 fehlt/unsicher | 2 falsche Argumente
set -euo pipefail

f="${1:-}"
[ -n "$f" ] || { echo "usage: $0 <datei>" >&2; exit 2; }
[ -f "$f" ] || { echo "[perms] FEHLER: Datei fehlt: $(basename "$f")" >&2; exit 1; }

m="$(stat -c '%a' "$f" 2>/dev/null || stat -f '%Lp' "$f" 2>/dev/null || echo '')"
case "$m" in
  [0-7]00) exit 0 ;;  # nur Owner-Bits gesetzt -> sicher (z. B. 600, 400, 700)
  *) echo "[perms] FEHLER: unsichere Rechte ($m) auf $(basename "$f"); 'chmod 600 $(basename "$f")' (kein Gruppen-/World-Read)." >&2; exit 1 ;;
esac
