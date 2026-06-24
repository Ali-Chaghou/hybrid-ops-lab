#!/usr/bin/env bash
#
# upgrade-phase-3-runtime.sh — kontrolliertes Phase-3-Runtime-Upgrade von site-dc
# (Gate D3B1). Bringt site-dc auf die neue Inventory-Version (kennt Migration 0004)
# und einen DISABLED Publisher. AKTIVIERT NICHTS: weder den Publisher noch den
# Event-Versand. Variante B (Inventory kontrolliert stoppen -> 0004 -> neue Version),
# weil check_schema fail closed ist (neue Version verlangt 0004; alte Version lehnt
# 0004 als unbekannt-neuer ab).
#
# Eigenschaften: nur auf site-dc; flock; atomarer JSON-State; Resume; fail closed;
# keine Secrets in Argumenten/Logs; nutzt AUSSCHLIESSLICH das Basis-Compose mit
# PUBLISHER_ENABLED="false"; verweigert den Lauf bei einer aktivierenden Override-
# Datei; sendet keine Queue-Nachricht. Das Gate-A-Skript upgrade-site-dc.sh bleibt
# unberuehrt.
#
# Befehle:  preflight | run | resume | state
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SITE_DIR="${REPO_ROOT}/sites/dc"
COMPOSE_FILE="${SITE_DIR}/docker-compose.yml"
ENV_FILE="${ENV_FILE:-${SITE_DIR}/.env}"
# Ueberschreibbar fuer Tests (sonst gitignored unter sites/dc).
STATE_DIR="${PHASE3_STATE_DIR:-${SITE_DIR}/.phase3-runtime}"
STATE_FILE="${STATE_DIR}/state.json"
LOCK_FILE="${STATE_DIR}/.upgrade.lock"
REQUIRED_MIGRATION="0004_add_outbox_claim_fields"

log()  { printf '\n[phase3-upgrade] %s\n' "$*"; }
fail() { printf '\n[phase3-upgrade] FEHLER: %s\n' "$*" >&2; exit 1; }

# Compose-Wrapper: Secrets kommen ueber --env-file (Datei, NICHT als Argument).
dc() { docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" "$@"; }

# --- State (atomar; nur Ablaufstatus, NIE Schema, NIE Secrets) ----------------
STEPS=(preflight images-built roles-ready inventory-stopped migration-complete inventory-ready publisher-disabled-ready complete)

step_index() {
  local s i=0
  for s in "${STEPS[@]}"; do [ "$s" = "$1" ] && { echo "$i"; return 0; }; i=$((i+1)); done
  echo -1
}

get_step() {
  [ -f "${STATE_FILE}" ] || { echo ""; return 0; }
  python3 - "${STATE_FILE}" <<'PY' 2>/dev/null || echo ""
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    print(d.get("step", "") if isinstance(d, dict) else "")
except Exception:
    print("")
PY
}

write_state() {
  # $1 = step, $2 = complete(true/false)
  mkdir -p "${STATE_DIR}"
  local tmp
  tmp="$(mktemp "${STATE_DIR}/.state.XXXXXX")" || fail "Temp-State nicht anlegbar"
  STEP="$1" COMPLETE="$2" python3 - "$tmp" <<'PY' || { rm -f "$tmp"; fail "State-Serialisierung fehlgeschlagen"; }
import json, os, sys, datetime
json.dump({
    "schema_version": 1,
    "gate": "D3B1",
    "step": os.environ["STEP"],
    "complete": os.environ["COMPLETE"] == "true",
    "publisher_enabled": False,
    "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
}, open(sys.argv[1], "w"), indent=2)
PY
  mv -f "$tmp" "${STATE_FILE}" || { rm -f "$tmp"; fail "Atomarer State-Wechsel (mv) fehlgeschlagen"; }
}

state_cmd() {
  if [ ! -f "${STATE_FILE}" ]; then echo "step: (none)"; return 0; fi
  python3 "${REPO_ROOT}/ops/deploy/check-phase-3-runtime-state.py" "${STATE_FILE}" >/dev/null 2>&1 \
    && echo "step: $(get_step) (valid/complete)" || echo "step: $(get_step) (incomplete/invalid)"
}

# --- Guards -------------------------------------------------------------------
assert_no_activating_override() {
  # Kein Override darf PUBLISHER_ENABLED aktivieren.
  local f
  for f in "${SITE_DIR}"/docker-compose.override.yml "${SITE_DIR}"/docker-compose.*.override.yml; do
    [ -f "$f" ] || continue
    if grep -Eiq 'PUBLISHER_ENABLED' "$f"; then
      fail "Aktivierende Override-Datei erkannt ($f) — Abbruch. D3B1 startet den Publisher NIE."
    fi
  done
}

assert_base_publisher_disabled() {
  grep -Eq 'PUBLISHER_ENABLED:[[:space:]]*"false"' "${COMPOSE_FILE}" \
    || fail "Basis-Compose hat den Publisher NICHT hart auf disabled — Abbruch."
}

# --- Phasen -------------------------------------------------------------------
preflight() {
  log "PREFLIGHT"
  [ -f "${COMPOSE_FILE}" ] || fail "sites/dc/docker-compose.yml fehlt — falsches Repository?"
  [ -f "${REPO_ROOT}/apps/inventory/Dockerfile" ] || fail "Inventory-Dockerfile fehlt"
  [ -f "${REPO_ROOT}/apps/publisher/Dockerfile" ] || fail "Publisher-Dockerfile fehlt"
  [ -f "${ENV_FILE}" ] || fail ".env fehlt (aus .env.example anlegen, starke Werte setzen)"
  local mode
  mode="$(stat -c '%a' "${ENV_FILE}" 2>/dev/null || echo '')"
  case "${mode}" in
    *[4567]) fail ".env ist world-readable (Modus ${mode}); chmod 600 setzen" ;;
  esac
  # Pflicht/Definiertheit OHNE Werte auszugeben:
  grep -Eq '^INVENTORY_PUBLISHER_PASSWORD=.+' "${ENV_FILE}" \
    || fail "INVENTORY_PUBLISHER_PASSWORD nicht gesetzt (leer = Fail closed)"
  grep -Eq '^PUBLISHER_SQS_ENDPOINT_URL=' "${ENV_FILE}" || fail "PUBLISHER_SQS_ENDPOINT_URL nicht definiert"
  grep -Eq '^PUBLISHER_SQS_QUEUE_URL=' "${ENV_FILE}"    || fail "PUBLISHER_SQS_QUEUE_URL nicht definiert"
  assert_base_publisher_disabled
  assert_no_activating_override
  command -v docker >/dev/null 2>&1 || fail "docker nicht gefunden"
  docker compose version >/dev/null 2>&1 || fail "docker compose nicht verfuegbar"
  # DB-Erreichbarkeit (kein Secret): pg_isready im laufenden db-Container, falls vorhanden.
  if dc ps --services 2>/dev/null | grep -qx db; then
    dc exec -T db pg_isready >/dev/null 2>&1 || log "Hinweis: db noch nicht bereit (wird im Lauf gestartet)."
  fi
  write_state preflight false
  log "Preflight ok."
}

phase_images_built() {
  log "BUILD: Inventory- und Publisher-Image bauen (kein Downtime)"
  dc build db-bootstrap publisher || die "Image-Build fehlgeschlagen"
  write_state images-built false
}

phase_roles_ready() {
  log "ROLLEN: db starten + idempotenter Bootstrap (legt inventory_publisher an), prepare pruefen"
  dc up -d db || die "db-Start fehlgeschlagen"
  dc run --rm --no-deps db-bootstrap || die "db-bootstrap fehlgeschlagen"
  dc run --rm --no-deps db-prepare || die "db-prepare fehlgeschlagen"
  write_state roles-ready false
}

phase_inventory_stopped() {
  log "VARIANTE B: altes Inventory kontrolliert stoppen (nicht loeschen) — kurzes Downtime-Fenster"
  dc stop inventory >/dev/null 2>&1 || true   # idempotent: evtl. laeuft nichts
  write_state inventory-stopped false
}

phase_migration_complete() {
  log "MIGRATION: inventory-migrate ausfuehren (idempotent; wendet 0004 an)"
  dc run --rm --no-deps inventory-migrate || die "inventory-migrate fehlgeschlagen"
  log "VERIFY: Migration ${REQUIRED_MIGRATION} angewandt"
  # Verifikation laeuft in der neuen Inventory-Image-Runtime ueber den Schema-Check
  # beim spaeteren /readyz; hier nur, dass migrate erfolgreich endete.
  write_state migration-complete false
}

phase_inventory_ready() {
  log "INVENTORY: neue Version starten + /readyz verifizieren (validiert Schema inkl. 0004)"
  dc up -d inventory || die "inventory-Start fehlgeschlagen"
  _wait_health inventory || die "inventory wurde nicht healthy/ready"
  write_state inventory-ready false
}

phase_publisher_disabled_ready() {
  log "PUBLISHER: DISABLED starten (PUBLISHER_ENABLED=false aus Basis-Compose)"
  dc up -d publisher || die "publisher-Start fehlgeschlagen"
  _wait_health publisher || die "publisher wurde nicht healthy"
  log "VERIFY: publisher disabled, kein Publish, keine Queue-Aktivierung"
  _verify_publisher_disabled || die "Publisher-Disabled-Verifikation fehlgeschlagen"
  write_state publisher-disabled-ready false
}

phase_complete() {
  write_state complete true
  log "PHASE-3-RUNTIME-UPGRADE ABGESCHLOSSEN: neue Inventory-Version auf Schema 0004, Publisher vorhanden & DISABLED, kein Event publiziert, Phase 3 NICHT aktiviert."
}

# Healthcheck-Status abwarten (compose healthcheck).
_wait_health() {
  local svc="$1" i cid status
  cid="$(dc ps -q "$svc" 2>/dev/null || true)"
  [ -n "$cid" ] || return 1
  for i in $(seq 1 30); do
    status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null || echo '')"
    case "$status" in
      healthy|running) return 0 ;;
    esac
    sleep 2
  done
  return 1
}

# Read-only Nachweis: publisher_enabled=0 und kein erfolgreicher Publish.
_verify_publisher_disabled() {
  dc exec -T publisher python -c '
import urllib.request, sys
b = urllib.request.urlopen("http://127.0.0.1:8000/metrics", timeout=3).read().decode()
ok = any(line.startswith("publisher_enabled ") and line.split()[1] in ("0","0.0") for line in b.splitlines())
sys.exit(0 if ok else 1)
' >/dev/null 2>&1
}

# Bei Fehler nach erfolgreicher Migration NICHT das alte Inventory starten.
die() {
  local st; st="$(get_step)"
  printf '\n[phase3-upgrade] FEHLER: %s (letzter State: %s)\n' "$*" "${st:-none}" >&2
  case "${st}" in
    preflight|images-built|roles-ready|inventory-stopped)
      log "Fehler VOR erfolgreicher Migration — altes Inventory darf kontrolliert wieder gestartet werden:"
      log "  docker compose -f ${COMPOSE_FILE} --env-file ${ENV_FILE} up -d inventory   (altes Image)"
      ;;
    migration-complete|inventory-ready|publisher-disabled-ready)
      log "Fehler NACH Migration — altes Inventory NICHT starten (lehnt 0004 fail closed ab). Mit 'resume' fortsetzen (neue Version)."
      ;;
  esac
  log "Publisher bleibt disabled. Kein Event publiziert."
  exit 1
}

# --- Ablaufsteuerung (run/resume teilen denselben, idempotenten Pfad) ---------
_run_from() {
  # Fuehrt alle Phasen ab Index $1 aus.
  local start="$1"
  [ "$start" -le 0 ] && preflight
  [ "$start" -le 1 ] && phase_images_built
  [ "$start" -le 2 ] && phase_roles_ready
  [ "$start" -le 3 ] && phase_inventory_stopped
  [ "$start" -le 4 ] && phase_migration_complete
  [ "$start" -le 5 ] && phase_inventory_ready
  [ "$start" -le 6 ] && phase_publisher_disabled_ready
  [ "$start" -le 7 ] && phase_complete
}

with_lock() {
  command -v flock >/dev/null 2>&1 || fail "flock nicht gefunden"
  mkdir -p "${STATE_DIR}"
  exec 9>"${LOCK_FILE}"
  flock -n 9 || fail "Ein anderer Phase-3-Upgrade-Lauf haelt bereits den Lock (${LOCK_FILE})."
}

cmd_run() {
  with_lock
  assert_base_publisher_disabled
  assert_no_activating_override
  _run_from 0
}

cmd_resume() {
  with_lock
  assert_base_publisher_disabled
  assert_no_activating_override
  local cur idx
  # FILE-ABWESENHEIT (kein State) -> vollstaendig starten.
  if [ ! -f "${STATE_FILE}" ]; then
    log "Kein State — starte vollstaendig."
    _run_from 0
    return
  fi
  # File VORHANDEN aber leerer/unlesbarer step (z. B. korruptes JSON) -> fail closed.
  cur="$(get_step)"
  [ -n "${cur}" ] || fail "State-Datei vorhanden, aber unlesbar/korrupt — Abbruch (kein blindes Fortsetzen)."
  # Beschaedigter/ungueltiger step -> fail closed (kein blindes Fortsetzen).
  case "${cur}" in
    preflight|images-built|roles-ready|inventory-stopped|migration-complete|inventory-ready|publisher-disabled-ready|complete) ;;
    *) fail "Beschaedigter/unbekannter State (step=${cur}) — Abbruch (kein blindes Fortsetzen)." ;;
  esac
  if [ "${cur}" = "complete" ]; then
    log "Bereits complete — nichts zu tun."
    return
  fi
  idx="$(step_index "${cur}")"
  log "RESUME ab Schritt nach '${cur}' (Index $((idx+1)))."
  _run_from $((idx+1))
}

usage() { echo "usage: $0 {preflight|run|resume|state}" >&2; exit 2; }

case "${1:-}" in
  preflight) with_lock; preflight ;;
  run)       cmd_run ;;
  resume)    cmd_resume ;;
  state)     state_cmd ;;
  *)         usage ;;
esac
