# hybrid-ops-lab

Showcase-Projekt für eine DevOps-Rolle (Hybrid Cloud / Data Center).

Demonstriert einen Event-Driven-Flow über zwei simulierte Standorte:
ein Data-Center-Stack (`site-dc`) und einen Cloud-Stack (`site-cloud`),
verbunden über eine gedrosselte Netzwerkstrecke (Toxiproxy).

## Architektur-Überblick

![Architektur-Überblick](docs/img/architecture.png)

Das Diagramm zeigt die Zielarchitektur; der SQS-Endpoint ist im Lab ElasticMQ
statt LocalStack (siehe [ADR-005](docs/decisions/005-elasticmq-statt-localstack.md)).
Der Umsetzungsstand steht im Abschnitt [Status](#status).

## Infrastructure as Code (OpenTofu)

Die Umgebung ist durchgängig mit OpenTofu beschrieben — von den virtuellen
Maschinen bis zur Queue. Secrets und echte Werte (API-Token, IPs, SSH-Keys) liegen
ausschließlich in gitignorten `terraform.tfvars`; im Code stehen nur typisierte
Variablen.

**VM-Provisionierung — `infra/proxmox/`.** Beide Standort-VMs werden per `for_each`
über eine Site-Map aus einem Ubuntu-Template geklont: `hol-site-dc` (2 vCPU / 4 GB)
und `hol-site-cloud` (4 vCPU / 8 GB). cloud-init setzt statische IP, den Benutzer
`ops` und die SSH-Keys. Provider `bpg/proxmox`. Die beiden „Standorte" sind damit
reproduzierbar und versioniert, nicht handgeklickt.

**Wiederverwendbares Queue-Modul — `infra/modules/event-queue/`.** Kapselt eine
**Standard-SQS-Queue** (kein FIFO, at-least-once — **keine** Exactly-once-Behauptung)
mit Visibility-Timeout und Retention als Variablen, gepinnt auf
`hashicorp/aws ~> 6.0`. Das Modul erzeugt zusätzlich eine **Dead-Letter-Queue** und
setzt eine **native Redrive-Policy** (`maxReceiveCount = 5`): dauerhaft nicht
verarbeitbare Nachrichten verschiebt SQS/ElasticMQ selbst in die DLQ — die Anwendung
verschiebt nichts manuell. Hintergrund:
[ADR-007](docs/decisions/007-dlq-and-redrive.md). Das Environment
`infra/environments/cloud/` ruft das Modul auf und richtet den AWS-Provider auf den
lokalen ElasticMQ-Endpoint (Dummy-Credentials + `skip_*`-Flags, damit kein echtes
AWS angesprochen wird).

**Bewusste Trennung statt Workaround.** Gegen den Emulator wird *kein* `tofu apply`
gefahren: Der AWS-Provider liest nach dem Anlegen `GetQueueAttributes` zurück und
vergleicht den vollständigen Attributsatz, den ElasticMQ nicht deckungsgleich
liefert (Timeout `notequal`). Der naheliegende „Fix" — einen Security-Default
abzuschwächen, nur damit das Tooling durchläuft — wurde bewusst vermieden. Das
Modul bleibt ein AWS-portables Artefakt (über `tofu validate` / `tofu plan` belegt),
die lokale Queue wird deklarativ in `sites/cloud/elasticmq.conf` bereitgestellt, und
`apply` zielt auf echtes AWS. Hintergrund:
[ADR-005](docs/decisions/005-elasticmq-statt-localstack.md).

**Generierte Doku & automatische Checks.** Jedes Modul hat eine per
[terraform-docs](https://github.com/terraform-docs/terraform-docs) generierte
Referenz (Inputs/Outputs/Provider): [`event-queue`](infra/modules/event-queue/),
[`proxmox`](infra/proxmox/), [`environments/cloud`](infra/environments/cloud/).
`tofu fmt`, `tofu validate`, `tflint` und ein `trivy`-Security-Scan laufen als
pre-commit-Hooks und in der CI — der SSE-Default der Queue wurde z. B. durch genau
diesen Scan erzwungen.

## Monitoring & Incident-Nachweis

Prometheus scrapt beide Standorte (node-Metriken, die inventory-App und Toxiproxy)
und probt die Strecke zusätzlich per Blackbox-Exporter. Ein provisioniertes
Grafana-Dashboard macht die Signale sichtbar.

![Grafana-Dashboard im Normalbetrieb](docs/img/grafana-dashboard.png)

Normalbetrieb: Die Probe-Latenz über die Strecke liegt im einstelligen
Millisekundenbereich, die Probe ist erfolgreich (Wert 1), der Durchsatz durch den
Toxiproxy-Proxy ist stabil, und beide Sites liefern ihre node-Metriken.

![Grafana-Dashboard während der gedrosselten Strecke](docs/img/grafana-incident.png)

Incident: Eine über Toxiproxy injizierte Latenz von ~7 s lässt die Probe-Latenz
sprunghaft ansteigen — die beiden Plateaus sind zwei Drossel-Zyklen. Die Probe
bleibt dabei erfolgreich (Wert 1): die Strecke ist langsam, nicht tot. Nach dem
Aufheben der Störung fällt die Latenz sofort zurück. Reproduzierbar über die
Chaos-Skripte in `ops/chaos/`; Ablauf und Diagnose im
[Runbook](docs/runbook-link-degradation.md).

Auf dasselbe Signal feuert eine Alert-Regel: `StreckeDegraded`
(`probe_duration_seconds > 2` für 1 Minute) geht nach anhaltender Drosselung in
den Zustand *firing* und wird an den Alertmanager geroutet.

![Alertmanager mit aktivem StreckeDegraded-Alert](docs/img/alertmanager-firing.png)

Der Alertmanager nutzt im Lab einen Null-Receiver (kein echter Versand, keine
Secrets im Repo); eine zweite Regel `StreckeDown` (`probe_success == 0`) deckt den
Totalausfall ab.

**Consumer- und Queue-/DLQ-Monitoring (Gate D2, im Repository).** Der Consumer
exponiert niedrig-kardinale Metriken für Liveness und Readiness, Receive-/DB-/
Delete-Fehler, Redeliveries (`ApproximateReceiveCount > 1`), Poison-/Conflict-Signale
(Validierungsfehler, Integritätskonflikte) sowie Main-Queue- und DLQ-Tiefe.
Dazu gehören Alert-Regeln (`ConsumerDown`, `ConsumerNotReady`, `MainQueueBacklog`,
`DLQNotEmpty` u. a.) mit großzügigen `for`-Dauern. **Wichtig:** Die Queue-/DLQ-Tiefe
wird vom Consumer abgefragt — fällt der Consumer aus, fehlen diese Werte, und
`ConsumerDown` ist dann das primäre Signal. Es gibt **keine** unabhängige
Queue-Überwachung. Scrape-Pfad und Prometheus-Target werden reproduzierbar
vorbereitet; eine **Live-Verifikation auf der VM steht noch aus**.

## Status

Alle nachfolgend aufgeführten Basisphasen 1–7 wurden umgesetzt und in der
Lab-Umgebung verifiziert.

| Phase | Inhalt | Stand |
|-------|--------|-------|
| 1 | Repo-Skeleton, Security-Tooling, CI, ADRs, VM-Provisionierung (Proxmox/OpenTofu) | ✅ |
| 2 | site-dc: inventory-App (FastAPI), Postgres, node_exporter | ✅ |
| 3 | site-cloud: SQS-Endpoint (ElasticMQ), node_exporter, OpenTofu-Queue-Modul | ✅ |
| 4 | Consumer auf k3d (at-least-once), Ende-zu-Ende verifiziert | ✅ |
| 5 | Toxiproxy als Strecke, Incident-Szenario, Chaos-Skripte, Runbook | ✅ |
| 6 | Monitoring: Prometheus, Grafana, Alertmanager, Blackbox | ✅ |
| 7 | make-Orchestrierung beider Sites, README | ✅ |

### Phase 2B — Transactional Outbox & kontrollierter site-dc-Upgrade (Gate A)

Aufbauend auf dem Showcase wurde `site-dc` auf das **Transactional-Outbox-Muster**
([ADR-006](docs/decisions/006-transactional-outbox.md)) gehoben: `POST /movements`
schreibt Bewegung **und** Event atomar in einer PostgreSQL-Transaktion, statt im
Request-Pfad an eine Queue zu publizieren. Das Upgrade einer **bestehenden**
Installation lief kontrolliert und idempotent über
[`ops/deploy/upgrade-site-dc.sh`](docs/runbook-phase-2b-upgrade-site-dc.md)
(Preflight → Rollout → Resume).

| Aspekt | Stand |
|---|---|
| Phase-2B-Migration (`0001`/`0002`/`0003`, `event_outbox` inkl. Backfill) | ✅ abgeschlossen |
| Kontrollierter Rollout + Safe Resume (atomarer State, `flock`) | ✅ abgeschlossen |
| **Gate A** (technisch und formal) | ✅ abgeschlossen |
| Events (`EVENTS_ENABLED`) | ⏸️ deaktiviert (`false`) |
| Outbox-Einträge | `pending` (kein Publish im HTTP-Request-Pfad) |
| Event-Flow (Phase 3) | siehe Abschnitt unten (Gate D1/D2 im Repo, nicht live) |

Der bewiesene Live-Zustand, die erhaltene Fehlerhistorie und der ausgeführte
Resume-Betriebsnachweis stehen im
[Handoff Phase 2B / Gate A](docs/handoff-phase-2b-gate-a.md); der Resume-Pfad selbst
im [Runbook](docs/runbook-phase-2b-upgrade-site-dc.md#resume--read-only-nachverifikation-und-state-abschluss).

### Phase 3 — Event-Flow (Consumer-Idempotenz, DLQ, Monitoring)

Phase 3 baut den Weg `event_outbox → separater Publisher → Queue → Consumer` auf.
`EVENTS_ENABLED=false`, der Publisher ist **standardmäßig deaktiviert** und Phase 3 ist
**nicht aktiviert**. Wichtige Unterscheidung: **im Repository implementiert & gemerged**
ist nicht dasselbe wie **live deployed/auf der VM verifiziert**. D1/D2/D3A/D3B1 sind im
Repository umgesetzt, aber **noch nicht live deployed/verifiziert**.

| Gate | Inhalt | Stand |
|---|---|---|
| Gate A / Phase 2B | Transactional Outbox, kontrollierter site-dc-Upgrade | ✅ abgeschlossen & verifiziert |
| Gate D1 | idempotente Consumer-Runtime (Inbox/Projection verdrahtet, Commit vor Queue-Delete, Transport-/Business-Duplikate erkannt, Konflikte/Fehler fail closed) | 🔧 im Repo gemerged · ⛔ nicht live deployed |
| Gate D2 | Main Queue + DLQ, native Redrive-Policy (`maxReceiveCount = 5`), Poison-Message-Policy, Consumer-/Queue-/DLQ-Metriken & Alerts, reproduzierbarer Scrape-Pfad | 🔧 im Repo gemerged · ⛔ nicht live deployed |
| Gate D3A | lease-basierter Outbox-Publisher-Kern (Claim/Fencing, einzelnes SendMessage, eigene Least-Privilege-Rolle, Migration `0004`, disabled by default) | 🔧 im Repo gemerged · ⛔ nicht live deployed |
| Gate D3B1 | Publisher-Service-Wiring (Compose disabled), Secret-Isolation, Route-Config, Prometheus-Scrape + enabled-gated Alerts, kontrolliertes Upgrade-Skript + Fail-closed-Guards | 🔧 im aktuellen Branch implementiert · ⛔ nicht live deployed |
| Gate D3B2 | kontrollierter Live-Rollout + bewusste Aktivierung + E2E-Nachweis | ⬜ ausstehend |
| Phase 3 gesamt | Event-Versand aktiviert | ⛔ nicht aktiviert (`EVENTS_ENABLED=false`) |

Details: [Idempotenz](docs/idempotency.md) (D1), [ADR-007](docs/decisions/007-dlq-and-redrive.md)
(D2), [ADR-008](docs/decisions/008-outbox-publisher.md) (D3A/D3B1) und das
[Phase-3-Runtime-Upgrade-Runbook](docs/runbook-phase-3-runtime-upgrade.md). Nichts davon
ist produktiv, live oder vollständig ausgerollt.

### Nächster Schritt — Gate D3B2 (kontrollierter Live-Rollout)

Noch **nicht** ausgeführt (kein Live-Zugriff in diesem Stand): D1/D2 live deployen &
verifizieren, Migration `0004` live (Variante B), Inventory aktualisieren, Publisher
**disabled** deployen, Monitoring (Scrape + Alerts) prüfen, **bewusste** Aktivierung über
ein separates Gate, End-to-End-Nachweis über den echten Outbox-Pfad, Disable-/Rollback-Test.

## Schnellstart

Voraussetzungen:

- Zwei Ubuntu-24-VMs im selben Netz, vom Desktop per SSH erreichbar:
  `site-dc` (Docker) und `site-cloud` (Docker, k3d, kubectl).
- VM-Einrichtung über die Bootstrap-Skripte in `ops/bootstrap/`.
- Pro Stack eine lokale `.env` auf der jeweiligen VM (Vorlagen: `*/.env.example`).

Orchestriert wird vom Desktop aus über `make` (per ssh auf die VMs); die
VM-Adressen kommen aus `make.env`:

```bash
cp make.env.example make.env   # DC_HOST / CLOUD_HOST eintragen

make up              # beide Sites + Monitoring hoch, Consumer deployen
make check           # Prometheus-Targets + Consumer-Status
make demo-incident   # Strecken-Latenz einschalten (Toxiproxy)
make demo-restore    # Störung aufheben
make down            # Compose-Stacks stoppen (k3d-Cluster bleibt)
```

## Struktur

```
hybrid-ops-lab/
├── apps/
│   ├── inventory/        # FastAPI + Postgres: schreibt Movement + Outbox-Event atomar
│   └── consumer/         # FastAPI-Consumer (at-least-once, idempotent), laeuft auf k3d
├── sites/
│   ├── dc/               # Docker-Compose: inventory, Postgres, node_exporter
│   └── cloud/            # Docker-Compose: ElasticMQ (SQS), Toxiproxy, node_exporter
│       └── k8s/          # Consumer-Manifest (Namespace, Deployment, Service)
├── monitoring/           # Prometheus, Grafana, Alertmanager, Blackbox
├── infra/
│   ├── modules/event-queue/   # OpenTofu-Modul: SQS-Queue (AWS-portabel)
│   ├── environments/cloud/    # Tofu gegen ElasticMQ (lokal nicht applied)
│   └── proxmox/               # OpenTofu: VM-Provisionierung
├── ops/
│   ├── bootstrap/        # VM-Setup (Docker, k3d)
│   ├── deploy/           # deploy-consumer.sh (k3d-Gateway-IP zur Deploy-Zeit)
│   └── chaos/            # degrade-/restore-link.sh
├── docs/
│   ├── decisions/        # ADRs
│   ├── img/              # Architektur + Monitoring-Screenshots
│   └── runbook-link-degradation.md
└── Makefile              # Desktop-Orchestrierung (make up/check/demo-*/down)
```

**Event-Flow (Soll):** `inventory` schreibt Movement und Outbox-Event **atomar** in
einer Transaktion (kein direkter Publish im HTTP-Request-Pfad); ein **künftiger,
separater Publisher** (Gate D3, noch nicht implementiert) übernimmt
`event_outbox → Queue`; der `consumer` verarbeitet Queue-Events **idempotent** über
die `event_id` (Inbox/Projection). Siehe [Idempotenz](docs/idempotency.md),
[ADR-006](docs/decisions/006-transactional-outbox.md) und
[ADR-007](docs/decisions/007-dlq-and-redrive.md).

## Sicherheit

Keine produktiven Secrets im Repository. Siehe [SECURITY.md](SECURITY.md).

## Entscheidungen

- [ADR-001](docs/decisions/001-warum-lokal-statt-aws.md) – Warum lokal statt echtes AWS
- [ADR-002](docs/decisions/002-zwei-vms-statt-einer.md) – Warum zwei VMs statt einer
- [ADR-003](docs/decisions/003-toxiproxy-als-strecke.md) – Toxiproxy als Standortverbindung
- [ADR-004](docs/decisions/004-proxmox-provisionierung.md) – Proxmox-Provisionierung via OpenTofu
- [ADR-005](docs/decisions/005-elasticmq-statt-localstack.md) – ElasticMQ statt LocalStack als SQS-Endpoint
- [ADR-006](docs/decisions/006-transactional-outbox.md) – Transactional Outbox statt Publish im Request-Pfad
- [ADR-007](docs/decisions/007-dlq-and-redrive.md) – DLQ, native Redrive-Policy und Poison-Message-Behandlung
- [Idempotenz](docs/idempotency.md) – Consumer-Idempotenz (Inbox/Projection, Duplikat-/Konfliktbehandlung)
