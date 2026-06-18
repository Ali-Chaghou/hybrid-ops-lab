# site-dc

Standort "Rechenzentrum" des hybrid-ops-lab. Bildet eine klassische On-Prem-Umgebung nach: eine REST-Anwendung mit eigener Datenbank, lokal auf einem Host betrieben, mit Host-Metriken fuer das spaetere Monitoring.

## Komponenten

| Service | Image | Zweck |
|---|---|---|
| `db` | `postgres:16-alpine` | Datenhaltung; Cluster-Admin nur fuer Setup |
| `db-bootstrap` | `hol-inventory:dev` (One-Shot) | legt die Rollen idempotent an (`ops.db.bootstrap`) |
| `db-prepare` | `hol-inventory:dev` (One-Shot) | erstellt die Inventory-DB, Owner `inventory_admin` (`ops.db.prepare`) |
| `inventory-migrate` | `hol-inventory:dev` (One-Shot) | versionierte Migrationen als `inventory_admin` (`ops.db.migrate`) |
| `inventory` | `hol-inventory:dev` | REST-API als Least-Privilege-Rolle `inventory_app`, exponiert `/metrics` |
| `node_exporter` | `quay.io/prometheus/node-exporter:v1.11.1` | Host-Metriken (CPU, RAM, Disk) fuer Prometheus |

Die drei Setup-Services laufen genau einmal und beenden mit Exit 0. Reihenfolge:
`db` (healthy) → `db-bootstrap` → `db-prepare` → `inventory-migrate` → `inventory`.
Das Image wird aus dem **Repo-Root** gebaut (`context: ../..`, `dockerfile:
apps/inventory/Dockerfile`) und enthaelt App, `migrations/` und `ops/`.

## Voraussetzungen

Docker Engine inkl. Compose-Plugin. Auf einem frischen Ubuntu-Host installiert das
Bootstrap-Skript beides aus dem offiziellen Docker-Repo, **erzeugt beim ersten Lauf
eine geschuetzte `.env` (Modus 600) mit kryptografisch zufaelligen lokalen
Passwoertern**, validiert die Compose-Konfiguration (`config --quiet`) und zieht den
Stack hoch:

    sudo ./ops/bootstrap/setup-site-dc.sh

Eine **bestehende `.env` bleibt unveraendert** (keine Rotation, kein Ueberschreiben).
Das Skript ist idempotent, mehrfach ausfuehrbar.

## Konfiguration

Alle Werte kommen aus `.env` (12-factor). Die echte `.env` ist gitignored und wird
vom Bootstrap-Skript mit Zufallspasswoertern erzeugt. `.env.example` ist **nur eine
manuell nutzbare Vorlage** (oeffentliche Beispielwerte) und wird **nicht** automatisch
als aktive `.env` kopiert. Ohne Bootstrap legt man die `.env` manuell an und setzt
echte Werte — keine von `ops.db.bootstrap` abgelehnten Platzhalter (z. B. `change-me`).

| Variable | Bedeutung |
|---|---|
| `POSTGRES_USER` / `_PASSWORD` / `_DB` | Cluster-Admin (Superuser) + Maintenance-DB — nur Bootstrap/Prepare |
| `INVENTORY_DB` | Name der Inventory-Datenbank (von `db-prepare` angelegt) |
| `INVENTORY_ADMIN_PASSWORD` | Passwort der Rolle `inventory_admin` (Migrationen + Ownership) |
| `INVENTORY_APP_PASSWORD` | Passwort der Rolle `inventory_app` (Runtime, Least-Privilege) |
| `INVENTORY_HOST_PORT` | Host-Port-Mapping der App (Default `8000`) |
| `EVENTS_ENABLED` | SQS-Publish an/aus (ab Phase 3; Default `false`) |
| `SQS_ENDPOINT_URL` / `SQS_QUEUE_URL` | Queue-Endpoint/-URL (Phase 3 = Toxiproxy-Adresse) |
| `AWS_REGION` | Region fuer den SQS-Client |

**Die Inventory-Runtime erhaelt weder `PG_ADMIN_DSN` noch das `inventory_admin`-
Passwort** — nur eine `DATABASE_URL` fuer `inventory_app`.

## Deploy

    docker compose --env-file .env up -d --build

Compose faehrt die Setup-Kette deterministisch durch: `db-bootstrap` →
`db-prepare` → `inventory-migrate` (je `service_completed_successfully`), dann
`inventory` (erst nach erfolgreicher Migration). Die App fuehrt **keine** DDL aus;
sie prueft beim Start nur die Schema-Version (`db.check_schema()`).

Reproduzierbarer, isolierter Smoke-Test (eigener Compose-Projektname, frisches
Volume, Cleanup per `trap`):

    ./sites/dc/smoke-test.sh

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

Nach `down -v` ist die DB leer. Beim naechsten `up` baut die Setup-Kette
(Bootstrap → Prepare → Migrate) das Schema reproduzierbar neu auf — **nicht** die
Runtime. Der Stack kommt deterministisch aus dem Nichts hoch.

## Endpunkte

| Methode | Pfad | Zweck |
|---|---|---|
| GET | `/healthz` | Liveness (prueft nichts) |
| GET | `/readyz` | Readiness (prueft DB-Verbindung) |
| POST | `/movements` | Lagerbewegung anlegen |
| GET | `/movements` | Bewegungen lesen |
| GET | `/metrics` | Prometheus-Exposition |

## Betriebshinweise

- Build-Kontext ist das **Repo-Root** (`context: ../..`), damit ein gemeinsames Image
  App, `migrations/` und `ops/` enthaelt. Ein `.dockerignore` im Repo-Root schliesst
  Sensibles aus (insbesondere `infra/` mit `*.tfvars`) und haelt den Kontext klein.
- "Container gestartet" heisst nicht "App bereit": uvicorn + Lifespan (Pool, Schema)
  brauchen einen Moment. Deshalb `/readyz` und in der CI eine Readiness-Schleife statt `sleep`.
- `node_exporter` laeuft mit `network_mode: host` / `pid: host` (read-only Root-Mount) —
  noetig fuer echte Host-Metriken, nur auf dem Lab-Host vorgesehen, nicht extern exponiert.
  In der CI bleibt der Service bewusst aussen vor.
