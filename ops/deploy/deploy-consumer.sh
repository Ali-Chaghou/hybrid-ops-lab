#!/usr/bin/env bash
#
# deploy-consumer.sh — baut das Consumer-Image, importiert es in den k3d-Cluster
# und deployt das Manifest. Die k3d-Netz-Gateway-IP (= VM-Host aus Pod-Sicht) wird
# zur Deploy-Zeit ermittelt und in das hostAliases-Feld eingesetzt, statt sie fest
# im YAML zu pinnen.
# Auszufuehren auf der VM site-cloud (docker, k3d, kubectl vorhanden).
set -euo pipefail

CLUSTER="${CLUSTER:-site-cloud}"
NETWORK="${NETWORK:-k3d-${CLUSTER}}"
IMAGE="${IMAGE:-inventory-consumer:dev}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MANIFEST="${REPO_ROOT}/sites/cloud/k8s/consumer.yaml"

# Gateway des k3d-Docker-Netzes = VM-Host, wie ihn die Pods erreichen.
GATEWAY="$(docker network inspect "${NETWORK}" -f '{{range .IPAM.Config}}{{.Gateway}}{{end}}')"
if [ -z "${GATEWAY}" ]; then
  echo "[deploy-consumer] Gateway-IP von Docker-Netz '${NETWORK}' nicht ermittelbar." >&2
  exit 1
fi
echo "[deploy-consumer] k3d-Gateway: ${GATEWAY}"

# Image bauen und in den Cluster importieren (kein Registry-Push noetig).
docker build -t "${IMAGE}" "${REPO_ROOT}/apps/consumer"
k3d image import "${IMAGE}" -c "${CLUSTER}"

# Platzhalter durch die ermittelte Gateway-IP ersetzen und anwenden.
sed "s|__K3D_GATEWAY__|${GATEWAY}|g" "${MANIFEST}" | kubectl apply -f -

kubectl -n inventory rollout status deployment/inventory-consumer --timeout=90s
echo "[deploy-consumer] Consumer deployed (host.k3d.internal -> ${GATEWAY})."
