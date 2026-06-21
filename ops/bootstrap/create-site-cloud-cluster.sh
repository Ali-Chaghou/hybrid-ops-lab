#!/usr/bin/env bash
#
# create-site-cloud-cluster.sh — erstellt den k3d-Cluster `site-cloud` MIT einer
# Host-Portabbildung fuer den Consumer-/metrics-NodePort (Default 30090), damit
# Prometheus (im monitoring-Compose) den Consumer ueber host.docker.internal:30090
# scrapen kann. Ohne diese Abbildung ist ein `type: NodePort`-Service NICHT vom
# Docker-Host erreichbar.
#
# Idempotent: existiert der Cluster bereits, wird NUR geprueft, ob die Portabbildung
# vorhanden ist. Eine Portabbildung laesst sich bei k3d NICHT nachtraeglich
# hinzufuegen — fehlt sie, muss der Cluster bewusst geloescht und neu erstellt werden
# (fail closed mit klarer Anleitung). Auszufuehren als normaler Nutzer auf site-cloud.
set -euo pipefail

CLUSTER="${CLUSTER:-site-cloud}"
METRICS_NODEPORT="${METRICS_NODEPORT:-30090}"
SERVER_NODE="k3d-${CLUSTER}-server-0"

if k3d cluster list "${CLUSTER}" >/dev/null 2>&1; then
  echo "[create-cluster] Cluster '${CLUSTER}' existiert bereits."
  if docker port "${SERVER_NODE}" "${METRICS_NODEPORT}/tcp" >/dev/null 2>&1; then
    echo "[create-cluster] Port ${METRICS_NODEPORT} ist bereits auf den Host veroeffentlicht — ok."
    exit 0
  fi
  echo "[create-cluster] FEHLER: Port ${METRICS_NODEPORT} ist NICHT veroeffentlicht." >&2
  echo "[create-cluster] Eine k3d-Portabbildung kann nicht nachtraeglich ergaenzt werden." >&2
  echo "[create-cluster] Cluster bewusst neu erstellen (verwirft den Cluster-Zustand):" >&2
  echo "[create-cluster]   k3d cluster delete ${CLUSTER} && CLUSTER=${CLUSTER} $0" >&2
  exit 1
fi

echo "[create-cluster] Erstelle k3d-Cluster '${CLUSTER}' mit ${METRICS_NODEPORT}->Host (@server:0)."
k3d cluster create "${CLUSTER}" \
  --port "${METRICS_NODEPORT}:${METRICS_NODEPORT}@server:0"
echo "[create-cluster] Fertig. Consumer-/metrics ist nach dem Deploy ueber host.docker.internal:${METRICS_NODEPORT} erreichbar."
