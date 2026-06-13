# site-dc

Standort "Rechenzentrum" des hybrid-ops-lab. Bildet eine klassische On-Prem-Umgebung nach: eine REST-Anwendung mit eigener Datenbank, lokal auf einem Host betrieben, mit Host-Metriken fuer das spaetere Monitoring.

## Komponenten

| Service | Image | Zweck |
|---|---|---|
| `inventory` | lokal gebaut (`apps/inventory`) | REST-API, persistiert Lagerbewegungen, exponiert `/metrics` |
| `db` | `postgres:16-alpine` | Datenhaltung fuer `inventory` |
| `node_exporter` | `quay.io/prometheus/node-exporter:v1.11.1` | Host-Metriken (CPU, RAM, Disk) fuer Prometheus |

## Voraussetzungen

Docker Engine inkl. Compose-Plugin. Auf einem frischen Ubuntu-Host installiert das Bootstrap-Skript beides aus dem offiziellen Docker-Repo und zieht den Stack hoch:

    sudo ./ops/bootstrap/setup-site-dc.sh

Das Skript ist idempotent, mehrfach ausfuehrbar.

## Konfiguration

Alle Werte kommen aus `.env` (12-factor). Vorlage ist `.env.example`; die echte `.env` ist gitignored.

    cp .env.example .env

| Variable | Default | Bedeutung |
|---|---|---|
| `POSTGRES_USER` / `_PASSWORD` / `_DB` | — | Datenbank-Zugang |
| `EVENTS_ENABLED` | `false` | SQS-Publish an/aus (ab Phase 3) |
| `SQS_ENDPOINT_URL` | leer | Queue-Endpoint (Phase 3 = Toxiproxy-Adresse) |
| `AWS_REGION` | `eu-central-1` | Region fuer den SQS-Client |

Kein Endpoint ist hartkodiert — das Queue-Ziel ist eine Variable und damit austauschbar.

## Deploy

    docker compose --env-file .env up -d --build

`inventory` startet erst, wenn `db` den Healthcheck besteht
(`depends_on: condition: service_healthy`) — kein Start-Race.

## Testen

    curl -s localhost:8000/healthz      # 200, Liveness
    curl -s localhost:8000/readyz       # 200, prueft DB-Verbindung
    curl -s -X POST localhost:8000/movements \
      -H 'Content-Type: application/json' \
      -d '{"sku":"PAL-001","quantity":10,"warehouse":"DC"}'   # 201
    curl -s localhost:8000/movements    # Liste, neueste zuerst
    curl -s localhost:8000/metrics | grep '^inventory_'

Dieselben Checks laufen automatisiert in der CI
(`.github/workflows/build-and-test.yml`) bei jedem Push.

## Destroy / Redeploy

    docker compose --env-file .env down -v    # inkl. pgdata-Volume -> Clean-Slate
    docker compose --env-file .env up -d --build

Nach `down -v` ist die DB leer; das Schema legt sich beim Start idempotent neu an.
Der Stack kommt reproduzierbar aus dem Nichts hoch.

## Endpunkte

| Methode | Pfad | Zweck |
|---|---|---|
| GET | `/healthz` | Liveness (prueft nichts) |
| GET | `/readyz` | Readiness (prueft DB-Verbindung) |
| POST | `/movements` | Lagerbewegung anlegen |
| GET | `/movements` | Bewegungen lesen |
| GET | `/metrics` | Prometheus-Exposition |

## Betriebshinweise

- Build-Kontext der App liegt in `apps/inventory/` (Dockerfile, requirements.txt,
  .dockerignore dort, nicht eine Ebene hoeher) — sonst findet `compose build` kein Dockerfile.
- "Container gestartet" heisst nicht "App bereit": uvicorn + Lifespan (Pool, Schema)
  brauchen einen Moment. Deshalb `/readyz` und in der CI eine Readiness-Schleife statt `sleep`.
- `node_exporter` laeuft mit `network_mode: host` / `pid: host` (read-only Root-Mount) —
  noetig fuer echte Host-Metriken, nur auf dem Lab-Host vorgesehen, nicht extern exponiert.
  In der CI bleibt der Service bewusst aussen vor.
