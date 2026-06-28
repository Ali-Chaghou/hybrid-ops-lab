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
# Beschraenkte, read-only Queue-Readiness-Wartung: ElasticMQ braucht nach 'running'
# einige Sekunden, bis REST-API + deklarative Queues bereit sind. Defaults 30x1s;
# fuer Tests ueberschreibbar (Werte werden vor Verwendung validiert).
QUEUE_READY_ATTEMPTS="${D3B2_QUEUE_READY_ATTEMPTS:-30}"
QUEUE_READY_INTERVAL="${D3B2_QUEUE_READY_INTERVAL:-1}"
# Beschraenkte, read-only Runtime-Verifikations-Wartung: nach erfolgreichem rollout
# kann die exec-Bereitschaft des neuen Pods minimal nachlaufen und ein alter Pod kann
# kurz weiter terminieren. Defaults 30x1s; fuer Tests ueberschreibbar (vor Verwendung
# validiert). KEINE Mutation in der Schleife.
RUNTIME_VERIFY_ATTEMPTS="${D3B2_RUNTIME_VERIFY_ATTEMPTS:-30}"
RUNTIME_VERIFY_INTERVAL="${D3B2_RUNTIME_VERIFY_INTERVAL:-1}"
# HARTES Gesamtzeitbudget (monotone Deadline ueber SECONDS) fuer die Runtime-Verify-
# Retries. Begrenzt die Gesamtdauer auch dann, wenn einzelne read-only HTTP-/exec-
# Aufrufe (je bis ~5s) die naive Rechnung 30x1s sprengen wuerden. Default ~30s; die
# Versuchszahl bleibt als zusaetzliche obere Schranke (was zuerst greift, stoppt).
RUNTIME_VERIFY_BUDGET="${D3B2_RUNTIME_VERIFY_BUDGET_SECONDS:-30}"
# Beschraenkte, read-only Monitoring-Readiness-Wartung NACH dem einmaligen Prometheus-
# Force-Recreate: Docker 'Started' beweist NICHT, dass HTTP-API, Target-Discovery und
# Rule-Loading bereit sind (Recreate liefert kurzzeitig 0/0 Targets). Defaults 60x2s,
# Budget ~90s; fuer Tests ueberschreibbar (vor Verwendung im Preflight validiert).
# KEINE Mutation in der Schleife.
MONITORING_VERIFY_ATTEMPTS="${D3B2_MONITORING_VERIFY_ATTEMPTS:-60}"
MONITORING_VERIFY_INTERVAL="${D3B2_MONITORING_VERIFY_INTERVAL:-2}"
MONITORING_VERIFY_BUDGET="${D3B2_MONITORING_VERIFY_BUDGET_SECONDS:-90}"
# Bewusstes Operator-Acknowledgement fuer bestehende Consumer-Restarts (kein Secret).
ACK_RESTARTS="${D3B2_ACK_CONSUMER_RESTARTS:-0}"
# Release-Bindung: 40-hex Commit vom Orchestrierungs-Desktop (kein Remote-.git).
RELEASE_SHA="${D3B2_RELEASE_SHA:-}"
RUNTIME_TAG=""   # gesetzt durch validate_release_sha
NAMESPACE="inventory"
DEPLOY="inventory-consumer"
CONTAINER="consumer"
CONSUMER_DB_NAME="${CONSUMER_DB:-consumer}"
# k3d-Server-Node = Quelle der Wahrheit fuer laufende Pod-Images (containerd/CRI).
# Ueberschreibbar fuer Tests, aber Name wird gegen ein enges Muster validiert.
K3D_NODE="${D3B2_K3D_NODE:-k3d-site-cloud-server-0}"
# NodePort-Publikation: k3d veroeffentlicht den Host-Port NICHT auf dem Server-Node,
# sondern auf dem generierten Server-Loadbalancer. Nur fuer den Publikations-Check
# (NICHT fuer CRI/containerd) genutzt. Getrennte, validierte Variable; kein Fallback.
K3D_PORT_OWNER="${D3B2_K3D_PORT_OWNER:-k3d-site-cloud-serverlb}"
CRI_ID_HELPER="${REPO_ROOT}/ops/deploy/cri-image-identity.py"
# Deterministische, read-only Pod-Auswahl (ersetzt nicht deterministisches .items[0]).
# Pflichtabhaengigkeit des Controllers; Pfad fuer Tests ueberschreibbar, Existenz/
# Ladbarkeit werden VOR State-Schreiben/Mutation fail-closed geprueft.
POD_SELECT_HELPER="${D3B2_POD_SELECT_HELPER:-${REPO_ROOT}/ops/deploy/select-consumer-pod.py}"
# Der k3d-Node nutzt die EIGENSTAENDIGEN Binaries crictl/ctr (NICHT 'k3s crictl' /
# 'k3s ctr' — die liefern im k3d-Node keine verwertbare Ausgabe). Feste, im Code
# definierte Kandidaten-Pfade; per Muster validiert; Funktion explizit geprueft.
_CRICTL_CANDIDATES="/bin/crictl /usr/bin/crictl /usr/local/bin/crictl"
_CTR_CANDIDATES="/bin/ctr /usr/bin/ctr /usr/local/bin/ctr"
CRICTL_BIN=""   # gesetzt durch detect_runtime_tools (fail closed)
CTR_BIN=""

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

# Fail-closed Erkennung der eigenstaendigen Runtime-Binaries im k3d-Node. Prueft
# Node-Erreichbarkeit, Existenz/Ausfuehrbarkeit + Funktion von crictl und ctr
# (inkl. k8s.io-Lesbarkeit und Tag-Subkommando). KEINE Installation, KEIN stiller
# Rueckfall auf 'k3s crictl'/'k3s ctr'. Keine Image-Liste/Registry im Log.
detect_runtime_tools() {
  [ -n "${CRICTL_BIN}" ] && [ -n "${CTR_BIN}" ] && return 0   # idempotent
  printf '%s' "${K3D_NODE}" | grep -Eq '^[A-Za-z0-9._-]+$' || fail "ungueltiger k3d-Node-Name."
  docker inspect -f '{{.State.Status}}' "${K3D_NODE}" >/dev/null 2>&1 \
    || fail "k3d-Server-Node nicht vorhanden/erreichbar."
  local c
  for c in ${_CRICTL_CANDIDATES}; do
    printf '%s' "$c" | grep -Eq '^(/[A-Za-z0-9._-]+)+$' || continue
    if docker exec "${K3D_NODE}" test -x "$c" >/dev/null 2>&1 \
       && docker exec "${K3D_NODE}" "$c" inspecti --help >/dev/null 2>&1; then CRICTL_BIN="$c"; break; fi
  done
  [ -n "${CRICTL_BIN}" ] || fail "kein funktionsfaehiges crictl im k3d-Node (kein Rueckfall auf 'k3s crictl')."
  for c in ${_CTR_CANDIDATES}; do
    printf '%s' "$c" | grep -Eq '^(/[A-Za-z0-9._-]+)+$' || continue
    # k8s.io lesbar UND 'images tag'-Subkommando vorhanden. Harmlose Help-Probe
    # (Exit 0); KEINE Image-Referenzen, erzeugt KEINEN Tag, keine Mutation. (Der
    # No-Argument-Aufruf endet non-zero mit Usage und ist als Probe ungeeignet.)
    if docker exec "${K3D_NODE}" test -x "$c" >/dev/null 2>&1 \
       && docker exec "${K3D_NODE}" "$c" -n k8s.io images ls -q >/dev/null 2>&1 \
       && docker exec "${K3D_NODE}" "$c" -n k8s.io images tag --help >/dev/null 2>&1; then CTR_BIN="$c"; break; fi
  done
  [ -n "${CTR_BIN}" ] || fail "kein funktionsfaehiges ctr (k8s.io + tag) im k3d-Node (kein Rueckfall auf 'k3s ctr')."
  log "Runtime-Tools erkannt (crictl/ctr im k3d-Node; keine 'k3s'-Wrapper)."
}

_ensure_tools() { detect_runtime_tools; }

# CRI/containerd-Operationen im k3d-Node (Source of Truth fuer laufende Pod-Images).
# Feste Argumentstrukturen, getrennte Argumente, kein eval, nie eine ganze
# Befehlszeile aus State/Env. Eigenstaendige crictl/ctr-Binaries (kein 'k3s ...').
# cri_inspect ist read-only. KEIN '|| true' mehr — Fehler werden nicht als leere Ausgabe
# maskiert. Innerhalb einer aktiven Verify-Deadline laeuft der docker/crictl-Aufruf ueber
# den zentralen Wrapper _dl (timeout + kill-after). Getrennte, kontrollierte Codes:
#   0            + CRI-JSON auf stdout            -> Erfolg
#   124/137                                       -> Deadline-Timeout
#   125/126/127                                   -> timeout-/Tool-/Ausfuehrungsfehler (fatal)
#   ${_RC_CRI_EMPTY}                              -> Exit 0, aber leere Ausgabe
#   sonstiger !=0                                 -> docker/crictl-Transportfehler
# (stderr wird verworfen: keine Registry/Hosts/Secret-Leaks in Logs.)
_RC_CRI_EMPTY=12
cri_inspect() {
  _ensure_tools
  local out rc=0
  if [ -n "${_VERIFY_DEADLINE}" ]; then
    out="$(_dl docker exec "${K3D_NODE}" "${CRICTL_BIN}" inspecti -o json "$1" 2>/dev/null)" || rc=$?
  else
    out="$(docker exec "${K3D_NODE}" "${CRICTL_BIN}" inspecti -o json "$1" 2>/dev/null)" || rc=$?
  fi
  [ "${rc}" -ne 0 ] && return "${rc}"
  [ -n "$(printf '%s' "${out}" | tr -d '[:space:]')" ] || return "${_RC_CRI_EMPTY}"
  printf '%s' "${out}"
  return 0
}
ctr_tag()     { _ensure_tools; docker exec "${K3D_NODE}" "${CTR_BIN}" -n k8s.io images tag "$1" "$2"; }

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

# ---- Gemeinsames, monotones Gesamtzeitbudget der Runtime-Verifikation -------
# EINE absolute Deadline (SECONDS-basiert, monoton) gilt fuer die GESAMTE Sequenz aus
# Spec-Image-Check, Release-Pod-Selektion, CRI-Identitaet, Health und Readiness —
# nicht je Teilfunktion separat. ALLE externen Prozesse (kubectl, docker/crictl, beide
# Python-Helper, Identity-Parsing, exec) laufen ueber den EINEN Wrapper _dl; Sleeps sind
# auf die Restzeit gedeckelt. Kein Versuch nach Ablauf.
_VERIFY_DEADLINE=""
# Optionales Argument = Budget in Sekunden (Default RUNTIME_VERIFY_BUDGET). So teilen
# sich Runtime- und Monitoring-Verifikation dieselbe Deadline-/_dl-Maschinerie, jeweils
# mit GENAU EINER monotonen Gesamtdeadline pro Aufruf.
_begin_verify_deadline() { _VERIFY_DEADLINE=$(( SECONDS + ${1:-RUNTIME_VERIFY_BUDGET} )); }
_clear_verify_deadline() { _VERIFY_DEADLINE=""; }
_verify_remaining() {
  local r=$(( _VERIFY_DEADLINE - SECONDS )); [ "${r}" -lt 0 ] && r=0; printf '%s' "${r}"
}
_verify_deadline_reached() { [ -n "${_VERIFY_DEADLINE}" ] && [ "${SECONDS}" -ge "${_VERIFY_DEADLINE}" ]; }
_min() { if [ "$1" -le "$2" ]; then printf '%s' "$1"; else printf '%s' "$2"; fi; }

# Kurze Gnadenfrist nach TERM, bis hart per KILL beendet wird. Verhindert, dass ein
# Prozess, der TERM ignoriert (trap '' TERM), nach Ablauf der Deadline unbeschraenkt
# weiterlaeuft. Fuer Tests ueberschreibbar.
_KILL_AFTER="${D3B2_VERIFY_KILL_AFTER:-1}"

# ZENTRALER Deadline-Wrapper: fuehrt GENAU EINEN read-only Prozess gebunden an die
# verbleibende gemeinsame Deadline aus. `timeout --kill-after` sendet erst TERM und nach
# der Gnadenfrist KILL. Keine verstreuten direkten `timeout`-Aufrufe im Verify-Pfad.
# Exit-Klassen: 124 (TERM-Timeout) und 137 (KILL nach kill-after) = Deadline-Timeout;
# 125 (timeout-interner Fehler), 126/127 (nicht ausfuehrbar/ nicht gefunden) = sofort
# fatale Tool-/Ausfuehrungsfehler; sonst der echte Befehls-Exit-Code.
_dl() {
  local rem; rem="$(_verify_remaining)"
  [ "${rem}" -ge 1 ] || return 124
  timeout --kill-after="${_KILL_AFTER}s" "${rem}s" "$@"
}
# Exit-Code-Klassifizierer fuer deadlinegebundene Prozesse.
_is_timeout_rc() { case "$1" in 124|137) return 0 ;; *) return 1 ;; esac; }
_is_toolerr_rc() { case "$1" in 125|126|127) return 0 ;; *) return 1 ;; esac; }

# Prueft FUNKTIONAL, dass `timeout --kill-after` unterstuetzt wird (nicht nur, dass ein
# Befehl 'timeout' existiert): ein TERM-ignorierender Prozess muss per KILL (Exit 137)
# innerhalb der Gnadenfrist enden. Fail closed, sonst koennte ein blockierter Prozess
# trotz Deadline unbeschraenkt weiterlaufen. Idempotent.
_KILL_AFTER_OK=""
_ensure_kill_after() {
  [ -n "${_KILL_AFTER_OK}" ] && return 0
  command -v timeout >/dev/null 2>&1 || fail "timeout nicht gefunden"
  timeout --kill-after=1s 1s true >/dev/null 2>&1 \
    || fail "timeout unterstuetzt --kill-after nicht (GNU coreutils noetig) — fail closed."
  local rc=0
  timeout --kill-after=1s 1s bash -c 'trap "" TERM; sleep 30' >/dev/null 2>&1 || rc=$?
  [ "${rc}" = "137" ] \
    || fail "timeout --kill-after beendet TERM-ignorierende Prozesse nicht hart (rc=${rc}) — fail closed."
  _KILL_AFTER_OK=1
}

# ---- Runtime-Verify-Konfiguration: gemeinsame, idempotente Validierung ------
# Obergrenzen bewusst konservativ, damit eine Fehlkonfiguration nicht zu extremen
# Laufzeiten/Last fuehrt: ATTEMPTS<=1000 (Schleifen-Backstop), INTERVAL<=60s (kein
# Minuten-Sleep pro Versuch), BUDGET<=600s (10-Minuten-Hard-Cap; Default 30s).
_RV_MAX_ATTEMPTS=1000
_RV_MAX_INTERVAL=60
_RV_MAX_BUDGET=600
_RV_CONFIG_OK=""
_validate_runtime_verify_config() {
  [ -n "${_RV_CONFIG_OK}" ] && return 0
  printf '%s' "${RUNTIME_VERIFY_ATTEMPTS}" | grep -Eq '^[0-9]+$' || fail "RUNTIME_VERIFY_ATTEMPTS: Ganzzahl >= 1 erwartet."
  printf '%s' "${RUNTIME_VERIFY_INTERVAL}" | grep -Eq '^[0-9]+$' || fail "RUNTIME_VERIFY_INTERVAL: Ganzzahl >= 0 erwartet."
  printf '%s' "${RUNTIME_VERIFY_BUDGET}"   | grep -Eq '^[0-9]+$' || fail "RUNTIME_VERIFY_BUDGET: Ganzzahl >= 1 erwartet."
  { [ "${RUNTIME_VERIFY_ATTEMPTS}" -ge 1 ] && [ "${RUNTIME_VERIFY_ATTEMPTS}" -le "${_RV_MAX_ATTEMPTS}" ]; } \
    || fail "RUNTIME_VERIFY_ATTEMPTS muss 1..${_RV_MAX_ATTEMPTS} sein."
  { [ "${RUNTIME_VERIFY_INTERVAL}" -ge 0 ] && [ "${RUNTIME_VERIFY_INTERVAL}" -le "${_RV_MAX_INTERVAL}" ]; } \
    || fail "RUNTIME_VERIFY_INTERVAL muss 0..${_RV_MAX_INTERVAL} sein."
  { [ "${RUNTIME_VERIFY_BUDGET}" -ge 1 ] && [ "${RUNTIME_VERIFY_BUDGET}" -le "${_RV_MAX_BUDGET}" ]; } \
    || fail "RUNTIME_VERIFY_BUDGET muss 1..${_RV_MAX_BUDGET} sein."
  _RV_CONFIG_OK=1
}

# Monitoring-Verify-Konfiguration: gleiche konservativen Obergrenzen wie oben. Wird im
# Preflight VOR State-Schreiben/Mutation validiert; idempotent.
_MON_CONFIG_OK=""
_validate_monitoring_verify_config() {
  [ -n "${_MON_CONFIG_OK}" ] && return 0
  printf '%s' "${MONITORING_VERIFY_ATTEMPTS}" | grep -Eq '^[0-9]+$' || fail "MONITORING_VERIFY_ATTEMPTS: Ganzzahl >= 1 erwartet."
  printf '%s' "${MONITORING_VERIFY_INTERVAL}" | grep -Eq '^[0-9]+$' || fail "MONITORING_VERIFY_INTERVAL: Ganzzahl >= 0 erwartet."
  printf '%s' "${MONITORING_VERIFY_BUDGET}"   | grep -Eq '^[0-9]+$' || fail "MONITORING_VERIFY_BUDGET: Ganzzahl >= 1 erwartet."
  { [ "${MONITORING_VERIFY_ATTEMPTS}" -ge 1 ] && [ "${MONITORING_VERIFY_ATTEMPTS}" -le "${_RV_MAX_ATTEMPTS}" ]; } \
    || fail "MONITORING_VERIFY_ATTEMPTS muss 1..${_RV_MAX_ATTEMPTS} sein."
  { [ "${MONITORING_VERIFY_INTERVAL}" -ge 0 ] && [ "${MONITORING_VERIFY_INTERVAL}" -le "${_RV_MAX_INTERVAL}" ]; } \
    || fail "MONITORING_VERIFY_INTERVAL muss 0..${_RV_MAX_INTERVAL} sein."
  { [ "${MONITORING_VERIFY_BUDGET}" -ge 1 ] && [ "${MONITORING_VERIFY_BUDGET}" -le "${_RV_MAX_BUDGET}" ]; } \
    || fail "MONITORING_VERIFY_BUDGET muss 1..${_RV_MAX_BUDGET} sein."
  _MON_CONFIG_OK=1
}

# Fail-closed Pruefung der Pflichtabhaengigkeit select-consumer-pod.py VOR jeder
# State-Schreib-/Mutationsoperation: Datei vorhanden, regulaer + lesbar, von Python
# ladbar und der Selektor grundsaetzlich aufrufbar (--help laedt das Modul + argparse,
# liest aber kein stdin und mutiert nichts). Ein fehlender/defekter Helper darf NIE
# als 'kein Pod'/'0 Restarts' durchrutschen. Idempotent.
_POD_SELECTOR_OK=""
_ensure_pod_selector() {
  [ -n "${_POD_SELECTOR_OK}" ] && return 0
  [ -e "${POD_SELECT_HELPER}" ] || fail "Pod-Selektor fehlt: ${POD_SELECT_HELPER} (Pflichtabhaengigkeit) — fail closed."
  [ -f "${POD_SELECT_HELPER}" ] || fail "Pod-Selektor ist keine regulaere Datei — fail closed."
  [ -r "${POD_SELECT_HELPER}" ] || fail "Pod-Selektor nicht lesbar — fail closed."
  python3 "${POD_SELECT_HELPER}" --help >/dev/null 2>&1 \
    || fail "Pod-Selektor nicht ladbar/aufrufbar (Syntax/Argparse defekt) — fail closed."
  _POD_SELECTOR_OK=1
}

# Gemeinsame, idempotente, READ-ONLY Voraussetzungspruefung fuer JEDE Verifikation
# (Runtime + Monitoring). MUSS vor jeder State-Schreib-/Mutationsoperation laufen — im
# frischen `preflight` UND im `resume`. Sonst koennte ein Resume ab consumer-deployed
# bereits `mon up -d`/Prometheus-Force-Recreate ausfuehren, bevor _verify_monitoring_ready
# eine ungueltige Monitoring-Konfiguration oder fehlendes `timeout --kill-after` bemerkt.
# Keine Mutation, kein State-Schreiben; alle Teilpruefungen sind selbst idempotent.
_validate_verify_prerequisites() {
  _ensure_pod_selector
  _validate_runtime_verify_config
  _validate_monitoring_verify_config
  need curl
  need python3
  need timeout
  _ensure_kill_after
}

# Restart-Gate: an die beobachtete Restartzahl gebunden. Eine HOEHERE Zahl als die
# zuletzt bestaetigte verlangt erneutes Acknowledgement (kein Blanket-Ack). Fail closed
# bei kubectl-/JSON-/Helper-Fehler: ein Fehler wird NIE als '0 Restarts' interpretiert.
# Bewertet die HOECHSTE Restartzahl ueber ALLE nicht terminierenden Consumer-Pods
# (nicht nur den juengsten), damit kein Pod mit Restarts verborgen wird.
restart_gate() {
  _ensure_pod_selector
  _clear_verify_deadline   # Restart-Gate laeuft ausserhalb der Verify-Deadline (unbeschraenkt)
  local restarts rc acked
  # Aggregat-Modus: gueltige, leere (bzw. nur-terminierende) Pod-Menge -> '0' mit Exit 0;
  # kubectl-/JSON-/Helper-Fehler -> Exit != 0 -> fail closed (KEIN '|| echo 0').
  if restarts="$(_select_pod --print max-restart-count)"; then rc=0; else rc=$?; fi
  [ "${rc}" -eq 0 ] || fail "Restart-Gate: Consumer-Restarts nicht ermittelbar (kubectl/JSON/Helper-Fehler, exit=${rc}) — fail closed."
  case "${restarts}" in ''|*[!0-9]*) fail "Restart-Gate: ungueltige Restartausgabe — fail closed." ;; esac
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
  # Gemeinsame Voraussetzungen (Pod-Selektor, Runtime-/Monitoring-Konfig, curl/python3/
  # timeout, funktionales kill-after) VOR State-Schreiben/Mutation pruefen.
  _validate_verify_prerequisites
  [ -f "${ENV_FILE}" ] || fail "sites/cloud/.env fehlt (aus .env.example anlegen, starke Werte setzen)"
  "${REPO_ROOT}/ops/deploy/check-local-perms.sh" "${ENV_FILE}" || fail ".env hat unsichere Rechte (chmod 600)"
  grep -Eq '^CONSUMER_DB=.+' "${ENV_FILE}" || fail "CONSUMER_DB nicht gesetzt"
  grep -Eq '^CONSUMER_APP_PASSWORD=.+' "${ENV_FILE}" || fail "CONSUMER_APP_PASSWORD nicht gesetzt (leer = Fail closed)"
  for t in docker k3d kubectl curl python3 timeout; do need "$t"; done
  docker compose version >/dev/null 2>&1 || fail "docker compose nicht verfuegbar"
  k3d cluster list 2>/dev/null | grep -q . || fail "kein k3d-Cluster gefunden"
  # Runtime-Tool-Gate VOR State-Schreiben/Mutation: crictl/ctr im k3d-Node funktionsfaehig.
  detect_runtime_tools
  # NodePort 30090 auf Host veroeffentlicht — k3d publiziert ihn ueber den generierten
  # Server-Loadbalancer (K3D_PORT_OWNER), NICHT ueber den Server-Node. Kein Fallback.
  printf '%s' "${K3D_PORT_OWNER}" | grep -Eq '^[A-Za-z0-9._-]+$' \
    || fail "ungueltiger Port-Owner-Containername."
  docker inspect "${K3D_PORT_OWNER}" >/dev/null 2>&1 \
    || fail "Port-Owner-Container ${K3D_PORT_OWNER} nicht gefunden/erreichbar."
  docker port "${K3D_PORT_OWNER}" "30090/tcp" >/dev/null 2>&1 \
    || fail "Consumer-NodePort 30090 nicht auf den Host veroeffentlicht (erwartet auf ${K3D_PORT_OWNER}; create-site-cloud-cluster.sh)"
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

# Beschraenkte, read-only Wartung bis ElasticMQ bereit ist: ruft NUR das vorhandene
# _verify_queue() (ListQueues/GetQueueAttributes + Leerheitsgate) auf — keine
# Mutation (kein create/purge/send/receive/redrive). Fail closed nach Timeout.
_wait_queue_ready() {
  local n="${QUEUE_READY_ATTEMPTS}" iv="${QUEUE_READY_INTERVAL}" i
  printf '%s' "${n}"  | grep -Eq '^[1-9][0-9]*$' || fail "ungueltiges QUEUE_READY_ATTEMPTS (positive Ganzzahl erwartet)."
  printf '%s' "${iv}" | grep -Eq '^[0-9]+$'      || fail "ungueltiges QUEUE_READY_INTERVAL (Ganzzahl >= 0 erwartet)."
  for i in $(seq 1 "${n}"); do
    if _verify_queue; then
      log "ElasticMQ bereit (Versuch ${i}/${n}): Main+DLQ vorhanden, Redrive ok, Tiefen 0."
      return 0
    fi
    [ "${i}" -lt "${n}" ] && sleep "${iv}"
  done
  fail "ElasticMQ wurde nach ${n} Versuchen nicht bereit (read-only Queue-/DLQ-/Redrive-Verifikation) — fail closed."
}

phase_queue_config_ready() {
  log "QUEUE-GATE: Read-only Leerheitspruefung (ListQueues/GetQueueAttributes)"
  ${QUEUE_GATE_CMD} || fail "Queue-Leerheitsgate fehlgeschlagen — keine ElasticMQ-Neuerstellung (fail closed)."
  log "QUEUE: nur den sqs-Service GENAU EINMAL kontrolliert neu erstellen (kein pauschales down)"
  dc up -d --force-recreate --no-deps sqs || fail "sqs-Neuerstellung fehlgeschlagen"
  _wait_health sqs || log "Hinweis: sqs-Health n/a (kein Healthcheck) — Readiness folgt read-only."
  log "VERIFY: warte beschraenkt+read-only auf Main+DLQ, Redrive maxReceiveCount=5, Tiefen 0, Toxiproxy-Pfad"
  _wait_queue_ready
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
  if ! IMAGE="${RUNTIME_TAG}" K3D_PORT_OWNER="${K3D_PORT_OWNER}" ${DEPLOY_CONSUMER_CMD}; then
    log "ROLLOUT-FEHLER: kontrollierter Rollback auf gespeicherten immutable Rollback-Tag (DB/Queue bleiben)"
    _rollback_consumer || log "Rollback NICHT beweisbar erfolgreich — manuelle Pruefung noetig."
    write_state consumer-schema-ready false   # zurueck auf letzten guten Schritt
    fail "Consumer-Rollout fehlgeschlagen — Rollback ausgefuehrt, Resume moeglich."
  fi
  log "VERIFY: Spec-Image, Release-Tag im CRI-Store, laufende CRI-Identitaet == Release-Tag, /healthz, /readyz — EIN gemeinsames Zeitbudget"
  _verify_consumer_runtime || fail "Consumer-Runtime-Verifikation fehlgeschlagen"
  write_state consumer-deployed false
}

phase_monitoring_ready() {
  log "MONITORING: consumer.json vorhanden? (von deploy-consumer.sh atomar erzeugt)"
  [ -f "${TARGET_DIR}/consumer.json" ] || fail "consumer.json fehlt — Consumer-Target nicht erzeugt"
  log "PROMETHEUS: Stack starten + Prometheus GENAU EINMAL force-recreaten (neue prometheus.yml + Rules)"
  mon up -d || fail "monitoring-Stack-Start fehlgeschlagen"
  # Force-Recreate GENAU EINMAL pro Phaseninvocation — VOR und AUSSERHALB des Waiters.
  mon up -d --force-recreate --no-deps prometheus || fail "Prometheus-Neuerstellung fehlgeschlagen"
  log "VERIFY: beschraenkte read-only Prometheus-Readiness + Targets/Rules (fail closed, KEINE Mutation)"
  _verify_monitoring_ready || fail "Monitoring-Readiness-Verifikation fehlgeschlagen — fail closed"
  write_state monitoring-ready false
}

phase_verified() {
  log "D1/D2-GESAMTVERIFIKATION (read-only)"
  _verify_consumer_schema || fail "D1-Schema-Verifikation fehlgeschlagen"
  _verify_consumer_runtime || fail "D1-Runtime-Verifikation fehlgeschlagen"
  _verify_queue || fail "D2-Queue-/DLQ-Verifikation fehlgeschlagen"
  # Dieselbe sichere, read-only Monitoring-Verifikation — OHNE erneuten Force-Recreate.
  _verify_monitoring_ready || fail "D2-Monitoring-Verifikation fehlgeschlagen"
  write_state verified false
}

phase_complete() {
  write_state complete true
  log "D3B2.1 ABGESCHLOSSEN: Consumer+D1/D2 live; Queue+DLQ+Redrive; Monitoring geladen. KEIN Publisher, KEIN site-dc, KEINE Phase-3-Aktivierung."
}

# ---- Deterministische, read-only Pod-Auswahl --------------------------------
# Liest die Pod-Liste (-o json) kontrolliert in eine Variable und uebergibt sie DANACH
# an den Python-Helper — KEINE gemischte Pipeline. So bleibt der kubectl-Fehler vom
# Helper-Ergebnis unterscheidbar. Innerhalb einer aktiven Verify-Deadline laufen BEIDE
# Prozesse (kubectl get UND der Selektor-Helper) ueber den zentralen Wrapper _dl, sodass
# auch ein blockierender Selektor durch das gemeinsame Budget hart beendet wird.
# Rueckgabecodes:
#   0  Erfolg (Feld auf stdout)
#   3  valide Pod-Liste, aber kein Kandidat (retryfaehig)
#   2  Helper-Input-/Strukturfehler (fatal)         4  ungueltige Helper-Argumente (fatal)
#   124/137  Deadline-Timeout (kubectl oder Helper)  125/126/127 Tool-Fehler (fatal)
#   ${_RC_KUBECTL}  kubectl-Fehler (fatal)
_RC_KUBECTL=10
_select_pod() {
  local json rc=0
  if [ -n "${_VERIFY_DEADLINE}" ]; then
    json="$(_dl kubectl -n "${NAMESPACE}" get pod -l app="${DEPLOY}" -o json 2>/dev/null)" || rc=$?
    if [ "${rc}" -ne 0 ]; then _is_timeout_rc "${rc}" && return "${rc}"; return "${_RC_KUBECTL}"; fi
    printf '%s' "${json}" | _dl python3 "${POD_SELECT_HELPER}" "$@"
  else
    json="$(kubectl -n "${NAMESPACE}" get pod -l app="${DEPLOY}" -o json 2>/dev/null)" \
      || return "${_RC_KUBECTL}"
    printf '%s' "${json}" | python3 "${POD_SELECT_HELPER}" "$@"
  fi
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

# Secret-freie Diagnose: Teilschritt, gewaehlter Pod (falls vorhanden), Versuch,
# Exit-Code, Gesamtbudget, verstrichene + verbleibende Zeit und Abbruchgrund. KEINE
# Rohdaten/Secrets/Pod-JSON.
_verify_diag() {
  local sub="$1" pod="$2" att="$3" n="$4" ec="$5" start="$6" mode="${7:-deadline/attempts}"
  log "RUNTIME-VERIFY: nicht bestaetigt [substep=${sub}] [pod=${pod:-<keiner>}] [versuch=${att}/${n}] [exit=${ec}] [budget=${RUNTIME_VERIFY_BUDGET}s] [elapsed=$(( SECONDS - start ))s] [remaining=$(_verify_remaining)s] [abbruch=${mode}]"
}

# Oeffentlicher Einstieg: setzt GENAU EINE gemeinsame Deadline, ruft die Sequenz und
# raeumt _VERIFY_DEADLINE auf JEDEM Return-Pfad (Erfolg, Spec-Fehler, fataler Fehler,
# Timeout, Retry-Timeout) wieder auf — ohne globale Traps zu beruehren.
# Args: [expect_image=RUNTIME_TAG] [cri_ref=RUNTIME_TAG] [expect_rid=""]
_verify_consumer_runtime() {
  _validate_runtime_verify_config   # defensiv (Erstvalidierung erfolgt im Preflight)
  _begin_verify_deadline
  local rc=0
  _verify_runtime_seq "$@" || rc=$?
  _clear_verify_deadline
  return "${rc}"
}

# Vereinheitlichte Verifikationssequenz unter der bereits gesetzten gemeinsamen Deadline.
# ALLE externen Prozesse laufen ueber _dl (kubectl get/exec, docker/crictl via cri_inspect,
# beide Python-Helper, Identity-Parsing); Sleeps sind auf die Restzeit gedeckelt — keine
# neue Deadline je Teilschritt. Pod-Name UND Image-ID stammen aus DEMSELBEN Snapshot
# (Selektor --print identity), damit CRI-Pruefung, Health/Readiness und Diagnose denselben
# Pod betreffen. Pro Versuch genau EIN `kubectl exec` (Health+Readiness) mit festen Codes
# (21 Health, 22 Readiness).
# Retry-Entscheidung (alles innerhalb der gemeinsamen Deadline):
#   * retryfaehig: Selektor Exit 3 (kein Kandidat); CRI-Identity-Helper Exit 3
#     (gueltige CRI-Antwort, Digest-Mismatch — Rollout noch nicht konsistent); transienter
#     CRI-Transportfehler/leere CRI-Ausgabe; Health/Readiness noch nicht ok.
#   * SOFORT fatal: Selektor/CRI-Helper Exit 2 (malformed) oder 4 (Args); kubectl-Fehler;
#     Tool-Fehler (125/126/127). Timeout (124/137) beendet kontrolliert (Deadline).
_verify_runtime_seq() {
  local expect="${1:-${RUNTIME_TAG}}" cri_ref="${2:-${RUNTIME_TAG}}" expect_rid="${3:-}"
  local n="${RUNTIME_VERIFY_ATTEMPTS}" iv="${RUNTIME_VERIFY_INTERVAL}"
  local start i pod imageid specimg sprc rorc selrc cri crirc cid hrc last rc rem ex T slp identity
  local -a _idf
  start="${SECONDS}"; i=1; last="spec-image"; rc=1; pod=""

  # 1) Spec-Image (Consumer-Container) == erwartetem Image. kubectl-Exit getrennt erfasst:
  #    Fehler/Timeout/Tool-Fehler werden NICHT als leeres Image maskiert (kein '|| echo').
  specimg="$(_dl kubectl -n "${NAMESPACE}" get deploy/"${DEPLOY}" -o jsonpath="{.spec.template.spec.containers[?(@.name=='${CONTAINER}')].image}" 2>/dev/null)" && sprc=0 || sprc=$?
  if [ "${sprc}" -ne 0 ]; then
    _is_timeout_rc "${sprc}" && { _verify_diag spec-image "" 0 "${n}" "${sprc}" "${start}" timeout; return 1; }
    _verify_diag spec-image "" 0 "${n}" "${sprc}" "${start}" fatal-kubectl; return 1
  fi
  [ "${specimg}" = "${expect}" ] || { _verify_diag spec-image "" 0 "${n}" 1 "${start}" image-mismatch; return 1; }

  # 2) Rollout-Abschluss unter derselben Deadline (kein eigenes 90s-Fenster).
  _dl kubectl -n "${NAMESPACE}" rollout status deploy/"${DEPLOY}" --timeout="$(_verify_remaining)s" >/dev/null 2>&1 && rorc=0 || rorc=$?
  if [ "${rorc}" -ne 0 ]; then
    _is_timeout_rc "${rorc}" && { _verify_diag rollout "" 0 "${n}" "${rorc}" "${start}" timeout; return 1; }
    _verify_diag rollout "" 0 "${n}" "${rorc}" "${start}" rollout; return 1
  fi

  # 3) Retry-Schleife: Selektion(identity) -> CRI-Identitaet -> EIN exec (Health+Ready).
  while :; do
    _verify_deadline_reached && break
    identity="$(_select_pod --expect-image "${expect}" --require-running --require-ready --require-image-id --print identity)" && selrc=0 || selrc=$?
    if [ "${selrc}" -eq 0 ]; then
      # Identity-Parsing ebenfalls deadline-gebunden.
      mapfile -t _idf < <(printf '%s' "${identity}" | _dl python3 -c 'import json,sys
d=json.load(sys.stdin)
print(d.get("name",""))
print(d.get("imageID",""))' 2>/dev/null || true)
      pod="${_idf[0]:-}"; imageid="${_idf[1]:-}"
      if [ -z "${pod}" ] || [ -z "${imageid}" ]; then
        _verify_diag pod-selection "" "${i}" "${n}" 2 "${start}" fatal-input; return 1
      fi
      # CRI-Inspektion (docker/crictl) + Identity-Helper, beide deadline-gebunden, mit
      # GETRENNT erhaltenen Exit-Codes (kein Maskieren als leere Ausgabe).
      cri="$(cri_inspect "${cri_ref}")" && crirc=0 || crirc=$?
      if [ "${crirc}" -ne 0 ]; then
        if _is_timeout_rc "${crirc}"; then last="cri-timeout"; rc="${crirc}"
        elif _is_toolerr_rc "${crirc}"; then _verify_diag cri-identity "${pod}" "${i}" "${n}" "${crirc}" "${start}" fatal-tool; return 1
        else last="cri-transport"; rc="${crirc}"; fi   # transienter Transport/leer -> retry in Deadline
      else
        cid="$(printf '%s' "${cri}" | _dl python3 "${CRI_ID_HELPER}" "${imageid}" 2>/dev/null)" && crirc=0 || crirc=$?
        case "${crirc}" in
          0)
            if [ -n "${expect_rid}" ] && [ "${cid}" != "${expect_rid}" ]; then
              last="cri-identity"; rc=3   # Rollback-Digest weicht ab -> begrenzt retry
            else
              # CRI bestaetigt: EIN exec fuer Health UND Readiness, an Restzeit gebunden.
              rem="$(_verify_remaining)"; [ "${rem}" -ge 1 ] || break
              ex="$(_min "${rem}" 8)"; T="${ex}"
              _dl kubectl -n "${NAMESPACE}" exec "${pod}" -- python -c "import urllib.request as u,sys
def ok(e):
    try: return u.urlopen('http://127.0.0.1:8000/'+e,timeout=${T}).status==200
    except Exception: return False
if not ok('healthz'): sys.exit(21)
if not ok('readyz'): sys.exit(22)
sys.exit(0)" >/dev/null 2>&1 && hrc=0 || hrc=$?
              case "${hrc}" in
                0)        return 0 ;;
                21)       last="healthz"; rc=21 ;;
                22)       last="readyz";  rc=22 ;;
                124|137)  last="exec-timeout"; rc="${hrc}" ;;
                125|126|127) _verify_diag exec "${pod}" "${i}" "${n}" "${hrc}" "${start}" fatal-tool; return 1 ;;
                *)        last="exec"; rc="${hrc}" ;;
              esac
            fi ;;
          3)  last="cri-identity"; rc=3 ;;                          # Digest-Mismatch -> begrenzt retry
          2)  _verify_diag cri-identity "${pod}" "${i}" "${n}" 2 "${start}" fatal-input; return 1 ;;
          4)  _verify_diag cri-identity "${pod}" "${i}" "${n}" 4 "${start}" fatal-args; return 1 ;;
          124|137) last="cri-timeout"; rc="${crirc}" ;;
          125|126|127) _verify_diag cri-identity "${pod}" "${i}" "${n}" "${crirc}" "${start}" fatal-tool; return 1 ;;
          *)  _verify_diag cri-identity "${pod}" "${i}" "${n}" "${crirc}" "${start}" fatal; return 1 ;;
        esac
      fi
    elif [ "${selrc}" -eq 3 ]; then
      last="pod-selection"; rc=3   # valide Liste, (noch) kein Kandidat -> retryfaehig
    elif _is_timeout_rc "${selrc}"; then
      last="pod-selection"; rc="${selrc}"   # Selektor-Timeout -> Deadline beendet gleich
    elif _is_toolerr_rc "${selrc}"; then
      _verify_diag pod-selection "${pod}" "${i}" "${n}" "${selrc}" "${start}" fatal-tool; return 1
    else
      # kubectl-/Input-(2)/Argument-(4)-Fehler -> sofort fail closed, kein Retry.
      _verify_diag pod-selection "${pod}" "${i}" "${n}" "${selrc}" "${start}" fatal; return 1
    fi
    [ "${i}" -ge "${n}" ] && break
    _verify_deadline_reached && break
    rem="$(_verify_remaining)"; slp="${iv}"; [ "${slp}" -gt "${rem}" ] && slp="${rem}"
    [ "${slp}" -gt 0 ] && sleep "${slp}"
    i=$((i+1))
  done
  _verify_diag "${last}" "${pod}" "${i}" "${n}" "${rc}" "${start}"
  return 1
}

# ---- Bounded, read-only Monitoring-Readiness-Verifikation -------------------
# Strukturelle JSON-Auswertung (kein grep): liest Targets-/Rules-JSON von stdin und gibt
# GENAU einen nicht-sensiblen Token aus — niemals Roh-JSON. Keine Mutation.
_MON_TARGETS_PARSER='import json,sys
try:
    d=json.load(sys.stdin)
except Exception:
    print("BAD_JSON");sys.exit(0)
if not isinstance(d,dict):
    print("BAD_JSON");sys.exit(0)
if d.get("status")!="success":
    print("BAD_STATUS");sys.exit(0)
data=d.get("data")
ts=data.get("activeTargets") if isinstance(data,dict) else None
if not isinstance(ts,list):
    print("BAD_JSON");sys.exit(0)
cons=[]
for t in ts:
    if not isinstance(t,dict):
        print("BAD_JSON");sys.exit(0)
    lbl=t.get("labels") if isinstance(t.get("labels"),dict) else {}
    job=lbl.get("job")
    if job=="publisher":
        print("PUBLISHER");sys.exit(0)
    if job=="consumer":
        cons.append(t)
if not cons:
    print("NO_CONSUMER");sys.exit(0)
for t in cons:
    if t.get("health")!="up":
        print("CONSUMER_DOWN");sys.exit(0)
print("OK")'

_MON_RULES_PARSER='import json,sys
try:
    d=json.load(sys.stdin)
except Exception:
    print("BAD_JSON");sys.exit(0)
if not isinstance(d,dict):
    print("BAD_JSON");sys.exit(0)
if d.get("status")!="success":
    print("BAD_STATUS");sys.exit(0)
data=d.get("data")
gs=data.get("groups") if isinstance(data,dict) else None
if not isinstance(gs,list):
    print("BAD_JSON");sys.exit(0)
names=set()
for g in gs:
    if isinstance(g,dict):
        names.add(g.get("name"))
if "consumer" not in names:
    print("NO_CONSUMER_RULE");sys.exit(0)
if "queue" not in names:
    print("NO_QUEUE_RULE");sys.exit(0)
print("OK")'

# Fuehrt den Parser-Helper deadlinegebunden aus und klassifiziert SEINEN echten Exit-Code
# (kein '|| true'). Bei Exit 0 wird der Token via stdout zurueckgegeben; sonst wird ein
# fester Reason-Token gedruckt und ein Code zurueckgegeben, den _mon_check_once auf seine
# eigene Semantik (1 transient / 3 fatal) abbildet:
#   0  -> Parser-Token auf stdout, return 0
#   1  -> Deadline-/Parser-Timeout (124/137): 'parser-timeout', return 1 (durch Deadline beendet)
#   3  -> Tool-/Prozessfehler (125/126/127 oder jeder andere !=0): 'parser-error', return 3
# Roh-JSON wird NIE ausgegeben; stderr des Parsers wird verworfen.
_mon_parse() {
  local script="$1" body="$2" out prc=0
  out="$(printf '%s' "${body}" | _dl python3 -c "${script}")" || prc=$?
  if [ "${prc}" -eq 0 ]; then printf '%s' "${out}"; return 0; fi
  if _is_timeout_rc "${prc}"; then printf 'parser-timeout'; return 1; fi
  # 125/126/127 (timeout-intern/nicht ausfuehrbar/nicht gefunden) sowie jeder andere
  # Prozessfehler -> sofort fatal, NICHT als bad-json bis zum Limit retryen.
  printf 'parser-error'; return 3
}

# Eine read-only Einzelpruefung ALLER Monitoring-Bedingungen, gebunden an die
# gemeinsame Deadline (alle curl/python via _dl). Body UND HTTP-Code stammen aus DERSELBEN
# curl-Anfrage (`-w '\n%{http_code}'`, keine Temp-Dateien); nur HTTP 200 wird geparst.
# Gibt GENAU einen nicht-sensiblen Reason-Token aus; loggt nie Roh-JSON. Rueckgabe:
#   0  alle Bedingungen erfuellt
#   2  Publisher-Target = Policy-Verletzung -> sofort fail closed (kein Retry)
#   3  Tool-/Parser-Fehler (nicht ausfuehrbar) -> sofort fail closed
#   1  transient/retryfaehig (Token nennt den Grund), inkl. Nicht-2xx
_mon_check_once() {
  local code rc resp body parse
  # 1) /-/ready == HTTP 200 (ohne -f, damit 503/000 als Code lesbar bleiben).
  rc=0; code="$(_dl curl -sS -o /dev/null -w '%{http_code}' "${PROM_ENDPOINT}/-/ready" 2>/dev/null)" || rc=$?
  if _is_toolerr_rc "${rc}"; then printf 'tool-error'; return 3; fi
  if _is_timeout_rc "${rc}"; then printf 'prom-timeout'; return 1; fi
  if [ "${code}" != "200" ]; then printf 'not-ready'; return 1; fi   # conn-refused -> "000"

  # 2) Targets-API: Body + HTTP-Code aus EINER Anfrage. Nur 200 wird geparst; Nicht-2xx
  #    ist transient (retryfaehig), darf aber NIE ueber success-foermiges JSON gewinnen.
  rc=0; resp="$(_dl curl -sS -w '\n%{http_code}' "${PROM_ENDPOINT}/api/v1/targets?state=active" 2>/dev/null)" || rc=$?
  if _is_toolerr_rc "${rc}"; then printf 'tool-error'; return 3; fi
  if _is_timeout_rc "${rc}"; then printf 'prom-timeout'; return 1; fi
  if [ "${rc}" -ne 0 ]; then printf 'targets-unreachable'; return 1; fi   # conn-refused/Transport
  code="${resp##*$'\n'}"; body="${resp%$'\n'*}"
  [ "${code}" = "200" ] || { printf 'targets-http-%s' "${code:-000}"; return 1; }
  parse="$(_mon_parse "${_MON_TARGETS_PARSER}" "${body}")" || { rc=$?; printf '%s' "${parse}"; return "${rc}"; }
  case "${parse}" in
    PUBLISHER)     printf 'publisher-target'; return 2 ;;
    OK)            : ;;
    NO_CONSUMER)   printf 'no-consumer-target'; return 1 ;;
    CONSUMER_DOWN) printf 'consumer-target-down'; return 1 ;;
    BAD_STATUS)    printf 'targets-bad-status'; return 1 ;;
    *)             printf 'targets-bad-json'; return 1 ;;
  esac

  # 3) Rules-API: ebenso Body + HTTP-Code aus EINER Anfrage, nur 200 wird geparst.
  rc=0; resp="$(_dl curl -sS -w '\n%{http_code}' "${PROM_ENDPOINT}/api/v1/rules" 2>/dev/null)" || rc=$?
  if _is_toolerr_rc "${rc}"; then printf 'tool-error'; return 3; fi
  if _is_timeout_rc "${rc}"; then printf 'prom-timeout'; return 1; fi
  if [ "${rc}" -ne 0 ]; then printf 'rules-unreachable'; return 1; fi
  code="${resp##*$'\n'}"; body="${resp%$'\n'*}"
  [ "${code}" = "200" ] || { printf 'rules-http-%s' "${code:-000}"; return 1; }
  parse="$(_mon_parse "${_MON_RULES_PARSER}" "${body}")" || { rc=$?; printf '%s' "${parse}"; return "${rc}"; }
  case "${parse}" in
    OK)               printf 'ok'; return 0 ;;
    NO_CONSUMER_RULE) printf 'no-consumer-rule'; return 1 ;;
    NO_QUEUE_RULE)    printf 'no-queue-rule'; return 1 ;;
    BAD_STATUS)       printf 'rules-bad-status'; return 1 ;;
    *)                printf 'rules-bad-json'; return 1 ;;
  esac
}

# Secret-freie Diagnose: nur Reason-Token, Versuch, Budget, elapsed/remaining, Modus.
_mon_diag() {
  local reason="$1" att="$2" n="$3" start="$4" mode="${5:-deadline/attempts}"
  log "MONITORING-VERIFY: nicht bestaetigt [reason=${reason}] [versuch=${att}/${n}] [budget=${MONITORING_VERIFY_BUDGET}s] [elapsed=$(( SECONDS - start ))s] [remaining=$(_verify_remaining)s] [abbruch=${mode}]"
}

# Oeffentlicher Einstieg: GENAU EINE gemeinsame Monitoring-Deadline; raeumt sie auf
# jedem Return-Pfad wieder auf (ohne globale Traps zu beruehren). Read-only, fail closed.
_verify_monitoring_ready() {
  _validate_monitoring_verify_config   # defensiv (Erstvalidierung erfolgt im Preflight)
  _begin_verify_deadline "${MONITORING_VERIFY_BUDGET}"
  local rc=0
  _verify_monitoring_seq || rc=$?
  _clear_verify_deadline
  return "${rc}"
}

# Beschraenkte, read-only Retry-Schleife unter der bereits gesetzten Deadline. KEINE
# Mutation (kein recreate/restart/up). Publisher-Target und Tool-Fehler brechen SOFORT
# ab; transiente Startupzustaende werden bis zur Deadline/Versuchsgrenze erneut geprueft.
_verify_monitoring_seq() {
  local n="${MONITORING_VERIFY_ATTEMPTS}" iv="${MONITORING_VERIFY_INTERVAL}"
  local i start reason res crc rem slp
  start="${SECONDS}"; i=1; reason="startup"
  while :; do
    _verify_deadline_reached && break
    crc=0; res="$(_mon_check_once)" || crc=$?
    case "${crc}" in
      0) return 0 ;;
      2) _mon_diag "${res}" "${i}" "${n}" "${start}" policy-violation; return 1 ;;
      3) _mon_diag "${res}" "${i}" "${n}" "${start}" fatal-tool; return 1 ;;
      *) reason="${res}" ;;
    esac
    [ "${i}" -ge "${n}" ] && break
    _verify_deadline_reached && break
    rem="$(_verify_remaining)"; slp="${iv}"; [ "${slp}" -gt "${rem}" ] && slp="${rem}"
    [ "${slp}" -gt 0 ] && sleep "${slp}"
    i=$((i+1))
  done
  _mon_diag "${reason}" "${i}" "${n}" "${start}" deadline/attempts
  return 1
}

# ---- Rollback (deterministisch, CRI/containerd-digest-verifiziert) -----------
# Erfasst VOR jeder Mutation: Revision, Spec-Image, laufende Pod-Image-ID. Beweist
# die laufende Image-Identitaet AUSSCHLIESSLICH ueber CRI/containerd im k3d-Node
# (vollstaendige sha256:<64>-Digests, kein Praefix, KEINE Docker-.Id), sichert das
# bestehende Image DIREKT im containerd-Namespace k8s.io unter einem eindeutigen
# Rollback-Tag (kein 'docker tag', kein 'k3d image import') und verifiziert ihn
# erneut ueber CRI. Sonst fail closed.
_capture_rollback_target() {
  mkdir -p "${STATE_DIR}"
  _clear_verify_deadline   # Capture laeuft ausserhalb der Verify-Deadline (unbeschraenkt)
  local rev img pod_id cri runtime_id srcref repo rbref exist
  rev="$(kubectl -n "${NAMESPACE}" get deploy/"${DEPLOY}" -o jsonpath='{.metadata.annotations.deployment\.kubernetes\.io/revision}' 2>/dev/null || echo '')"
  img="$(kubectl -n "${NAMESPACE}" get deploy/"${DEPLOY}" -o jsonpath="{.spec.template.spec.containers[?(@.name=='${CONTAINER}')].image}" 2>/dev/null || echo '')"
  [ -n "${img}" ] || fail "aktuelles Deployment-Image nicht ermittelbar — Rollback nicht absicherbar."
  # Kohaerenz: der erfasste Pod muss BEWEISBAR zum gerade gelesenen Deployment-Image
  # gehoeren — nicht terminierend, Running, nicht leere Image-ID UND exakt 'img'. Ready
  # ist hier BEWUSST gefordert: das Rollback-Ziel muss ein tatsaechlich bedienender
  # (gesunder) Pod sein, damit das gesicherte Image known-good ist — konsistent mit der
  # Rollback-Verifikation, die ebenfalls --require-ready nutzt. Sonst kein kohaerenter
  # Kandidat -> fail closed.
  pod_id="$(_select_pod --expect-image "${img}" --require-running --require-ready --require-image-id --print imageID 2>/dev/null || echo '')"
  [ -n "${pod_id}" ] || fail "kein laufender, Ready Consumer-Pod mit dem aktuellen Deployment-Image — Rollback-Ziel nicht kohaerent erfassbar (fail closed)."
  # CRI inspiziert das LAUFENDE Image (per Pod-Digest) -> volle containerd-Identitaet.
  # cri_inspect liefert jetzt getrennte Codes (kein '|| true'): MUSS hier erfolgreich
  # sein, sonst fail closed (kein Maskieren als leere Ausgabe).
  local cri_rc=0
  cri="$(cri_inspect "${pod_id}")" || cri_rc=$?
  [ "${cri_rc}" -eq 0 ] || fail "CRI/containerd kann das laufende alte Image nicht inspizieren (exit=${cri_rc}) — Abbruch (fail closed)."
  runtime_id="$(printf '%s' "${cri}" | python3 "${CRI_ID_HELPER}" "${pod_id}")" \
    || fail "CRI/containerd kann das laufende alte Image nicht eindeutig bestaetigen — Abbruch (fail closed)."
  srcref="$(printf '%s' "${cri}" | python3 "${CRI_ID_HELPER}" --source-ref "${pod_id}")" \
    || fail "keine nutzbare CRI-Quell-Referenz fuer das alte Image — Abbruch (fail closed)."
  case "${srcref}" in *@*) repo="${srcref%@*}" ;; *:*) repo="${srcref%:*}" ;; *) repo="${srcref}" ;; esac
  rbref="${repo}:rollback-$(printf '%s' "${runtime_id#sha256:}" | cut -c1-12)"
  # Existiert der Rollback-Tag schon? `crictl inspecti` eines fehlenden Tags endet
  # bewusst non-zero (bzw. _RC_CRI_EMPTY) — das ist hier eine legitime Existenz-Probe,
  # KEIN maskierter Fehler: exrc wird explizit erfasst, leere/non-zero Antwort = absent
  # -> Tag wird via containerd neu angelegt. Vorhandener Tag muss auf dieselbe Identitaet
  # zeigen, sonst fail closed (kein stilles Ueberschreiben).
  local exrc=0
  exist="$(cri_inspect "${rbref}")" || exrc=$?
  if [ "${exrc}" -eq 0 ] && [ -n "$(printf '%s' "${exist}" | tr -d '[:space:]')" ]; then
    printf '%s' "${exist}" | python3 "${CRI_ID_HELPER}" "${runtime_id}" >/dev/null \
      || fail "vorhandener Rollback-Tag zeigt auf ABWEICHENDE Identitaet — Abbruch (kein stilles Ueberschreiben)."
    log "Bestehender Rollback-Tag mit identischer CRI-Identitaet wiederverwendet."
  else
    ctr_tag "${srcref}" "${rbref}" >/dev/null 2>&1 \
      || fail "containerd-Tagging (k8s.io) des Rollback-Images fehlgeschlagen."
  fi
  # Verifizieren: Rollback-Tag ueber CRI vorhanden + zeigt auf die laufende Identitaet.
  printf '%s' "$(cri_inspect "${rbref}")" | python3 "${CRI_ID_HELPER}" "${runtime_id}" >/dev/null \
    || fail "Rollback-Tag nicht ueber CRI auf die laufende Identitaet verifizierbar — Abbruch."
  local tmp; tmp="$(mktemp "${STATE_DIR}/.rollback.XXXXXX")"
  REV="${rev:-}" IMG="${img}" POD="${pod_id}" RID="${runtime_id}" RB="${rbref}" REL="${RELEASE_SHA}" \
    python3 - "$tmp" <<'PY' || { rm -f "$tmp"; fail "Rollback-Ziel nicht serialisierbar"; }
import json,os,sys,datetime
json.dump({"revision":os.environ.get("REV",""),"old_spec_image":os.environ["IMG"],
          "old_pod_image_id":os.environ["POD"],"runtime_id":os.environ["RID"],
          "rollback_tag":os.environ["RB"],"release_sha":os.environ["REL"],
          "at":datetime.datetime.now(datetime.timezone.utc).isoformat()},open(sys.argv[1],"w"))
PY
  mv -f "$tmp" "${ROLLBACK_FILE}"
  log "Rollback-Ziel erfasst + im containerd-Store gesichert (volle Digest-Identitaet; keine Docker-.Id, keine Registry/Hosts)."
}

# Setzt das Deployment explizit auf den containerd-Rollback-Tag und VERIFIZIERT die
# laufende Identitaet ueber CRI gegen den gespeicherten vollen Runtime-Digest.
# 'kubectl rollout undo' bleibt ausdruecklich unzureichend und wird NICHT verwendet.
_rollback_consumer() {
  [ -f "${ROLLBACK_FILE}" ] || { log "kein Rollback-Ziel erfasst — kein Rollback"; return 1; }
  _clear_verify_deadline   # Vorpruefungen unbeschraenkt; die Verifikation setzt ihre eigene Deadline
  local rbref rid
  rbref="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("rollback_tag",""))' "${ROLLBACK_FILE}" 2>/dev/null || echo '')"
  rid="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("runtime_id",""))' "${ROLLBACK_FILE}" 2>/dev/null || echo '')"
  [ -n "${rbref}" ] || { log "Rollback-Tag fehlt — kein sicherer Rollback"; return 1; }
  printf '%s' "${rid}" | grep -Eq '^sha256:[0-9a-f]{64}$' || { log "gespeicherter Runtime-Digest ungueltig"; return 1; }
  # 1/2) Rollback-Tag muss noch ueber CRI vorhanden sein UND auf rid zeigen.
  printf '%s' "$(cri_inspect "${rbref}")" | python3 "${CRI_ID_HELPER}" "${rid}" >/dev/null \
    || { log "Rollback-Tag ueber CRI nicht (mehr) auf die gespeicherte Identitaet aufloesbar"; return 1; }
  # 3) Deployment explizit auf den containerd-Rollback-Tag setzen.
  kubectl -n "${NAMESPACE}" set image deploy/"${DEPLOY}" "${CONTAINER}=${rbref}" >/dev/null 2>&1 || return 1
  # 4-8) EINE Runtime-Verifikation mit EINEM Gesamtbudget: Spec-Image==rbref, Rollout,
  # Release-Pod-Selektion, CRI-Identitaet == gespeichertem rid, Health, Readiness.
  # Kein zweites unabhaengiges 30s-Fenster.
  _verify_consumer_runtime "${rbref}" "${rbref}" "${rid}" || return 1
  log "Rollback bestaetigt: laufende containerd-Identitaet == gespeicherter alter Runtime-Digest."
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
  # detect_runtime_tools laeuft im fresh-run ueber preflight (vor jeder Mutation);
  # ein No-op-Lauf auf bereits 'complete' fasst den Node bewusst NICHT an.
  with_lock; validate_release_sha; _validate_verify_prerequisites; restart_gate
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
  # Voraussetzungen (inkl. Monitoring-Konfig + kill-after) VOR detect_runtime_tools,
  # restart_gate und jeder Phase pruefen — ein Resume ab consumer-deployed darf NICHTS
  # mutieren/recreaten, wenn eine Voraussetzung fehlschlagen wuerde (fail closed).
  with_lock; validate_release_sha; _validate_verify_prerequisites; detect_runtime_tools; restart_gate
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

# Source-Guard: Dispatcher nur bei direkter Ausfuehrung. Beim `source` (Unit-Tests
# einzelner Funktionen) wird nichts dispatcht.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  case "${1:-}" in
    preflight) with_lock; preflight ;;
    run)       cmd_run ;;
    resume)    cmd_resume ;;
    state)     state_cmd ;;
    *)         usage ;;
  esac
fi
