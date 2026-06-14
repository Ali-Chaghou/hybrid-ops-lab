#!/usr/bin/env bash
#
# degrade-link.sh — schaltet eine Latenz-Stoerung auf der Strecke (Toxiproxy)
# zwischen Consumer und Queue ein. Simuliert eine degradierte Standortverbindung.
# Idempotent: ein bereits vorhandener Toxic wird zuerst entfernt.
#
# Aufruf auf der VM site-cloud:  ./ops/chaos/degrade-link.sh [latency_ms] [jitter_ms]
set -euo pipefail

TOXIPROXY_API="${TOXIPROXY_API:-http://localhost:8474}"
PROXY="${PROXY:-elasticmq}"
TOXIC="link_latency"
LATENCY="${1:-7000}"
JITTER="${2:-1000}"

curl -fsS -X DELETE "${TOXIPROXY_API}/proxies/${PROXY}/toxics/${TOXIC}" >/dev/null 2>&1 || true

curl -fsS -X POST "${TOXIPROXY_API}/proxies/${PROXY}/toxics" \
  -H 'Content-Type: application/json' \
  -d "{\"name\":\"${TOXIC}\",\"type\":\"latency\",\"attributes\":{\"latency\":${LATENCY},\"jitter\":${JITTER}}}" \
  >/dev/null

echo "[degrade-link] Latenz aktiv: ${LATENCY}ms (+/- ${JITTER}ms) auf Proxy '${PROXY}'."
