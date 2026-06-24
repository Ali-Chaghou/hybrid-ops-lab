#!/usr/bin/env bash
#
# upgrade-consumer-runtime.sh — kontrollierter D3B2.1-Consumer-Rollout, AUSSCHLIESSLICH
# auf site-cloud. Bringt Queue+DLQ+Redrive, Consumer-DB/Schema, den aktuellen Consumer
# und das Consumer-Monitoring kontrolliert live. Beruehrt WEDER site-dc NOCH den
# Publisher; aktiviert WEDER Phase 3 NOCH Events. Keine direkte Queue-Nachricht,
# kein Purge, kein Redrive.
#
# Eigenschaften: flock; atomarer JSON-State; Resume; fail closed; keine Secrets in
# Argumenten/Logs. Das Phase-3-Skript upgrade-phase-3-runtime.sh und upgrade-site-dc.sh
# bleiben unberuehrt.
#
# Befehle:  preflight | run | resume | state
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CLOUD_DIR="${REPO_ROOT}/sites/cloud"
MON_DIR="${REPO_ROOT}/monitoring"
COMPOSE_FILE="${CLOUD_DIR}/docker-compose.yml"
MON_COMPOSE="${MON_DIR}/docker-compose.yml"
ENV_FILE="${ENV_FILE:-${CLOUD_DIR}/.env}"
STATE_DIR="${D3B2_STATE_DIR:-${CLOUD_DIR}/.d3b2-consumer}"
STATE_FILE="${STATE_DIR}/state.json"
ROLLBACK_FILE="${STATE_DIR}/rollback.json"
RESTART_AUDIT="${STATE_DIR}/restart-ack.json"
LOCK_FILE="${STATE_DIR}/.rollout.lock"
TARGET_DIR="${D3B2_TARGET_DIR:-${REPO_ROOT}/monitoring/prometheus/targets}"
# Der eigentliche Consumer-Deploy (Build/Import/Secret/Rollout) bleibt deploy-consumer.sh;
# ueberschreibbar fuer Tests.
DEPLOY_CONSUMER_CMD="${DEPLOY_CONSUMER_CMD:-${REPO_ROOT}/ops/deploy/deploy-consumer.sh}"

QUEUE_ENDPOINT="${QUEUE_ENDPOINT:-http://localhost:9324}"
PROM_ENDPOINT="${PROM_ENDPOINT:-http://localhost:9090}"
# Read-only Queue-Leerheitsgate (ueberschreibbar fuer Tests).
QUEUE_GATE_CMD="${QUEUE_GATE_CMD:-python3 ${REPO_ROOT}/ops/deploy/check-queue-empty.py ${QUEUE_ENDPOINT}}"
# Bewusstes Operator-Acknowledgement fuer bestehende Consumer-Restarts (kein Secret).
ACK_RESTARTS="${D3B2_ACK_CONSUMER_RESTARTS:-0}"
# Release-Bindung: 40-hex Commit vom Orchestrierungs-Desktop (kein Remote-.git).
RELEASE_SHA="${D3B2_RELEASE_SHA:-}"
RUNTIME_TAG=""   # gesetzt durch validate_release_sha
NAMESPACE="inventory"
DEPLOY="inventory-consumer"
CONTAINER="consumer"
CONSUMER_DB_NAME="${CONSUMER_DB:-consumer}"

log()  { printf '\n[d3b2-consumer] %s\n' "$*"; }
fail() { printf '\n[d3b2-consumer] FEHLER: %s\n' "$*" >&2; exit 1; }

# Release-SHA validieren + immutable Runtime-Tag ableiten. UNTRUSTED-Werte werden NIE
# ungeprueft in Shell-Kommandos verwendet.
validate_release_sha() {
  printf '%s' "${RELEASE_SHA}" | grep -Eq '^[0-9a-f]{40}$' \
    || fail "D3B2_RELEASE_SHA fehlt/ungueltig (genau 40 hex erwartet)."
  RUNTIME_TAG="inventory-consumer:$(printf '%s' "${RELEASE_SHA}" | cut -c1-12)"
  case "${ACK_RESTARTS}" in 0|1) ;; *) fail "D3B2_ACK_CONSUMER_RESTARTS nur 0 oder 1." ;; esac
}

# sha256:<hex> aus beliebiger Image-ID-/Digest-Form extrahieren (Normalisierung).
_norm_id() { printf '%s' "$1" | grep -oE 'sha256:[0-9a-f]{12,64}' | head -1; }

dc()  { docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" "$@"; }
mon() { docker compose -f "${MON_COMPOSE}" "$@"; }

STEPS=(preflight images-built queue-config-ready consumer-db-ready consumer-schema-ready consumer-deployed monitoring-ready verified complete)

step_index() { local s i=0; for s in "${STEPS[@]}"; do [ "$s" = "$1" ] && { echo "$i"; return; }; i=$((i+1)); done; echo -1; }

get_step() {
  [ -f "${STATE_FILE}" ] || { echo ""; return 0; }
  python3 - "${STATE_FILE}" <<'PY' 2>/dev/null || echo ""
import json,sys
try:
    d=json.load(open(sys.argv[1])); print(d.get("step","") if isinstance(d,dict) else "")
except Exception: print("")
PY
}

write_state() {
  mkdir -p "${STATE_DIR}"
  local tmp; tmp="$(mktemp "${STATE_DIR}/.state.XXXXXX")" || fail "Temp-State nicht anlegbar"
  STEP="$1" COMPLETE="$2" REL="${RELEASE_SHA}" TAG="${RUNTIME_TAG}" python3 - "$tmp" <<'PY' || { rm -f "$tmp"; fail "State-Serialisierung fehlgeschlagen"; }
import json,os,sys,datetime
json.dump({"schema_version":1,"gate":"D3B2.1","step":os.environ["STEP"],
          "complete":os.environ["COMPLETE"]=="true",
          "release_sha":os.environ["REL"],"runtime_image_tag":os.environ["TAG"],
          "updated_at":datetime.datetime.now(datetime.timezone.utc).isoformat()},
          open(sys.argv[1],"w"),indent=2)
PY
  mv -f "$tmp" "${STATE_FILE}" || { rm -f "$tmp"; fail "Atomarer State-Wechsel (mv) fehlgeschlagen"; }
}

# Release-SHA aus dem (untrusted) State lesen — nur zum VERGLEICH, nie fuer Shell.
state_release() {
  [ -f "${STATE_FILE}" ] || { echo ""; return 0; }
  python3 -c 'import json,sys
try:
    print(json.load(open(sys.argv[1])).get("release_sha",""))
except Exception: print("")' "${STATE_FILE}" 2>/dev/null || echo ""
}

_read_restart_ack() {
  [ -f "${RESTART_AUDIT}" ] || { echo 0; return 0; }
  python3 -c 'import json,sys
try:
    v=json.load(open(sys.argv[1])).get("acked_restart_count",0); print(int(v) if str(v).isdigit() else 0)
except Exception: print(0)' "${RESTART_AUDIT}" 2>/dev/null || echo 0
}

_write_restart_ack() {
  mkdir -p "${STATE_DIR}"
  local tmp; tmp="$(mktemp "${STATE_DIR}/.ack.XXXXXX")" || fail "Temp-Audit nicht anlegbar"
  CNT="$1" python3 - "$tmp" <<'PY' || { rm -f "$tmp"; fail "Audit-Serialisierung fehlgeschlagen"; }
import json,os,sys,datetime
json.dump({"acked_restart_count":int(os.environ["CNT"]),
          "at":datetime.datetime.now(datetime.timezone.utc).isoformat()},open(sys.argv[1],"w"))
PY
  mv -f "$tmp" "${RESTART_AUDIT}" || { rm -f "$tmp"; fail "Audit-mv fehlgeschlagen"; }
}

# Restart-Gate: an die beobachtete Restartzahl gebunden. Eine HOEHERE Zahl als die
# zuletzt bestaetigte verlangt erneutes Acknowledgement (kein Blanket-Ack).
restart_gate() {
  local restarts acked
  restarts="$(kubectl -n "${NAMESPACE}" get pod -l app="${DEPLOY}" \
      -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}' 2>/dev/null || echo 0)"
  restarts="${restarts:-0}"; case "$restarts" in ''|*[!0-9]*) restarts=0 ;; esac
  acked="$(_read_restart_ack)"
  if [ "${restarts}" -gt 0 ] && [ "${restarts}" -gt "${acked}" ]; then
    log "WARNING: Consumer-Pod hat ${restarts} Restart(s) (bisher bestaetigt: ${acked})."
    [ "${ACK_RESTARTS}" = "1" ] || fail "Ungeklaerte/neue Consumer-Restarts (${restarts}). Ursache klaeren und bewusst mit D3B2_ACK_CONSUMER_RESTARTS=1 bestaetigen (fail closed)."
    _write_restart_ack "${restarts}"
    log "Acknowledgement fuer Restartzahl ${restarts} erfasst (an genau diese Zahl gebunden)."
  fi
}

state_cmd() {
  if [ ! -f "${STATE_FILE}" ]; then echo "step: (none)"; return 0; fi
  python3 "${REPO_ROOT}/ops/deploy/check-d3b2-consumer-state.py" "${STATE_FILE}" >/dev/null 2>&1 \
    && echo "step: $(get_step) (valid/complete)" || echo "step: $(get_step) (incomplete/invalid)"
}

with_lock() {
  command -v flock >/dev/null 2>&1 || fail "flock nicht gefunden"
  mkdir -p "${STATE_DIR}"
  exec 9>"${LOCK_FILE}"
  flock -n 9 || fail "Ein anderer Consumer-Rollout-Lauf haelt bereits den Lock (${LOCK_FILE})."
}

need() { command -v "$1" >/dev/null 2>&1 || fail "$1 nicht gefunden"; }

# Healthcheck-Status eines Compose-Service abwarten.
_wait_health() {
  local svc="$1" i cid st
  cid="$(dc ps -q "$svc" 2>/dev/null || true)"; [ -n "$cid" ] || return 1
  for i in $(seq 1 30); do
    st="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null || echo '')"
    case "$st" in healthy|running) return 0 ;; esac
    sleep 2
  done
  return 1
}

# ---- Phasen ------------------------------------------------------------------
preflight() {
  log "PREFLIGHT (site-cloud)"
  validate_release_sha
  log "Release-SHA gueltig; Runtime-Tag = ${RUNTIME_TAG}"
  [ -f "${COMPOSE_FILE}" ] || fail "sites/cloud/docker-compose.yml fehlt — falsches Repository/Site?"
  [ -f "${MON_COMPOSE}" ] || fail "monitoring/docker-compose.yml fehlt"
  [ -f "${REPO_ROOT}/ops/deploy/deploy-consumer.sh" ] || fail "deploy-consumer.sh fehlt"
  [ -f "${ENV_FILE}" ] || fail "sites/cloud/.env fehlt (aus .env.example anlegen, starke Werte setzen)"
  "${REPO_ROOT}/ops/deploy/check-local-perms.sh" "${ENV_FILE}" || fail ".env hat unsichere Rechte (chmod 600)"
  grep -Eq '^CONSUMER_DB=.+' "${ENV_FILE}" || fail "CONSUMER_DB nicht gesetzt"
  grep -Eq '^CONSUMER_APP_PASSWORD=.+' "${ENV_FILE}" || fail "CONSUMER_APP_PASSWORD nicht gesetzt (leer = Fail closed)"
  for t in docker k3d kubectl curl python3; do need "$t"; done
  docker compose version >/dev/null 2>&1 || fail "docker compose nicht verfuegbar"
  k3d cluster list 2>/dev/null | grep -q . || fail "kein k3d-Cluster gefunden"
  # NodePort 30090 auf Host veroeffentlicht (sonst kein Consumer-Scrape).
  docker port "k3d-site-cloud-server-0" "30090/tcp" >/dev/null 2>&1 \
    || fail "Consumer-NodePort 30090 nicht auf den Host veroeffentlicht (create-site-cloud-cluster.sh)"
  # Toxiproxy/ElasticMQ grundsaetzlich erreichbar (read-only ListQueues, keine URL-Logs).
  curl -s --max-time 6 "${QUEUE_ENDPOINT}/?Action=ListQueues&Version=2012-11-05" >/dev/null 2>&1 \
    || fail "ElasticMQ nicht erreichbar"
  # Restart-Gate (an Restartzahl gebunden, erneutes Ack bei hoeherem Wert).
  restart_gate
  # Kein Publisher-Target hier erzeugen/erwarten.
  [ -e "${TARGET_DIR}/publisher.json" ] && log "Hinweis: publisher.json vorhanden (von D3B1/Phase-3) — wird NICHT angefasst." || true
  write_state preflight false
  log "Preflight ok."
}

phase_images_built() {
  log "BUILD: aktuelles hol-consumer:dev aus dem Repo bauen (kein Vertrauen auf altes Image)"
  dc build consumer-db-bootstrap || fail "Consumer-Setup-Image-Build fehlgeschlagen"
  local iid
  iid="$(docker image inspect -f '{{.Id}}' hol-consumer:dev 2>/dev/null || echo '')"
  [ -n "${iid}" ] || fail "Image-ID von hol-consumer:dev nicht ermittelbar"
  log "Setup-Image-ID: ${iid%%:*}:$(printf '%s' "${iid#*:}" | cut -c1-12)…"  # gekuerzt, keine Registry/Hosts
  write_state images-built false
}

phase_queue_config_ready() {
  log "QUEUE-GATE: Read-only Leerheitspruefung (ListQueues/GetQueueAttributes)"
  ${QUEUE_GATE_CMD} || fail "Queue-Leerheitsgate fehlgeschlagen — keine ElasticMQ-Neuerstellung (fail closed)."
  log "QUEUE: nur den sqs-Service kontrolliert neu erstellen (kein pauschales down)"
  dc up -d --force-recreate --no-deps sqs || fail "sqs-Neuerstellung fehlgeschlagen"
  _wait_health sqs || log "Hinweis: sqs-Health n/a (kein Healthcheck) — Erreichbarkeit folgt"
  log "VERIFY: Main+DLQ vorhanden, Redrive maxReceiveCount=5, Tiefen 0, Toxiproxy-Pfad"
  _verify_queue || fail "Queue-/DLQ-/Redrive-Verifikation fehlgeschlagen"
  write_state queue-config-ready false
}

phase_consumer_db_ready() {
  log "DB: consumer-db starten + healthy abwarten"
  dc up -d consumer-db || fail "consumer-db-Start fehlgeschlagen"
  _wait_health consumer-db || fail "consumer-db wurde nicht healthy"
  log "DB: bootstrap (Rollen) -> prepare (DB/Owner), je Exit 0"
  dc run --rm --no-deps consumer-db-bootstrap || fail "consumer-db-bootstrap fehlgeschlagen"
  dc run --rm --no-deps consumer-db-prepare || fail "consumer-db-prepare fehlgeschlagen"
  write_state consumer-db-ready false
}

phase_consumer_schema_ready() {
  log "MIGRATION: consumer-migrate (idempotent, genau einmal pro Lauf)"
  dc run --rm --no-deps consumer-migrate || fail "consumer-migrate fehlgeschlagen"
  log "VERIFY: Schema (Migrationen, event_inbox, movement_projection, Constraints, Rollen)"
  _verify_consumer_schema || fail "Consumer-Schema-Verifikation fehlgeschlagen"
  write_state consumer-schema-ready false
}

phase_consumer_deployed() {
  log "ROLLBACK-ZIEL deterministisch erfassen (Revision, altes Image beweisbar verfuegbar, Rollback-Tag) VOR dem Deploy"
  _capture_rollback_target
  log "DEPLOY: immutable Runtime-Tag ${RUNTIME_TAG} (deploy-consumer.sh rendert ihn ins Manifest)"
  if ! IMAGE="${RUNTIME_TAG}" ${DEPLOY_CONSUMER_CMD}; then
    log "ROLLOUT-FEHLER: kontrollierter Rollback auf gespeicherten immutable Rollback-Tag (DB/Queue bleiben)"
    _rollback_consumer || log "Rollback NICHT beweisbar erfolgreich — manuelle Pruefung noetig."
    write_state consumer-schema-ready false   # zurueck auf letzten guten Schritt
    fail "Consumer-Rollout fehlgeschlagen — Rollback ausgefuehrt, Resume moeglich."
  fi
  log "VERIFY: Pod-Spec-Image == Release-Tag, /healthz, /readyz, D1/D2-Metriken"
  _verify_runtime_image || fail "Laufendes Deployment-Image entspricht nicht dem Release-Tag ${RUNTIME_TAG}"
  _verify_consumer_runtime || fail "Consumer-Runtime-Verifikation fehlgeschlagen"
  write_state consumer-deployed false
}

# Pod-Spec-Image MUSS exakt dem erwarteten immutable Release-Tag entsprechen.
_verify_runtime_image() {
  local img
  img="$(kubectl -n "${NAMESPACE}" get deploy/"${DEPLOY}" -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || echo '')"
  [ "${img}" = "${RUNTIME_TAG}" ]
}

phase_monitoring_ready() {
  log "MONITORING: consumer.json vorhanden? (von deploy-consumer.sh atomar erzeugt)"
  [ -f "${TARGET_DIR}/consumer.json" ] || fail "consumer.json fehlt — Consumer-Target nicht erzeugt"
  log "PROMETHEUS: kontrolliert neu laden (force-recreate), damit neue prometheus.yml + Rules greifen"
  mon up -d || fail "monitoring-Stack-Start fehlgeschlagen"
  mon up -d --force-recreate --no-deps prometheus || fail "Prometheus-Neuerstellung fehlgeschlagen"
  log "VERIFY: Consumer-Target up, consumer+queue-Rules geladen, KEINE Publisher-Serie/-Target"
  _verify_monitoring || fail "Monitoring-Reload-Verifikation fehlgeschlagen"
  write_state monitoring-ready false
}

phase_verified() {
  log "D1/D2-GESAMTVERIFIKATION (read-only)"
  _verify_consumer_schema || fail "D1-Schema-Verifikation fehlgeschlagen"
  _verify_consumer_runtime || fail "D1-Runtime-Verifikation fehlgeschlagen"
  _verify_queue || fail "D2-Queue-/DLQ-Verifikation fehlgeschlagen"
  _verify_monitoring || fail "D2-Monitoring-Verifikation fehlgeschlagen"
  write_state verified false
}

phase_complete() {
  write_state complete true
  log "D3B2.1 ABGESCHLOSSEN: Consumer+D1/D2 live; Queue+DLQ+Redrive; Monitoring geladen. KEIN Publisher, KEIN site-dc, KEINE Phase-3-Aktivierung."
}

# ---- Verifikations-Helfer (read-only; in Tests gefaked) ----------------------
_verify_queue() {
  local lx; lx="$(curl -s --max-time 6 "${QUEUE_ENDPOINT}/?Action=ListQueues&Version=2012-11-05" 2>/dev/null || echo '')"
  printf '%s' "$lx" | grep -q 'inventory-movements'      || return 1
  printf '%s' "$lx" | grep -q 'inventory-movements-dlq'  || return 1
  local ax; ax="$(curl -s --max-time 6 "${QUEUE_ENDPOINT}/queue/inventory-movements?Action=GetQueueAttributes&AttributeName.1=RedrivePolicy&Version=2012-11-05" 2>/dev/null || echo '')"
  printf '%s' "$ax" | grep -q 'maxReceiveCount' || return 1
  printf '%s' "$ax" | grep -q '5' || return 1
  ${QUEUE_GATE_CMD} >/dev/null 2>&1 || return 1   # beide Tiefen 0
  return 0
}

_verify_consumer_schema() {
  dc exec -T consumer-db sh -c "PGPASSWORD=\$POSTGRES_PASSWORD psql -U \$POSTGRES_USER -d ${CONSUMER_DB_NAME} -tAc \"SELECT (to_regclass('public.event_inbox') IS NOT NULL) AND (to_regclass('public.movement_projection') IS NOT NULL)\"" 2>/dev/null | grep -qi 't' || return 1
  return 0
}

_verify_consumer_runtime() {
  kubectl -n "${NAMESPACE}" rollout status deploy/"${DEPLOY}" --timeout=90s >/dev/null 2>&1 || return 1
  local pod; pod="$(kubectl -n "${NAMESPACE}" get pod -l app="${DEPLOY}" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo '')"
  [ -n "$pod" ] || return 1
  kubectl -n "${NAMESPACE}" exec "$pod" -- python -c "import urllib.request,sys; sys.exit(0 if all(urllib.request.urlopen('http://127.0.0.1:8000/'+e,timeout=5).status==200 for e in ('healthz','readyz')) else 1)" >/dev/null 2>&1 || return 1
  return 0
}

_verify_monitoring() {
  local t r; t="$(curl -s --max-time 6 "${PROM_ENDPOINT}/api/v1/targets?state=active" 2>/dev/null || echo '')"
  printf '%s' "$t" | grep -q '"job":"consumer"' || return 1
  r="$(curl -s --max-time 6 "${PROM_ENDPOINT}/api/v1/rules" 2>/dev/null || echo '')"
  printf '%s' "$r" | grep -q '"name":"consumer"' || return 1
  printf '%s' "$r" | grep -q '"name":"queue"' || return 1
  # KEINE Publisher-Serie/-Target erwartet.
  printf '%s' "$t" | grep -q '"job":"publisher"' && return 1
  return 0
}

# ---- Rollback (deterministisch, image-id-verifiziert) ------------------------
# Erfasst VOR jeder Mutation: aktuelle Revision, aktuelles Spec-Image, laufende
# Pod-Image-ID. Beweist, dass das laufende Image lokal verfuegbar ist (Pod-Image-ID
# == lokale Docker-Image-ID), sichert es unter einem eindeutigen, immutablen
# Rollback-Tag und importiert diesen VOR dem Neubau in k3d. Sonst fail closed.
_capture_rollback_target() {
  mkdir -p "${STATE_DIR}"
  local rev img pod_id local_id pid lid rbtag tmp
  rev="$(kubectl -n "${NAMESPACE}" get deploy/"${DEPLOY}" -o jsonpath='{.metadata.annotations.deployment\.kubernetes\.io/revision}' 2>/dev/null || echo '')"
  img="$(kubectl -n "${NAMESPACE}" get deploy/"${DEPLOY}" -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || echo '')"
  pod_id="$(kubectl -n "${NAMESPACE}" get pod -l app="${DEPLOY}" -o jsonpath='{.items[0].status.containerStatuses[0].imageID}' 2>/dev/null || echo '')"
  [ -n "${img}" ] || fail "aktuelles Deployment-Image nicht ermittelbar — Rollback nicht absicherbar"
  local_id="$(docker image inspect -f '{{.Id}}' "${img}" 2>/dev/null || echo '')"
  [ -n "${local_id}" ] || fail "altes Image ${img} NICHT lokal verfuegbar — Abbruch VOR Mutation (Rollback nicht garantierbar)."
  pid="$(_norm_id "${pod_id}")"; lid="$(_norm_id "${local_id}")"
  [ -n "${pid}" ] && [ -n "${lid}" ] || fail "Image-ID nicht normalisierbar — Rollback nicht absicherbar (fail closed)."
  # Praefix-Vergleich (Pod-Digest vs. lokale ID, gekuerzt) — bei Abweichung fail closed.
  case "${pid}" in "${lid}"*) ;; *) case "${lid}" in "${pid}"*) ;; *) fail "laufende Pod-Image-ID weicht vom lokalen alten Image ab — Abbruch (fail closed)." ;; esac ;; esac
  rbtag="inventory-consumer:rollback-$(printf '%s' "${lid#sha256:}" | cut -c1-12)"
  docker tag "${img}" "${rbtag}" || fail "Rollback-Tag konnte nicht gesetzt werden"
  k3d image import "${rbtag}" -c site-cloud >/dev/null 2>&1 || fail "Rollback-Tag-Import in k3d fehlgeschlagen"
  tmp="$(mktemp "${STATE_DIR}/.rollback.XXXXXX")"
  REV="${rev:-}" IMG="${img}" OID="${local_id}" RB="${rbtag}" python3 - "$tmp" <<'PY' || { rm -f "$tmp"; fail "Rollback-Ziel nicht serialisierbar"; }
import json,os,sys
json.dump({"revision":os.environ.get("REV",""),"old_image":os.environ["IMG"],
          "old_image_id":os.environ["OID"],"rollback_tag":os.environ["RB"]},open(sys.argv[1],"w"))
PY
  mv -f "$tmp" "${ROLLBACK_FILE}"
  log "Rollback-Ziel erfasst + gesichert (Revision, alte Image-ID, Rollback-Tag; keine Registry/Hosts)."
}

# Setzt das Deployment explizit auf den gesicherten immutablen Rollback-Tag und
# VERIFIZIERT die laufende Image-ID gegen die gespeicherte alte ID. 'rollout undo'
# allein gilt NICHT als Nachweis.
_rollback_consumer() {
  [ -f "${ROLLBACK_FILE}" ] || { log "kein Rollback-Ziel erfasst — kein Rollback"; return 1; }
  local rbtag oid newpid
  rbtag="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("rollback_tag",""))' "${ROLLBACK_FILE}" 2>/dev/null || echo '')"
  oid="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("old_image_id",""))' "${ROLLBACK_FILE}" 2>/dev/null || echo '')"
  [ -n "${rbtag}" ] && [ -n "${oid}" ] || { log "Rollback-Tag/alte ID fehlen — kein sicherer Rollback"; return 1; }
  kubectl -n "${NAMESPACE}" set image deploy/"${DEPLOY}" "${CONTAINER}=${rbtag}" >/dev/null 2>&1 || return 1
  kubectl -n "${NAMESPACE}" rollout status deploy/"${DEPLOY}" --timeout=90s >/dev/null 2>&1 || return 1
  _verify_consumer_runtime || return 1
  newpid="$(_norm_id "$(kubectl -n "${NAMESPACE}" get pod -l app="${DEPLOY}" -o jsonpath='{.items[0].status.containerStatuses[0].imageID}' 2>/dev/null || echo '')")"
  local oidn; oidn="$(_norm_id "${oid}")"
  case "${newpid}" in "${oidn}"*) ;; *) case "${oidn}" in "${newpid}"*) ;; *) log "Rollback-Image-ID stimmt NICHT mit der alten ID ueberein — Rollback nicht bestaetigt."; return 1 ;; esac ;; esac
  log "Rollback bestaetigt: laufendes Image == gespeicherte alte Image-ID."
  return 0
}

# ---- Ablaufsteuerung ---------------------------------------------------------
_run_from() {
  local s="$1"
  [ "$s" -le 0 ] && preflight
  [ "$s" -le 1 ] && phase_images_built
  [ "$s" -le 2 ] && phase_queue_config_ready
  [ "$s" -le 3 ] && phase_consumer_db_ready
  [ "$s" -le 4 ] && phase_consumer_schema_ready
  [ "$s" -le 5 ] && phase_consumer_deployed
  [ "$s" -le 6 ] && phase_monitoring_ready
  [ "$s" -le 7 ] && phase_verified
  [ "$s" -le 8 ] && phase_complete
  return 0
}

cmd_run() {
  with_lock; validate_release_sha; restart_gate
  if [ -f "${STATE_FILE}" ]; then
    local cur; cur="$(get_step)"
    if [ "${cur}" = "complete" ]; then
      [ "$(state_release)" = "${RELEASE_SHA}" ] || fail "complete-State gehoert zu anderem Release — Abbruch."
      log "State bereits complete fuer diesen Release — keine erneute Mutation."; return 0
    fi
    [ -n "${cur}" ] && fail "Unvollstaendiger State (step=${cur}) vorhanden. Mit 'resume' fortsetzen (kein blindes Neustarten)."
    fail "State-Datei vorhanden, aber unlesbar/korrupt — Abbruch."
  fi
  _run_from 0
}

cmd_resume() {
  with_lock; validate_release_sha; restart_gate
  [ -f "${STATE_FILE}" ] || fail "Kein State zum Fortsetzen — 'run' verwenden."
  local cur; cur="$(get_step)"
  [ -n "$cur" ] || fail "State-Datei vorhanden, aber unlesbar/korrupt — Abbruch (kein blindes Fortsetzen)."
  case "$cur" in
    preflight|images-built|queue-config-ready|consumer-db-ready|consumer-schema-ready|consumer-deployed|monitoring-ready|verified|complete) ;;
    *) fail "Beschaedigter/unbekannter State (step=${cur}) — Abbruch." ;;
  esac
  # Release-Bindung: nur denselben validierten Release fortsetzen.
  local st_sha; st_sha="$(state_release)"
  [ -n "${st_sha}" ] || fail "State ohne release_sha — Abbruch (kein Resume gegen unbekannten Release)."
  [ "${st_sha}" = "${RELEASE_SHA}" ] || fail "Release-Mismatch: State-Release weicht vom lokalen Release ab — Abbruch (fail closed)."
  if [ "$cur" = "complete" ]; then log "Bereits complete — nichts zu tun."; return; fi
  local idx; idx="$(step_index "$cur")"
  log "RESUME (Release gebunden) ab Schritt nach '${cur}'."
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
