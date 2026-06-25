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
# WICHTIG: k3d veroeffentlicht den Host-Port NICHT auf dem Server-Node, sondern auf
# dem generierten Server-Loadbalancer-Container (k3d-<cluster>-serverlb). Der
# Publikations-Check prueft daher den Loadbalancer (PORT_OWNER), nicht den Server-Node.
PORT_OWNER="${K3D_PORT_OWNER:-k3d-${CLUSTER}-serverlb}"
printf '%s' "${PORT_OWNER}" | grep -Eq '^[A-Za-z0-9._-]+$' \
  || { echo "[create-cluster] FEHLER: ungueltiger Port-Owner-Containername." >&2; exit 1; }

if k3d cluster list "${CLUSTER}" >/dev/null 2>&1; then
  echo "[create-cluster] Cluster '${CLUSTER}' existiert bereits."
  if ! docker inspect "${PORT_OWNER}" >/dev/null 2>&1; then
    echo "[create-cluster] FEHLER: Port-Owner-Container '${PORT_OWNER}' nicht gefunden/erreichbar." >&2
    exit 1
  fi
  if docker port "${PORT_OWNER}" "${METRICS_NODEPORT}/tcp" >/dev/null 2>&1; then
    echo "[create-cluster] Port ${METRICS_NODEPORT} ist bereits auf den Host veroeffentlicht (via ${PORT_OWNER}) — ok."
    exit 0
  fi
  echo "[create-cluster] FEHLER: Port ${METRICS_NODEPORT} ist NICHT veroeffentlicht (erwartet auf ${PORT_OWNER})." >&2
  echo "[create-cluster] Eine k3d-Portabbildung kann nicht nachtraeglich ergaenzt werden." >&2
  echo "[create-cluster] Cluster bewusst neu erstellen (verwirft den Cluster-Zustand):" >&2
  echo "[create-cluster]   k3d cluster delete ${CLUSTER} && CLUSTER=${CLUSTER} $0" >&2
  exit 1
fi

echo "[create-cluster] Erstelle k3d-Cluster '${CLUSTER}' mit ${METRICS_NODEPORT}->Host (@server:0)."
k3d cluster create "${CLUSTER}" \
  --port "${METRICS_NODEPORT}:${METRICS_NODEPORT}@server:0"
echo "[create-cluster] Fertig. Consumer-/metrics ist nach dem Deploy ueber host.docker.internal:${METRICS_NODEPORT} erreichbar."
