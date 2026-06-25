#!/usr/bin/env bash
#
# deploy-consumer.sh — baut das Consumer-Image, importiert es in den k3d-Cluster
# und deployt das Manifest. Die k3d-Netz-Gateway-IP (= VM-Host aus Pod-Sicht) wird
# zur Deploy-Zeit ermittelt und in das hostAliases-Feld eingesetzt, statt sie fest
# im YAML zu pinnen.
#
# VORAUSSETZUNG (wird NICHT von diesem Skript erledigt): Consumer-DB, Rollen
# (consumer_admin/consumer_app) und die Migration 0001_init muessen bereits
# vorbereitet sein — ueber den site-cloud-Compose-Stack:
#   cd sites/cloud && docker compose up -d consumer-db \
#       consumer-db-bootstrap consumer-db-prepare consumer-migrate
# Erst danach kann der k3d-Consumer als consumer_app gegen die DB starten.
#
# Auszufuehren auf der VM site-cloud (docker, k3d, kubectl vorhanden).
set -euo pipefail

CLUSTER="${CLUSTER:-site-cloud}"
NETWORK="${NETWORK:-k3d-${CLUSTER}}"
IMAGE="${IMAGE:-inventory-consumer:dev}"
# Injection-sichere Tag-Form (kein Shell-/sed-Metazeichen) — der Tag wird per sed in
# das Manifest gerendert. Nur Repo:Tag aus [A-Za-z0-9._/-].
case "${IMAGE}" in
  *[!A-Za-z0-9._/:-]*) echo "[deploy-consumer] FEHLER: ungueltiger IMAGE-Tag." >&2; exit 1 ;;
esac
printf '%s' "${IMAGE}" | grep -Eq '^[A-Za-z0-9._/-]+:[A-Za-z0-9._-]+$' \
  || { echo "[deploy-consumer] FEHLER: IMAGE muss die Form repo:tag haben." >&2; exit 1; }
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MANIFEST="${REPO_ROOT}/sites/cloud/k8s/consumer.yaml"
ENV_FILE="${ENV_FILE:-${REPO_ROOT}/sites/cloud/.env}"
METRICS_NODEPORT="${METRICS_NODEPORT:-30090}"
# k3d veroeffentlicht den Host-Port auf dem generierten Server-Loadbalancer-Container
# (k3d-<cluster>-serverlb), NICHT auf dem Server-Node -> Publikations-Check prueft den
# Loadbalancer (PORT_OWNER). Validierter, eng begrenzter Containername.
PORT_OWNER="${K3D_PORT_OWNER:-k3d-${CLUSTER}-serverlb}"
printf '%s' "${PORT_OWNER}" | grep -Eq '^[A-Za-z0-9._-]+$' \
  || { echo "[deploy-consumer] FEHLER: ungueltiger Port-Owner-Containername." >&2; exit 1; }
# Verzeichnis der Prometheus-file-sd-Targets (im monitoring-Compose read-only nach
# /etc/prometheus/targets gemountet). Ueberschreibbar fuer Tests.
TARGET_DIR="${TARGET_DIR:-${REPO_ROOT}/monitoring/prometheus/targets}"

echo "[deploy-consumer] Voraussetzung: Consumer-DB, Rollen und Migration sind vorbereitet"
echo "[deploy-consumer]   (sites/cloud: docker compose up -d consumer-db consumer-db-bootstrap consumer-db-prepare consumer-migrate)."
echo "[deploy-consumer] Dieses Skript richtet KEINE Datenbank ein."

if [ ! -f "${ENV_FILE}" ]; then
  echo "[deploy-consumer] ${ENV_FILE} fehlt (aus .env.example anlegen, starke Werte setzen)." >&2
  exit 1
fi

# Gateway des k3d-Docker-Netzes = VM-Host, wie ihn die Pods erreichen.
GATEWAY="$(docker network inspect "${NETWORK}" -f '{{range .IPAM.Config}}{{.Gateway}}{{end}}')"
if [ -z "${GATEWAY}" ]; then
  echo "[deploy-consumer] Gateway-IP von Docker-Netz '${NETWORK}' nicht ermittelbar." >&2
  exit 1
fi
echo "[deploy-consumer] k3d-Gateway: ${GATEWAY}"

# Fail closed: der Consumer-/metrics-NodePort MUSS auf den Host veroeffentlicht sein,
# sonst kann Prometheus host.docker.internal:${METRICS_NODEPORT} nicht scrapen und es
# entstuende keine up{job="consumer"}-Serie. k3d publiziert den Host-Port ueber den
# generierten Server-Loadbalancer (PORT_OWNER), NICHT ueber den Server-Node. Kein
# stiller Fallback zwischen Loadbalancer und Server-Node.
if ! docker inspect "${PORT_OWNER}" >/dev/null 2>&1; then
  echo "[deploy-consumer] FEHLER: Port-Owner-Container '${PORT_OWNER}' nicht gefunden/erreichbar." >&2
  exit 1
fi
if ! docker port "${PORT_OWNER}" "${METRICS_NODEPORT}/tcp" >/dev/null 2>&1; then
  echo "[deploy-consumer] FEHLER: NodePort ${METRICS_NODEPORT} ist nicht auf den Host veroeffentlicht (erwartet auf ${PORT_OWNER})." >&2
  echo "[deploy-consumer] Cluster mit Portabbildung erstellen: ops/bootstrap/create-site-cloud-cluster.sh" >&2
  exit 1
fi
echo "[deploy-consumer] NodePort ${METRICS_NODEPORT} auf Host veroeffentlicht (via ${PORT_OWNER}) — ok."

# Image bauen + importieren — BEVOR irgendwelche Secrets geladen werden, damit waehrend
# des Builds nichts Geheimes in der Umgebung steht. Kontext = Repo-Root (ops/ + Migrationen).
docker build -t "${IMAGE}" -f "${REPO_ROOT}/apps/consumer/Dockerfile" "${REPO_ROOT}"
k3d image import "${IMAGE}" -c "${CLUSTER}"

# Namespace sicherstellen (idempotent) — muss vor dem Secret existieren.
kubectl create namespace inventory --dry-run=client -o yaml | kubectl apply -f -

# --- Enger, kontrollierter Secret-Block --------------------------------------
# Die .env wird NICHT global exportiert (kein `set -a`), damit POSTGRES_PASSWORD,
# CONSUMER_*_PASSWORD und die DATABASE_URL nicht in den Environments von docker/
# k3d/kubectl-Kindprozessen landen. Der DSN geht ausschliesslich ueber eine
# 0600-Tempdatei an `kubectl --from-file` (Dateiinhalt = Wert, kein KEY=VALUE-Parsing,
# nichts in der Prozessargumentliste). trap loescht die Tempdatei bei Erfolg UND Fehler.
SECRET_FILE=""
cleanup_secret() { [ -n "${SECRET_FILE}" ] && rm -f "${SECRET_FILE}"; return 0; }
trap cleanup_secret EXIT

create_db_secret() {
  # shellcheck source=/dev/null
  . "${ENV_FILE}"
  : "${CONSUMER_DB:?CONSUMER_DB nicht in .env gesetzt}"
  : "${CONSUMER_APP_PASSWORD:?CONSUMER_APP_PASSWORD nicht in .env gesetzt (leer = Fail closed)}"
  local port="${CONSUMER_DB_HOST_PORT:-5433}"
  SECRET_FILE="$(mktemp)"
  chmod 600 "${SECRET_FILE}"
  printf '%s' "host=host.k3d.internal port=${port} user=consumer_app password=${CONSUMER_APP_PASSWORD} dbname=${CONSUMER_DB}" > "${SECRET_FILE}"
  kubectl create secret generic consumer-db-creds \
    --namespace inventory \
    --from-file=DATABASE_URL="${SECRET_FILE}" \
    --dry-run=client -o yaml | kubectl apply -f -
  rm -f "${SECRET_FILE}"
  SECRET_FILE=""
}
create_db_secret
trap - EXIT

# Manifest anwenden: NICHT-geheime Gateway-IP UND der tatsaechlich gebaute Image-Tag
# werden per sed gesetzt (beide vorab validiert -> keine Injection).
kubectl apply -f <(sed -e "s|__K3D_GATEWAY__|${GATEWAY}|g" -e "s|__CONSUMER_IMAGE__|${IMAGE}|g" "${MANIFEST}")

# Rollout neu anstossen, damit der Pod eine ggf. rotierte DATABASE_URL uebernimmt
# (envFrom-Secret-Aenderungen starten Pods nicht automatisch neu).
kubectl -n inventory rollout restart deployment/inventory-consumer
kubectl -n inventory rollout status deployment/inventory-consumer --timeout=90s

# Prometheus-file-sd-Target reproduzierbar + ATOMAR erzeugen (temp + mv). Adresse =
# host.docker.internal:${METRICS_NODEPORT} (stabil, unabhaengig von Cluster-Neuaufbau,
# keine echte IP, kein Secret). Damit existiert die up{job="consumer"}-Serie auch dann,
# wenn der Consumer-Endpunkt down ist (-> ConsumerDown-Alert auswertbar).
if [ ! -d "${TARGET_DIR}" ]; then
  echo "[deploy-consumer] FEHLER: Target-Verzeichnis ${TARGET_DIR} fehlt." >&2
  exit 1
fi
TMP_TARGET="$(mktemp "${TARGET_DIR}/.consumer.json.XXXXXX")"
printf '[\n  { "targets": ["host.docker.internal:%s"], "labels": { "site": "cloud", "app": "inventory-consumer" } }\n]\n' \
  "${METRICS_NODEPORT}" > "${TMP_TARGET}"
# mktemp erzeugt 0600; Prometheus laeuft als anderer Container-User und muss die
# bind-gemountete Datei lesen koennen -> Modus VOR dem atomaren mv explizit auf 0644.
chmod 0644 "${TMP_TARGET}" || { echo "[deploy-consumer] FEHLER: Dateimodus 0644 fuer das Target nicht setzbar." >&2; rm -f "${TMP_TARGET}"; exit 1; }
mv -f "${TMP_TARGET}" "${TARGET_DIR}/consumer.json"
echo "[deploy-consumer] Prometheus-Target geschrieben: ${TARGET_DIR}/consumer.json"
echo "[deploy-consumer] Consumer deployed (host.k3d.internal -> ${GATEWAY}; /metrics -> host.docker.internal:${METRICS_NODEPORT})."
