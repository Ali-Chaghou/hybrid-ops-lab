#!/usr/bin/env bash
#
# setup-site-cloud.sh — bringt Host site-cloud in den Soll-Zustand:
# installiert Docker Engine + Compose-Plugin aus dem offiziellen Docker-Repo,
# zieht den site-cloud-Stack hoch und installiert k3d + kubectl (Consumer-Cluster).
# Idempotent: mehrfach ausfuehrbar.
#
# Ziel-OS: Ubuntu 24.04. Aufruf auf der VM:  sudo ./ops/bootstrap/setup-site-cloud.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SITE_DIR="${REPO_ROOT}/sites/cloud"

K3D_VERSION="v5.9.0"
KUBECTL_VERSION="v1.35.5"

log() { printf '\n[setup-site-cloud] %s\n' "$1"; }

if [[ "${EUID}" -ne 0 ]]; then
  echo "Bitte mit sudo ausfuehren." >&2
  exit 1
fi

# --- 1. Alte/Distro-Docker-Pakete entfernen (kollidieren mit dem offiziellen Repo) ---
log "Entferne ggf. vorhandene Distro-Docker-Pakete"
for pkg in docker.io docker-doc docker-compose docker-compose-v2 podman-docker containerd runc; do
  apt-get remove -y "${pkg}" >/dev/null 2>&1 || true
done

# --- 2. Offizielles Docker-Repo einrichten (idempotent) ---
log "Richte offizielles Docker-Repo ein"
apt-get update -qq
apt-get install -y -qq ca-certificates curl
install -m 0755 -d /etc/apt/keyrings
if [[ ! -f /etc/apt/keyrings/docker.asc ]]; then
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
fi
ARCH="$(dpkg --print-architecture)"
CODENAME="$(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")"
echo \
  "deb [arch=${ARCH} signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${CODENAME} stable" \
  > /etc/apt/sources.list.d/docker.list

# --- 3. Docker Engine + Compose-Plugin installieren ---
log "Installiere Docker Engine + Compose-Plugin"
apt-get update -qq
apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# --- 4. Aufrufenden Nutzer in die docker-Gruppe (sudo-Aufruf: SUDO_USER) ---
TARGET_USER="${SUDO_USER:-}"
if [[ -n "${TARGET_USER}" ]]; then
  log "Fuege ${TARGET_USER} der docker-Gruppe hinzu (wirkt nach Neuanmeldung)"
  usermod -aG docker "${TARGET_USER}"
fi

# --- 5. .env sicherstellen ---
if [[ ! -f "${SITE_DIR}/.env" ]]; then
  log ".env aus .env.example erzeugen (Platzhalterwerte)"
  cp "${SITE_DIR}/.env.example" "${SITE_DIR}/.env"
fi

# --- 6. Stack hochziehen ---
log "Starte site-cloud-Stack"
cd "${SITE_DIR}"
docker compose --env-file .env up -d --build

# --- 7. k3d + kubectl installieren (Phase 4: Consumer-Cluster) ---
# Nur die Tools. Das Cluster wird bewusst NICHT hier (als root) erzeugt, sondern
# als normaler Nutzer, damit die kubeconfig im richtigen Home landet.
if ! command -v k3d >/dev/null 2>&1; then
  log "Installiere k3d ${K3D_VERSION}"
  curl -fsSLo /usr/local/bin/k3d \
    "https://github.com/k3d-io/k3d/releases/download/${K3D_VERSION}/k3d-linux-${ARCH}"
  chmod +x /usr/local/bin/k3d
fi

if ! command -v kubectl >/dev/null 2>&1; then
  log "Installiere kubectl ${KUBECTL_VERSION}"
  curl -fsSLo /usr/local/bin/kubectl \
    "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${ARCH}/kubectl"
  chmod +x /usr/local/bin/kubectl
fi

log "Fertig. Status:"
cd "${SITE_DIR}"
docker compose ps
k3d version
kubectl version --client
