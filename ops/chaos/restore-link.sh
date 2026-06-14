#!/usr/bin/env bash
#
# restore-link.sh — hebt die Latenz-Stoerung auf der Strecke wieder auf.
# Idempotent: kein Fehler, wenn kein Toxic vorhanden ist.
#
# Aufruf auf der VM site-cloud:  ./ops/chaos/restore-link.sh
set -euo pipefail

TOXIPROXY_API="${TOXIPROXY_API:-http://localhost:8474}"
PROXY="${PROXY:-elasticmq}"
TOXIC="link_latency"

curl -fsS -X DELETE "${TOXIPROXY_API}/proxies/${PROXY}/toxics/${TOXIC}" >/dev/null 2>&1 || true

echo "[restore-link] Stoerung entfernt. Strecke '${PROXY}' wieder ungedrosselt."
