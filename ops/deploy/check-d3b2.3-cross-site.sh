#!/usr/bin/env bash
# Read-only D3B2.3 Cross-Site-Preflight vom Orchestrierungs-Desktop.
set -euo pipefail

SSH_USER="${SSH_USER:-ops}"
DC_HOST="${DC_HOST:-}"
CLOUD_HOST="${CLOUD_HOST:-}"
REMOTE_DIR="${REMOTE_DIR:-/home/${SSH_USER}/hybrid-ops-lab}"
EXPECTED_PENDING="${D3B23_EXPECTED_PENDING:-}"
EXPECTED_CONSUMER_RELEASE="${D3B23_EXPECTED_CONSUMER_RELEASE_SHA:-}"

log()  { printf '\n[d3b2.3-cross-site] %s\n' "$*"; }
fail() { printf '\n[d3b2.3-cross-site] FEHLER: %s\n' "$*" >&2; exit 1; }

validate_inputs() {
  [ -n "${DC_HOST}" ] || fail "DC_HOST fehlt"
  [ -n "${CLOUD_HOST}" ] || fail "CLOUD_HOST fehlt"
  [[ "${EXPECTED_PENDING}" =~ ^[0-9]+$ ]] ||
    fail "D3B23_EXPECTED_PENDING muss eine nichtnegative Zahl sein"
  [[ "${EXPECTED_CONSUMER_RELEASE}" =~ ^[0-9a-f]{40}$ ]] ||
    fail "D3B23_EXPECTED_CONSUMER_RELEASE_SHA muss 40 hex sein"
}

run_cloud_checks() {
  log "site-cloud: Consumer, Queue, Monitoring und Alerts read-only pruefen"

  ssh "${SSH_USER}@${CLOUD_HOST}" bash -s -- \
    "${REMOTE_DIR}" "${EXPECTED_CONSUMER_RELEASE}" <<'REMOTE'
set -euo pipefail

repo="$1"
expected_release="$2"
cd "${repo}"

python3 ops/deploy/check-d3b2-consumer-state.py \
  sites/cloud/.d3b2-consumer/state.json "${expected_release}"

kubectl -n inventory rollout status \
  deployment/inventory-consumer --timeout=10s >/dev/null

kubectl -n inventory get pod \
  -l app=inventory-consumer -o json |
  python3 ops/deploy/select-consumer-pod.py \
    --require-running \
    --require-ready \
    --require-image-id \
    --print name >/dev/null

list_xml="$(curl -fsS --max-time 6 \
  'http://localhost:9324/?Action=ListQueues&Version=2012-11-05')"
grep -q 'inventory-movements' <<<"${list_xml}"
grep -q 'inventory-movements-dlq' <<<"${list_xml}"

redrive_xml="$(curl -fsS --max-time 6 \
  'http://localhost:9324/queue/inventory-movements?Action=GetQueueAttributes&AttributeName.1=RedrivePolicy&Version=2012-11-05')"
grep -q 'maxReceiveCount' <<<"${redrive_xml}"
grep -q '5' <<<"${redrive_xml}"

python3 ops/deploy/check-queue-empty.py \
  http://localhost:9324 >/dev/null

[ "$(curl -sS --max-time 6 -o /dev/null -w '%{http_code}' \
  http://localhost:9090/-/ready)" = "200" ]

[ "$(curl -sS --max-time 6 -o /dev/null -w '%{http_code}' \
  http://localhost:9093/-/ready)" = "200" ]

curl -fsS --max-time 6 'http://localhost:9090/api/v1/targets?state=active' |
python3 -c '
import json
import sys

data = json.load(sys.stdin)
if data.get("status") != "success":
    raise SystemExit("Prometheus targets status != success")

targets = data.get("data", {}).get("activeTargets")
if not isinstance(targets, list) or not targets:
    raise SystemExit("Keine aktiven Prometheus-Targets")

jobs = {item.get("labels", {}).get("job") for item in targets}
missing = {"consumer", "publisher"} - jobs
if missing:
    raise SystemExit("Pflicht-Targets fehlen: " + ",".join(sorted(missing)))

down = [
    item.get("labels", {}).get("job", "<unknown>")
    for item in targets
    if item.get("health") != "up"
]
if down:
    raise SystemExit("Prometheus-Targets down: " + ",".join(sorted(down)))
'

curl -fsS --max-time 6 'http://localhost:9090/api/v1/rules' |
python3 -c 'import json,sys; d=json.load(sys.stdin); n={r.get("name") for g in d.get("data",{}).get("groups",[]) for r in g.get("rules",[]) if r.get("type")=="alerting"}; req={"ConsumerDown","ConsumerNotReady","MainQueueBacklog","DLQNotEmpty","PublisherDown","PublisherEnabledNotReady","PublisherPollErrors","PublisherPublishErrors","PublisherFinalizeConflicts","PublisherBacklogStuck","PublisherOldestPendingAge","PublisherHighRetries","PublisherStaleClaims"}; m=req-n; assert d.get("status")=="success" and not m, f"missing rules: {sorted(m)}";'

curl -fsS --max-time 6 'http://localhost:9090/api/v1/alerts' |
python3 -c 'import json,sys; d=json.load(sys.stdin); w={"ConsumerDown","ConsumerNotReady","MainQueueBacklog","DLQNotEmpty","PublisherDown","PublisherEnabledNotReady","PublisherPollErrors","PublisherPublishErrors","PublisherFinalizeConflicts","PublisherBacklogStuck","PublisherOldestPendingAge","PublisherHighRetries","PublisherStaleClaims"}; a=d.get("data",{}).get("alerts",[]); f=sorted({x.get("labels",{}).get("alertname") for x in a if x.get("state")=="firing" and x.get("labels",{}).get("alertname") in w}); assert d.get("status")=="success" and not f, f"firing alerts: {f}";'

printf 'site-cloud-read-only=ok\n'
REMOTE
}

run_dc_checks() {
  log "site-dc: Runtime-State, Inventory, Publisher und Outbox read-only pruefen"

  ssh "${SSH_USER}@${DC_HOST}" bash -s -- \
    "${REMOTE_DIR}" "${EXPECTED_PENDING}" <<'REMOTE'
set -euo pipefail

repo="$1"
expected_pending="$2"
cd "${repo}"

base="sites/dc/docker-compose.yml"
enable="sites/dc/docker-compose.publisher-enabled.yml"
env_file="sites/dc/.env"

python3 ops/deploy/check-phase-3-runtime-state.py \
  sites/dc/.phase3-runtime/state.json

[ -f "${env_file}" ]

mode="$(stat -c '%a' "${env_file}")"
case "${mode}" in
  *[4567])
    echo ".env ist world-readable (Modus ${mode})" >&2
    exit 1
    ;;
esac

grep -Eq 'PUBLISHER_ENABLED:[[:space:]]*"false"' "${base}"
grep -Eq 'PUBLISHER_ENABLED:[[:space:]]*"true"' "${enable}"

docker compose -f "${base}" -f "${enable}" --env-file "${env_file}" \
  config --format json |
python3 -c '
import json
import sys

config = json.load(sys.stdin)
value = config["services"]["publisher"]["environment"]["PUBLISHER_ENABLED"]
if str(value).lower() != "true":
    raise SystemExit("Merged publisher config is not enabled")
'

env_get() {
  local key="$1"
  awk -F= -v key="${key}" '
    $1 == key {
      print substr($0, index($0, "=") + 1)
      found=1
    }
    END { exit !found }
  ' "${env_file}"
}

inventory_port="$(env_get INVENTORY_HOST_PORT || true)"
publisher_port="$(env_get PUBLISHER_HOST_PORT || true)"
inventory_port="${inventory_port:-8000}"
publisher_port="${publisher_port:-8001}"

for service in inventory publisher; do
  cid="$(docker compose -f "${base}" --env-file "${env_file}" ps -q "${service}")"
  [ -n "${cid}" ]
  status="$(docker inspect -f \
    '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
    "${cid}")"
  [ "${status}" = "healthy" ]
done

for endpoint in healthz readyz; do
  [ "$(curl -sS --max-time 6 -o /dev/null -w '%{http_code}' \
    "http://localhost:${inventory_port}/${endpoint}")" = "200" ]
  [ "$(curl -sS --max-time 6 -o /dev/null -w '%{http_code}' \
    "http://localhost:${publisher_port}/${endpoint}")" = "200" ]
done

curl -fsS --max-time 6 "http://localhost:${publisher_port}/metrics" |
awk '
  $1 == "publisher_enabled" {
    found=1
    ok=(($2 + 0) == 0)
  }
  END { exit !(found && ok) }
'

user="$(env_get POSTGRES_USER)"
db="$(env_get INVENTORY_DB)"
[ -n "${user}" ] && [ -n "${db}" ]

counts="$(
  docker compose -f "${base}" --env-file "${env_file}" exec -T db \
    psql -X -v ON_ERROR_STOP=1 -U "${user}" -d "${db}" -Atq -c \
    "BEGIN READ ONLY;
     SELECT
       (count(*) FILTER (WHERE status = 'pending'))::text || ' ' ||
       (count(*) FILTER (
          WHERE status = 'pending'
            AND (claim_owner IS NOT NULL OR claimed_at IS NOT NULL)
        ))::text
     FROM event_outbox;
     COMMIT;"
)"

read -r pending claimed <<<"${counts}"
[[ "${pending}" =~ ^[0-9]+$ && "${claimed}" =~ ^[0-9]+$ ]]
[ "${pending}" = "${expected_pending}" ]
[ "${claimed}" = "0" ]

if [ -f sites/dc/.phase3-activation/state.json ]; then
  python3 -c '
import json
import sys

with open(sys.argv[1], encoding="utf-8") as fh:
    data = json.load(fh)

assert data.get("schema_version") == 1
assert data.get("gate") == "D3B2.3"
assert data.get("publisher_enabled") is False
assert data.get("step") in {
    "preflight",
    "disabled",
    "emergency-disabled",
}
' sites/dc/.phase3-activation/state.json
fi

printf 'site-dc-read-only=ok\n'
REMOTE
}

main() {
  validate_inputs
  run_cloud_checks
  run_dc_checks
  log "Cross-Site-Preflight ok. Keine Aktivierung vorgenommen."
}

main "$@"
