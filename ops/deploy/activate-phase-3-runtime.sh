#!/usr/bin/env bash
# Kontrollierte D3B2.3-Publisher-Aktivierung.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SITE_DIR="${REPO_ROOT}/sites/dc"
BASE_COMPOSE="${SITE_DIR}/docker-compose.yml"
ENABLE_COMPOSE="${SITE_DIR}/docker-compose.publisher-enabled.yml"
ENV_FILE="${ENV_FILE:-${SITE_DIR}/.env}"
RUNTIME_STATE="${SITE_DIR}/.phase3-runtime/state.json"
STATE_DIR="${PHASE3_ACTIVATION_STATE_DIR:-${SITE_DIR}/.phase3-activation}"
STATE_FILE="${STATE_DIR}/state.json"
LOCK_FILE="${STATE_DIR}/.activation.lock"

log()  { printf "\n[phase3-activation] %s\n" "$*"; }
fail() { printf "\n[phase3-activation] FEHLER: %s\n" "$*" >&2; exit 1; }

dc_disabled() {
  docker compose -f "${BASE_COMPOSE}" --env-file "${ENV_FILE}" "$@"
}

dc_enabled() {
  docker compose -f "${BASE_COMPOSE}" -f "${ENABLE_COMPOSE}" --env-file "${ENV_FILE}" "$@"
}

publisher_port() {
  local port
  port="$(awk -F= '$1 == "PUBLISHER_HOST_PORT" { value=substr($0, index($0, "=") + 1) } END { print value }' "${ENV_FILE}")"
  printf "%s\n" "${port:-8001}"
}

with_lock() {
  command -v flock >/dev/null 2>&1 || fail "flock nicht gefunden"
  mkdir -p "${STATE_DIR}"
  exec 9>"${LOCK_FILE}"
  flock -n 9 || fail "Ein anderer Activation-Lauf haelt bereits den Lock."
}

write_state() {
  local step="$1" enabled="$2" tmp now
  mkdir -p "${STATE_DIR}"
  tmp="$(mktemp "${STATE_DIR}/.state.XXXXXX")" || fail "Temp-State nicht anlegbar"
  now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf "{\n  \"schema_version\": 1,\n  \"gate\": \"D3B2.3\",\n  \"step\": \"%s\",\n  \"publisher_enabled\": %s,\n  \"updated_at\": \"%s\"\n}\n" "${step}" "${enabled}" "${now}" > "${tmp}"
  mv -f "${tmp}" "${STATE_FILE}" || fail "Atomarer State-Wechsel fehlgeschlagen"
}

state_cmd() {
  if [ ! -f "${STATE_FILE}" ]; then
    echo "step: (none)"
    return 0
  fi
  python3 -m json.tool "${STATE_FILE}"
}

wait_health() {
  local mode="$1" cid status i
  if [ "${mode}" = "enabled" ]; then
    cid="$(dc_enabled ps -q publisher 2>/dev/null || true)"
  else
    cid="$(dc_disabled ps -q publisher 2>/dev/null || true)"
  fi
  [ -n "${cid}" ] || return 1
  for ((i = 0; i < 30; i++)); do
    status="$(docker inspect -f "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}" "${cid}" 2>/dev/null || true)"
    [ "${status}" = "healthy" ] && return 0
    sleep 2
  done
  return 1
}

http_is_200() {
  local path="$1" port code
  port="$(publisher_port)"
  code="$(curl -sS -o /dev/null -w "%{http_code}" "http://127.0.0.1:${port}/${path}")" || return 1
  [ "${code}" = "200" ]
}

metric_is() {
  local name="$1" expected="$2" port
  port="$(publisher_port)"
  curl -fsS "http://127.0.0.1:${port}/metrics" |
    awk -v metric="${name}" -v expected="${expected}" '$1 == metric { found=1; ok=(($2 + 0) == (expected + 0)) } END { exit !(found && ok) }'
}

verify_disabled() {
  http_is_200 healthz &&
    http_is_200 readyz &&
    metric_is publisher_enabled 0
}

verify_enabled() {
  http_is_200 healthz &&
    http_is_200 readyz &&
    metric_is publisher_enabled 1 &&
    metric_is publisher_live 1 &&
    metric_is publisher_ready 1
}

wait_verified() {
  local mode="$1" i

  for ((i = 0; i < 30; i++)); do
    if [ "${mode}" = "enabled" ]; then
      verify_enabled && return 0
    else
      verify_disabled && return 0
    fi
    sleep 2
  done

  return 1
}

verify_queue_route() {
  dc_disabled exec -T publisher python -c '
import os
import boto3

client = boto3.client(
    "sqs",
    endpoint_url=os.environ.get("SQS_ENDPOINT_URL") or None,
    region_name=os.environ.get("AWS_REGION", "eu-central-1"),
)
response = client.get_queue_attributes(
    QueueUrl=os.environ["SQS_QUEUE_URL"],
    AttributeNames=[
        "QueueArn",
        "ApproximateNumberOfMessages",
        "ApproximateNumberOfMessagesNotVisible",
        "ApproximateNumberOfMessagesDelayed",
    ],
)
attributes = response.get("Attributes", {})
counts = {
    name: int(attributes.get(name, "0"))
    for name in (
        "ApproximateNumberOfMessages",
        "ApproximateNumberOfMessagesNotVisible",
        "ApproximateNumberOfMessagesDelayed",
    )
}
if any(counts.values()):
    raise SystemExit(
        "Queue ist nicht leer: "
        + ", ".join(f"{name}={value}" for name, value in counts.items())
    )
' >/dev/null
}

env_get() {
  local key="$1"
  awk -F= -v key="${key}" '
    $1 == key {
      print substr($0, index($0, "=") + 1)
      found=1
    }
    END { exit !found }
  ' "${ENV_FILE}"
}

read_outbox_counts() {
  local user db
  user="$(env_get POSTGRES_USER)" || fail "POSTGRES_USER fehlt"
  db="$(env_get INVENTORY_DB)" || fail "INVENTORY_DB fehlt"

  dc_disabled exec -T db psql -X -v ON_ERROR_STOP=1 \
    -U "${user}" -d "${db}" -Atq -c \
    "BEGIN READ ONLY;
     SELECT
       (count(*) FILTER (WHERE status = 'pending'))::text || ' ' ||
       (count(*) FILTER (
          WHERE status = 'pending'
            AND (claim_owner IS NOT NULL OR claimed_at IS NOT NULL)
        ))::text
     FROM event_outbox;
     COMMIT;"
}

verify_expected_backlog() {
  local expected counts pending claimed
  expected="${D3B23_EXPECTED_PENDING:-}"

  [[ "${expected}" =~ ^[0-9]+$ ]] ||
    fail "D3B23_EXPECTED_PENDING muss als nichtnegative Zahl gesetzt sein"

  counts="$(read_outbox_counts)" ||
    fail "Outbox-Zaehler konnten nicht read-only gelesen werden"

  read -r pending claimed <<< "${counts}"

  [[ "${pending}" =~ ^[0-9]+$ && "${claimed}" =~ ^[0-9]+$ ]] ||
    fail "Unerwartetes Format der Outbox-Zaehler"

  [ "${pending}" = "${expected}" ] ||
    fail "Outbox-Gate: erwartet pending=${expected}, gefunden=${pending}"

  [ "${claimed}" = "0" ] ||
    fail "Outbox-Gate: ${claimed} Zeilen besitzen noch einen Claim"

  log "Outbox-Gate ok: pending=${pending}, claimed=0."
}

preflight() {
  log "PREFLIGHT"

  [ -f "${BASE_COMPOSE}" ] || fail "Basis-Compose fehlt"
  [ -f "${ENABLE_COMPOSE}" ] || fail "Enable-Compose fehlt"
  [ -f "${ENV_FILE}" ] || fail ".env fehlt"
  [ -f "${RUNTIME_STATE}" ] || fail "D3B2.2-Runtime-State fehlt"

  local mode
  mode="$(stat -c "%a" "${ENV_FILE}" 2>/dev/null || true)"
  case "${mode}" in
    *[4567]) fail ".env ist world-readable (Modus ${mode})" ;;
    "") fail ".env-Modus nicht lesbar" ;;
  esac

  python3 "${REPO_ROOT}/ops/deploy/check-phase-3-runtime-state.py" "${RUNTIME_STATE}" >/dev/null 2>&1 ||
    fail "D3B2.2-Runtime-State ist nicht complete und disabled"

  grep -Eq 'PUBLISHER_ENABLED:[[:space:]]*"false"' "${BASE_COMPOSE}" ||
    fail "Basis-Compose ist nicht hart deaktiviert"

  grep -Eq 'PUBLISHER_ENABLED:[[:space:]]*"true"' "${ENABLE_COMPOSE}" ||
    fail "Enable-Compose aktiviert den Publisher nicht eindeutig"

  grep -Eq "^INVENTORY_PUBLISHER_PASSWORD=.+" "${ENV_FILE}" ||
    fail "Publisher-Passwort fehlt"
  grep -Eq "^PUBLISHER_SQS_ENDPOINT_URL=.+" "${ENV_FILE}" ||
    fail "Publisher-SQS-Endpunkt fehlt"
  grep -Eq "^PUBLISHER_SQS_QUEUE_URL=.+" "${ENV_FILE}" ||
    fail "Publisher-Queue-URL fehlt"

  dc_enabled config --quiet ||
    fail "Zusammengefuehrte Compose-Konfiguration ungueltig"

  dc_enabled config --format json | python3 -c '
import json
import sys

config = json.load(sys.stdin)
value = config["services"]["publisher"]["environment"]["PUBLISHER_ENABLED"]
if str(value).lower() != "true":
    raise SystemExit(1)
' || fail "Zusammengefuehrte Konfiguration aktiviert den Publisher nicht"

  verify_disabled ||
    fail "Publisher ist vor Aktivierung nicht healthy und disabled"

  verify_expected_backlog

  verify_queue_route ||
    fail "Queue-Route ist nicht read-only erreichbar"

  write_state preflight false
  log "Preflight ok. Keine Aktivierung vorgenommen."
}

emergency_disable() {
  log "NOTAUS: Publisher ueber Basis-Compose deaktivieren"

  if ! dc_disabled up -d --no-deps --force-recreate publisher >/dev/null 2>&1; then
    log "NOTAUS fehlgeschlagen: Container konnte nicht neu erstellt werden."
    return 1
  fi

  if ! wait_health disabled; then
    log "NOTAUS fehlgeschlagen: Disabled Publisher wurde nicht healthy."
    return 1
  fi

  if ! wait_verified disabled; then
    log "NOTAUS fehlgeschlagen: Disable-Verifikation nicht bestätigt."
    return 1
  fi

  write_state emergency-disabled false
  log "Notaus erfolgreich: Publisher disabled und healthy."
}

disable_cmd() {
  with_lock
  log "DISABLE: Publisher ueber Basis-Compose deaktivieren"

  dc_disabled up -d --no-deps --force-recreate publisher ||
    fail "Publisher konnte nicht disabled neu gestartet werden"

  wait_health disabled ||
    fail "Disabled Publisher wurde nicht healthy"

  wait_verified disabled ||
    fail "Disable-Verifikation fehlgeschlagen"

  write_state disabled false
  log "Publisher deaktiviert und healthy."
}

enable_cmd() {
  [ "${D3B23_ACK_ACTIVATE:-}" = "1" ] ||
    fail "Aktivierung nicht bestaetigt. D3B23_ACK_ACTIVATE=1 ist erforderlich."

  with_lock
  preflight

  log "ENABLE: Publisher bewusst ueber explizite Override-Datei starten"

  if ! dc_enabled up -d --no-deps --force-recreate publisher; then
    emergency_disable || true
    fail "Publisher-Start fehlgeschlagen; Notaus wurde versucht"
  fi

  if ! wait_health enabled; then
    emergency_disable || true
    fail "Publisher wurde nicht healthy; Publisher wurde wieder deaktiviert"
  fi

  if ! wait_verified enabled; then
    emergency_disable || true
    fail "Enable-Verifikation fehlgeschlagen; Publisher wurde wieder deaktiviert"
  fi

  write_state enabled true
  log "Publisher aktiviert und healthy. D3B2.3 ist damit noch nicht abgeschlossen."
}

usage() {
  echo "usage: $0 {preflight|enable|disable|state}" >&2
  exit 2
}

case "${1:-}" in
  preflight) with_lock; preflight ;;
  enable)    enable_cmd ;;
  disable)   disable_cmd ;;
  state)     state_cmd ;;
  *)         usage ;;
esac
