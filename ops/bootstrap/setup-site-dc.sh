#!/usr/bin/env bash
#
# setup-site-dc.sh — bringt Host site-dc in den Soll-Zustand:
# installiert Docker Engine + Compose-Plugin aus dem offiziellen Docker-Repo
# und zieht den site-dc-Stack hoch. Idempotent: mehrfach ausfuehrbar.
#
# Ziel-OS: Ubuntu 24.04. Aufruf auf der VM:  sudo ./ops/bootstrap/setup-site-dc.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SITE_DIR="${REPO_ROOT}/sites/dc"

log() { printf '\n[setup-site-dc] %s\n' "$1"; }

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
# Robuste Zufallspasswoerter: head liest /dev/urandom direkt (bounded) -> kein
# SIGPIPE durch einen Infinite-Stream-in-Pipe; Reduktion auf alnum via Bash
# (kein zweiter Pipe-Konsument). Kryptografisch ausreichend (144 Bit Entropie).
gen_pw() {
  local raw
  raw="$(head -c 18 /dev/urandom | base64)"
  raw="${raw//[^A-Za-z0-9]/}"
  printf '%s' "${raw}"
}

if [[ ! -f "${SITE_DIR}/.env" ]]; then
  log "Erzeuge geschuetzte .env mit zufaelligen lokalen Passwoertern (Modus 600)"
  # .env.example wird NIE als aktive .env kopiert (oeffentlich bekannte Beispielwerte).
  umask 077
  cat > "${SITE_DIR}/.env" <<EOF
POSTGRES_USER=hol_admin
POSTGRES_PASSWORD=$(gen_pw)
POSTGRES_DB=postgres
INVENTORY_DB=inventory
INVENTORY_ADMIN_PASSWORD=$(gen_pw)
INVENTORY_APP_PASSWORD=$(gen_pw)
INVENTORY_HOST_PORT=8000
EVENTS_ENABLED=false
SQS_ENDPOINT_URL=
SQS_QUEUE_URL=
AWS_REGION=eu-central-1
EOF
  chmod 600 "${SITE_DIR}/.env"
  # Eigentum an den aufrufenden Nutzer, damit er die .env spaeter verwalten kann.
  # Primaere Gruppe robust ermitteln (nicht hardcoden, nicht == Username annehmen).
  if [[ -n "${TARGET_USER}" ]]; then
    TARGET_GROUP="$(id -gn "${TARGET_USER}")"
    chown "${TARGET_USER}:${TARGET_GROUP}" "${SITE_DIR}/.env"
  fi
else
  log "Bestehende .env bleibt unveraendert (keine Rotation, kein Ueberschreiben)"
fi

# --- 6. Compose-Konfiguration validieren (kein Secret-Output: --quiet) ---
cd "${SITE_DIR}"
log "Validiere Compose-Konfiguration"
if ! docker compose --env-file .env config --quiet; then
  echo "[setup-site-dc] Compose-Konfiguration ungueltig — Abbruch." >&2
  exit 1
fi

# --- 7. Stack hochziehen ---
log "Starte site-dc-Stack"
docker compose --env-file .env up -d --build

log "Fertig. Status:"
docker compose ps
