#!/usr/bin/env bash
#
# render-publisher-target.sh — erzeugt das Prometheus-file_sd-Target des Outbox-
# Publishers ATOMAR (mktemp + mv) aus Make-/Environment-Variablen. Laeuft auf dem
# Orchestrierungs-Desktop VOR dem rsync, damit die Datei anschliessend auf der
# Monitoring-VM (site-cloud) vorhanden ist. KEINE harte Adresse, KEINE Secrets.
#
# Eingaben (aus make.env/Make-Variablen, NICHT als CLI-Argument mit Secrets):
#   PUBLISHER_METRICS_HOST   Host, unter dem der Publisher-/metrics-Port erreichbar
#                            ist (site-dc-Host). Default: DC_HOST.
#   PUBLISHER_HOST_PORT      veroeffentlichter Host-Port (1..65535). Default: 8001.
#   TARGET_FILE              Zielpfad (Default: monitoring/prometheus/targets/publisher.json)
#   DEBUG                    "1" -> Zieladresse wird geloggt (sonst NICHT).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HOST="${PUBLISHER_METRICS_HOST:-${DC_HOST:-}}"
PORT="${PUBLISHER_HOST_PORT:-8001}"
TARGET_FILE="${TARGET_FILE:-${REPO_ROOT}/monitoring/prometheus/targets/publisher.json}"

# --- Validierung (fail closed) ----------------------------------------------
if [ -z "${HOST}" ]; then
  echo "[render-publisher-target] FEHLER: PUBLISHER_METRICS_HOST/DC_HOST nicht gesetzt." >&2
  exit 1
fi
# Nur Hostname-/IPv4-Zeichen. Lehnt Schemes (://), Pfade (/), Whitespace, Quotes,
# Kommas und JSON-Sonderzeichen ab -> keine file_sd-/JSON-Injection.
if ! printf '%s' "${HOST}" | grep -Eq '^[A-Za-z0-9.-]+$'; then
  echo "[render-publisher-target] FEHLER: ungueltiger Host (nur [A-Za-z0-9.-] erlaubt)." >&2
  exit 1
fi
if ! printf '%s' "${PORT}" | grep -Eq '^[0-9]+$' || [ "${PORT}" -lt 1 ] || [ "${PORT}" -gt 65535 ]; then
  echo "[render-publisher-target] FEHLER: ungueltiger Port (1..65535)." >&2
  exit 1
fi

TARGET_DIR="$(dirname "${TARGET_FILE}")"
if [ ! -d "${TARGET_DIR}" ]; then
  echo "[render-publisher-target] FEHLER: Zielverzeichnis fehlt: ${TARGET_DIR}" >&2
  exit 1
fi

# --- Atomar schreiben (Tempdatei im selben Verzeichnis + mv) -----------------
TMP="$(mktemp "${TARGET_DIR}/.publisher.json.XXXXXX")"
trap 'rm -f "${TMP}"' EXIT
printf '[\n  { "targets": ["%s:%s"], "labels": { "site": "dc", "app": "outbox-publisher" } }\n]\n' \
  "${HOST}" "${PORT}" > "${TMP}"
# mktemp erzeugt 0600; Prometheus (anderer Container-User) muss die bind-gemountete
# Datei lesen koennen -> Modus VOR dem atomaren mv explizit auf 0644. Bei Fehler
# fail closed (Cleanup-Trap entfernt die Tempdatei).
chmod 0644 "${TMP}" || { echo "[render-publisher-target] FEHLER: Dateimodus 0644 nicht setzbar." >&2; exit 1; }
mv -f "${TMP}" "${TARGET_FILE}"
trap - EXIT

if [ "${DEBUG:-}" = "1" ]; then
  echo "[render-publisher-target] geschrieben: ${TARGET_FILE} -> ${HOST}:${PORT}"
else
  echo "[render-publisher-target] Publisher-Target geschrieben (Adresse nicht geloggt)."
fi
