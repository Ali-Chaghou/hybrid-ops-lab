<!-- Kompakte Navigation als Headbar; erscheint direkt unter der nativen GitHub-Leiste.
     Nur relative Repo-Links, keine externen Dienste. -->
<p>
  <a href="README.md"><kbd>Übersicht</kbd></a>
  <a href="docs/README.md"><kbd>Dokumentation</kbd></a>
  <a href="docs/roadmap.md"><kbd>Status &amp; Roadmap</kbd></a>
  <a href="docs/evidence-index.md"><kbd>Nachweise</kbd></a>
  <a href="docs/README.md#entscheidungen"><kbd>Entscheidungen</kbd></a>
  <a href="docs/README.md#runbooks"><kbd>Runbooks</kbd></a>
</p>

# hybrid-ops-lab

hybrid-ops-lab ist ein synthetisches Hybrid-Cloud-Lab für einen event-getriebenen
Bestandsprozess über zwei simulierte Standorte. Das Projekt verbindet reproduzierbare
Infrastruktur, Transactional Outbox, Queue/DLQ, einen idempotenten Consumer, Monitoring
und kontrollierte Rollouts.

Consumer, Queue/DLQ und Monitoring sind auf site-cloud live verifiziert. Der
Outbox-Publisher wurde über den kontrollierten D3B2.3-Aktivierungspfad aktiviert; der
reale Ende-zu-Ende-Eventfluss von der Inventory-API über Transactional Outbox, Publisher,
Main Queue und Consumer bis in die Movement-Projection ist damit im Lab live verifiziert,
ebenso der kontrollierte Disable-/Re-enable-Pfad. Noch offen sind die vollständigen
Fehler-, Redelivery-, Transport-Duplikat- und DLQ-Nachweise sowie der abschließende
D3B2.3-Gesamtnachweis; das System bleibt at-least-once. Es handelt sich um eine
synthetische Lab-Umgebung und nicht um eine Produktionsumgebung.

## Architektur

![Architektur des hybrid-ops-lab](docs/img/architecture.png)

Das Diagramm zeigt die Gesamtarchitektur des Labs. site-dc speichert Bestandsbewegungen
und Outbox-Einträge. site-cloud stellt Queue, DLQ, Consumer und Monitoring bereit.
Toxiproxy simuliert die Netzwerkstrecke zwischen beiden Standorten. Der Publisher-Pfad
ist standardmäßig deaktiviert, wurde aber über den kontrollierten D3B2.3-Aktivierungspfad
aktiviert und der reale Ende-zu-Ende-Eventfluss im Lab nachvollzogen.

## Aktueller Stand

| Bereich | Stand |
|---|---|
| Transactional Outbox auf site-dc | Im Lab verifiziert |
| Main Queue und DLQ | Runtime vorhanden und live geprüft; vollständiger DLQ-Endnachweis offen |
| Idempotenter Consumer | Implementiert und live verifiziert |
| Prometheus, Grafana und Alertmanager | Live verifiziert |
| Publisher-Aktivierung (kontrollierter D3B2.3-Pfad) | Live verifiziert |
| Realer Ende-zu-Ende-Eventfluss | Live verifiziert |
| Kontrollierter Disable-/Re-enable-Pfad | Live verifiziert |
| Failure Injection / Redelivery | Deploy-Pfad implementiert, Runtime-Test offen |
| Transport-Duplikat- und DLQ-Endnachweis | Offen |
| D3B2.3-Gesamtabschluss | Offen |

Der Consumer-Rollout ist im [D3B2.1-Rollout-Nachweis](docs/handoff-d3b2.1-complete.md)
belegt, die site-dc-Migration im [D3B2.2-Abschlussnachweis](docs/evidence-d3b2.2.md).
Das System bleibt at-least-once; Exactly-once wird nicht behauptet.

### Aktueller Laufzeitnachweis

Im Lab real durchlaufen (synthetische Lab-Laufzeit, keine Produktion):

- Ein echtes `POST /movements` durchlief den Pfad Inventory-API → Transactional Outbox →
  Publisher → Main Queue → Consumer-Inbox → Movement-Projection; der Event wurde in der
  Outbox als `published` bestätigt und ohne offenen Claim finalisiert.
- Der Consumer verarbeitete genau den erwarteten Event als `applied`; die Projection
  enthielt korrekte SKU-, Mengen- und Warehouse-Werte, und Main Queue und DLQ waren nach
  der Verarbeitung wieder leer.
- Während der Läufe traten keine Publish-Fehler, Datenbank- oder Delete-Fehler und keine
  Finalize-Konflikte auf.
- Bei kontrolliert deaktiviertem Publisher blieb ein neues echtes API-Event `pending` in
  der Outbox — ohne Claim, mit leerer Main Queue und DLQ und unverändertem Consumer.
- Nach kontrollierter Reaktivierung wurde genau dieses wartende Event veröffentlicht,
  konsumiert und in die Projection übernommen; Main Queue und DLQ waren anschließend
  erneut leer.

Strukturierte Werte, IDs und Metrik-Snapshots dieses Laufs stehen im
[D3B2.3-Zwischennachweis](docs/evidence-d3b2.3.md) (Zwischenstand, Gesamtnachweis offen).

## Laufzeit im Lab

![Grafana-Dashboard im Normalbetrieb](docs/img/grafana-dashboard.png)

Das Dashboard zeigt die Netzwerkprobe, die Latenz über die simulierte Strecke sowie
Metriken der beiden Lab-Standorte.

## Das Problem in einfachen Worten

Im Lager (`site-dc`) entsteht eine Bestandsbewegung — etwa „10 Stück eingebucht". Diese
Bewegung soll zuverlässig an einen zweiten Standort (`site-cloud`) übertragen und dort
verarbeitet werden, auch wenn die Verbindung langsam ist oder kurz ausfällt.

Drei typische Probleme dürfen dabei nicht zu falschen Beständen führen:

- **Netzwerkprobleme** — die Strecke kann langsam oder unterbrochen sein.
- **Doppelte Nachrichten** — dieselbe Bewegung kann mehrfach ankommen (at-least-once).
- **Fehlerhafte Nachrichten** — eine dauerhaft nicht verarbeitbare Nachricht darf den
  Betrieb nicht blockieren.

Der aktuelle Stand beweist im Lab: den **Consumer**, seine **Idempotenz** (eine doppelt
zugestellte Bewegung wirkt nur einmal), **Queue + Dead-Letter-Queue** und das
**Monitoring**. Der **Publisher-Pfad** (der Bewegungen aus `site-dc` automatisch in die
Queue stellt) wurde über den kontrollierten D3B2.3-Pfad **aktiviert**, und der **reale
Ende-zu-Ende-Fluss** wurde einmal komplett nachvollzogen. Die Runtime-Nachweise für
**doppelte** und **fehlerhafte** Nachrichten (Redelivery, Transport-Duplikat,
Validation-/Poison-Fall und DLQ-Endnachweis) stehen dagegen noch aus.

## Architektur in 60 Sekunden

- **site-dc** erzeugt Bestandsbewegungen und schreibt sie zusammen mit einem
  Outbox-Eintrag atomar in PostgreSQL.
- **site-cloud** enthält Queue (ElasticMQ), Consumer und Monitoring.
- **Toxiproxy** simuliert die Netzwerkstrecke zwischen beiden Standorten.
- Der **Publisher-Pfad** (`event_outbox → Queue`) ist standardmäßig deaktiviert, wurde
  aber über den kontrollierten D3B2.3-Pfad aktiviert und real durchlaufen.

Das Diagramm zeigt die Gesamtarchitektur. Live verifiziert sind der Consumer-, Queue-/
DLQ- und Monitoring-Pfad auf site-cloud sowie der reale Ende-zu-Ende-Eventfluss über den
kontrolliert aktivierten Publisher. Der SQS-Endpoint ist im Lab ElasticMQ statt LocalStack
([ADR-005](docs/decisions/005-elasticmq-statt-localstack.md)); der Umsetzungsstand steht
im Abschnitt [Status](#status).

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

## Verhalten bei Netzwerkproblemen

Prometheus scrapt beide Standorte (node-Metriken, die inventory-App und Toxiproxy)
und probt die Strecke zusätzlich per Blackbox-Exporter. Ein provisioniertes
Grafana-Dashboard macht die Signale sichtbar (Normalbetrieb siehe Abschnitt
[Laufzeit im Lab](#laufzeit-im-lab)).

### Simulierte Netzwerkverschlechterung

![Grafana während einer gedrosselten Netzwerkstrecke](docs/img/grafana-incident.png)

- Toxiproxy erhöht die Latenz kontrolliert (~7 s) — die beiden Plateaus sind zwei
  Drossel-Zyklen.
- Die Anwendung bleibt erreichbar; die Probe bleibt erfolgreich: die Strecke ist langsam,
  nicht tot.
- Das Monitoring zeigt den Unterschied zwischen langsam und nicht erreichbar. Nach dem
  Aufheben der Störung fällt die Latenz sofort zurück.

Reproduzierbar über die Chaos-Skripte in `ops/chaos/`; Ablauf und Diagnose im
[Runbook](docs/runbook-link-degradation.md).

### Alerting

![Alertmanager mit aktivem StreckeDegraded-Alert](docs/img/alertmanager-firing.png)

- Die Regel `StreckeDegraded` (`probe_duration_seconds > 2` für 1 Minute) wird erst nach
  anhaltender Verschlechterung ausgelöst und an den Alertmanager geroutet.
- Der Alertmanager nutzt im Lab einen Null-Receiver — keine echten Benachrichtigungsziele
  und keine Secrets im Repository.
- Eine zweite Regel `StreckeDown` (`probe_success == 0`) deckt den Totalausfall ab.

**Consumer- und Queue-/DLQ-Monitoring (Gate D2).** Der Consumer
exponiert niedrig-kardinale Metriken für Liveness und Readiness, Receive-/DB-/
Delete-Fehler, Redeliveries (`ApproximateReceiveCount > 1`), Poison-/Conflict-Signale
(Validierungsfehler, Integritätskonflikte) sowie Main-Queue- und DLQ-Tiefe.
Dazu gehören Alert-Regeln (`ConsumerDown`, `ConsumerNotReady`, `MainQueueBacklog`,
`DLQNotEmpty` u. a.) mit großzügigen `for`-Dauern. **Wichtig:** Die Queue-/DLQ-Tiefe
wird vom Consumer abgefragt — fällt der Consumer aus, fehlen diese Werte, und
`ConsumerDown` ist dann das primäre Signal. Es gibt **keine** unabhängige
Queue-Überwachung. Dieser Scrape-Pfad ist mit Gate D3B2.1 **live im Lab verifiziert**
(`/-/ready` 200, Consumer-Target `up`, Rule-Gruppen `consumer` und `queue` geladen,
**kein** Publisher-Target aktiv) — siehe [Abschlussnachweis D3B2.1](docs/handoff-d3b2.1-complete.md).

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
| Event-Erzeugung (`EVENTS_ENABLED`) | Durch D3B2.1 nicht verändert |
| Outbox-Publisher | Zu Gate A nicht aktiviert (aktueller Stand: Abschnitt [Phase 3](#phase-3--event-flow-consumer-idempotenz-dlq-monitoring)) |
| Outbox-Einträge | `pending` (kein Publish im HTTP-Request-Pfad) |
| Event-Flow (Phase 3) | siehe Abschnitt [Phase 3](#phase-3--event-flow-consumer-idempotenz-dlq-monitoring); aktueller Stand: E2E live verifiziert, Fehler-/DLQ-Endnachweis offen |

Der bewiesene Live-Zustand, die erhaltene Fehlerhistorie und der ausgeführte
Resume-Betriebsnachweis stehen im
[Handoff Phase 2B / Gate A](docs/handoff-phase-2b-gate-a.md); der Resume-Pfad selbst
im [Runbook](docs/runbook-phase-2b-upgrade-site-dc.md#resume--read-only-nachverifikation-und-state-abschluss).

### Phase 3 — Event-Flow (Consumer-Idempotenz, DLQ, Monitoring)

Phase 3 baut den Weg `event_outbox → separater Publisher → Queue → Consumer` auf.
Der Publisher ist standardmäßig deaktiviert, wurde jedoch über den kontrollierten
D3B2.3-Aktivierungspfad aktiviert; der reale Ende-zu-Ende-Eventfluss und der kontrollierte
Disable-/Re-enable-Pfad sind im Lab live verifiziert. Implementiert im Repository ist nicht
dasselbe wie in der Lab-Laufzeit verifiziert. Mit D3B2.1 sind D1 und D2 auf `site-cloud`
live verifiziert. Offen bleiben die Fehlerinjektions-, Redelivery-, Transport-Duplikat- und
DLQ-Nachweise sowie der abschließende D3B2.3-Gesamtnachweis; Phase 3 ist deshalb noch nicht
formal abgeschlossen.

Legende:

- **Implementiert** — Code und Tests sind vorhanden.
- **Live verifiziert** — Funktion wurde in der Lab-Laufzeit geprüft.
- **Aktiviert** — Komponente nimmt am laufenden Eventfluss teil.
- **Ausstehend** — Umsetzung oder Laufzeitnachweis fehlt noch.

| Bereich | Aktueller Stand |
|---|---|
| Basis-Lab und Infrastruktur | Im Lab verifiziert |
| Transactional Outbox / Gate A | Im Lab verifiziert |
| Consumer-Idempotenz / D1 | Auf site-cloud live verifiziert |
| Queue, DLQ und Monitoring / D2 | Auf site-cloud live verifiziert |
| Publisher-Kern / D3A | Implementiert; standardmäßig deaktiviert |
| Publisher-Wiring / D3B1 | Implementiert; standardmäßig deaktiviert |
| Consumer-Rollout / D3B2.1 | Abgeschlossen und live verifiziert |
| site-dc-Migration / D3B2.2 | Abgeschlossen und live verifiziert; Publisher zu diesem Gate deaktiviert |
| Publisher-Aktivierung und realer E2E-Fluss / D3B2.3 | Live verifiziert |
| Kontrollierter Disable-/Re-enable-Pfad / D3B2.3 | Live verifiziert |
| Fehlerinjektion, Redelivery, Duplikat, DLQ-Endnachweis / D3B2.3 | Deploy-Pfad implementiert, Runtime-Test offen |
| D3B2.3-Gesamtabschluss und Phase 3 gesamt | Offen |

„Live verifiziert" bezieht sich ausschließlich auf die synthetische Lab-Laufzeit, nicht auf
eine Produktionsumgebung. Die technischen Laufzeitnachweise stehen im
[D3B2.1-Abschlussnachweis](docs/handoff-d3b2.1-complete.md) und im
[D3B2.2-Abschlussnachweis](docs/evidence-d3b2.2.md). Hintergrund:
[Idempotenz](docs/idempotency.md) (D1), [ADR-007](docs/decisions/007-dlq-and-redrive.md)
(D2), [ADR-008](docs/decisions/008-outbox-publisher.md) (D3A/D3B1) und das
[Phase-3-Runtime-Upgrade-Runbook](docs/runbook-phase-3-runtime-upgrade.md).

### Nächster Schritt — Gate D3B2.3 (Publisher-Aktivierung und E2E)

D3B2.2 wurde am 30. Juni 2026 im Lab erfolgreich verifiziert. Migration `0004`,
neue Inventory-Runtime, deaktivierter Publisher, Least-Privilege-Rolle und Monitoring
sind live nachgewiesen. Die Ergebnisse stehen im
[D3B2.2-Abschlussnachweis](docs/evidence-d3b2.2.md).

In **D3B2.3** wurden inzwischen die folgenden Schritte real im Lab durchlaufen:

- der Publisher wurde über den kontrollierten Aktivierungspfad bewusst aktiviert;
- der Weg Inventory-API → Transactional Outbox → Publisher → Main Queue → Consumer-Inbox →
  Movement-Projection wurde für ein echtes `POST /movements` durchgängig nachgewiesen;
- der kontrollierte Disable-/Re-enable-Pfad wurde getestet: bei deaktiviertem Publisher
  blieb ein neues echtes API-Event `pending` ohne Claim, mit leerer Main Queue und DLQ;
  nach Reaktivierung wurde genau dieses Event veröffentlicht, konsumiert und projiziert.

Noch offen sind — und werden erst nach ihrem Laufzeitnachweis als erledigt markiert:

- One-shot-Fehlerinjektion nach DB-Commit und vor Queue-Delete;
- Redelivery nach Visibility Timeout und ein Transport-Duplikat in der realen Laufzeit;
- Poison-/Validierungsfehlerfall und der vollständige DLQ-Redrive-Nachweis;
- der abschließende D3B2.3-Gesamtnachweis.

Für die offenen Fehlerszenarien ist ein kontrollierter Deploy-Pfad implementiert und
lokal getestet: das vorhandene immutable Consumer-Image wird wiederverwendet (kein neuer
Build unter dem alten Release-Tag), die Injektion ist ausschließlich explizit aktivierbar,
der Default bleibt deaktiviert, bei Image-Mismatch wird fail-closed abgebrochen, und
Contract-Tests sind vorhanden. Der eigentliche Runtime-Nachweis steht noch aus.

Das System bleibt at-least-once; Exactly-once wird nicht behauptet. Phase 3 ist damit
noch nicht formal abgeschlossen. Vollständiger Fahrplan: [docs/roadmap.md](docs/roadmap.md).

## Schnellstart

Voraussetzungen:

- Zwei Ubuntu-24-VMs im selben Netz, vom Desktop per SSH erreichbar:
  `site-dc` (Docker) und `site-cloud` (Docker, k3d, kubectl).
- VM-Einrichtung über die Bootstrap-Skripte in `ops/bootstrap/`.
- Pro Stack eine lokale `.env` auf der jeweiligen VM (Vorlagen: `*/.env.example`).

Orchestriert wird vom Desktop aus über `make` (per ssh auf die VMs); die
VM-Adressen kommen aus `make.env`. Dies setzt eingerichtete VMs und lokale `.env`-Dateien
voraus — ein frischer Clone startet **nicht** ohne diese Umgebung.

**Basis-Lab** (Standorte, Strecke, Monitoring, Incident-Demo):

```bash
cp make.env.example make.env   # DC_HOST / CLOUD_HOST eintragen

make up              # beide Sites + Monitoring hoch
make check           # Prometheus-Targets + Consumer-Status
make demo-incident   # Strecken-Latenz einschalten (Toxiproxy)
make demo-restore    # Störung aufheben
make down            # Compose-Stacks stoppen (k3d-Cluster bleibt)
```

**Kontrollierter D3B2.1-Pfad** (site-cloud-isolierter, release-gebundener Consumer-/
D1-/D2-Rollout — getrennt vom Basis-`make up`):

```bash
make cloud-up        # kontrollierter, release-gebundener Fresh Run (D3B2.1)
make cloud-state     # read-only: Rollout-State anzeigen
make cloud-check     # read-only: Release-/Monitoring-Prüfung
make cloud-resume    # NUR bei unvollständigem State desselben Release — nie blind verwenden
```

Ablauf und Fail-closed-Gates: [D3B2.1-Runbook](docs/runbook-d3b2-consumer-rollout.md).
Der bewiesene Endzustand steht im [Abschlussnachweis D3B2.1](docs/handoff-d3b2.1-complete.md).

## Struktur

```
hybrid-ops-lab/
├── apps/
│   ├── inventory/        # FastAPI + Postgres: schreibt Movement + Outbox-Event atomar
│   ├── consumer/         # FastAPI-Consumer (at-least-once, idempotent), laeuft auf k3d
│   └── publisher/        # apps/publisher: Outbox-Publisher (Lease/Fencing) — gemerged,
│                         #   standardmaessig deaktiviert; ueber den kontrollierten
│                         #   D3B2.3-Pfad aktiviert und real durchlaufen
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

**Event-Flow:** `inventory` schreibt Movement und Outbox-Event **atomar** in
einer Transaktion (kein direkter Publish im HTTP-Request-Pfad); ein **separater
Publisher** (Gate D3A/D3B1 implementiert/gemerged, **standardmäßig deaktiviert**, über den
kontrollierten D3B2.3-Pfad aktiviert und real durchlaufen) übernimmt `event_outbox → Queue`;
der `consumer` verarbeitet Queue-Events **idempotent** über die `event_id`
(Inbox/Projection). Siehe [Idempotenz](docs/idempotency.md),
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
- [ADR-008](docs/decisions/008-outbox-publisher.md) – Separater Outbox-Publisher (Lease/Fencing, standardmäßig deaktiviert)
- [Idempotenz](docs/idempotency.md) – Consumer-Idempotenz (Inbox/Projection, Duplikat-/Konfliktbehandlung)

Vollständiger Dokumentationsindex: [docs/README.md](docs/README.md) ·
Status & Roadmap: [docs/roadmap.md](docs/roadmap.md) ·
Nachweise: [docs/evidence-index.md](docs/evidence-index.md).
