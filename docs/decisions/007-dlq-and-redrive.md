# 007 — DLQ, native Redrive-Policy und Poison-Message-Behandlung

## Status
Akzeptiert (2026-06-21)

## Kontext
Die Movement-Queue (`inventory-movements`) ist eine **Standard-SQS-Queue** (kein
FIFO) mit **at-least-once**-Zustellung; die Reihenfolge ist nicht garantiert. Der
Consumer ist über die `event_id` idempotent (siehe [Idempotenz](../idempotency.md)).
Validierungsfehler und Integritätskonflikte werden **fail closed** behandelt — nie
still bestätigt. Ohne Gegenmaßnahme würde eine dauerhaft nicht verarbeitbare
„poison" Nachricht jedoch nach jedem Visibility-Timeout **endlos** erneut zugestellt.

Der Outbox-**Publisher** ist weiterhin **nicht** implementiert und Phase 3 ist
**nicht** deployed; dieses Dokument bereitet ausschließlich Queue/DLQ und Monitoring
vor, damit der Publisher später sicher aktiviert werden kann.

## Entscheidung

### Standard Queue + Visibility Timeout
Standard-Queue (kein FIFO; keine Exactly-once-/Ordering-Behauptung). Visibility
Timeout = 30 s: lange genug für die kurze Verarbeitung (Validierung + eine
DB-Transaktion + Delete), kurz genug für zügige Redelivery nach einem Fehler.

### Native SQS-Redrive-Policy statt App-Logik
Das Dead-Lettering übernimmt die **native** Redrive-Policy von SQS bzw. ElasticMQ.
Die Anwendung baut **keine** eigene „Send-to-DLQ"-Operation und trifft **keine**
Routing-Entscheidung anhand des Receive Counts — SQS/ElasticMQ bleibt Source of
Truth. Der Consumer löscht eine Nachricht nur nach erfolgreichem Commit bzw. bei
einem idempotent erkannten Duplikat; alle übrigen Fälle bleiben unbestätigt und
werden — bei wiederholtem Misserfolg — automatisch dead-lettert.

### `maxReceiveCount = 5`
Gewählt mit Bedacht: **nicht zu niedrig** (1–2 würde bei einem kurzen DB-/Queue-Blip
sofort dead-lettern), **nicht unbegrenzt** (eine echte Poison Message muss die
Main-Queue verlassen). Bei Visibility 30 s überspannt 5 Zustellungen ein
mehrminütiges Fehlerfenster. Hinweis: Bei einem DB-Ausfall **empfängt** der Consumer
gar nicht (Readiness-Gate), sodass der Receive Count solcher Nachrichten in dieser
Zeit nicht steigt — `maxReceiveCount` zählt damit vor allem echte wiederholte
Verarbeitungsfehler. Der Wert ist im Terraform-Modul (`var.max_receive_count`,
validiert 2–100) und in `elasticmq.conf` identisch gesetzt.

### DLQ
`inventory-movements-dlq` mit **eigener, langer Retention** (AWS: 14 Tage) zum
Inspizieren/Replayen. Im AWS-Modul zusätzlich eine `redrive_allow_policy`, die der
DLQ nur Nachrichten aus genau der Main-Queue erlaubt. SSE-SQS bleibt auf beiden
Queues aktiv. ElasticMQ bildet dasselbe Verhalten über `deadLettersQueue { name,
maxReceiveCount }` nach (Retention ist dort nicht konfigurierbar — Lab-Grenze).

### Retry vs. Redrive
- **Retry** = dieselbe Nachricht wird nach Visibility-Timeout erneut **in der
  Main-Queue** zugestellt (transiente Fehler: DB/Queue/Delete, Failure-Injection).
- **Redrive** = nach `maxReceiveCount` erfolglosen Zustellungen verschiebt SQS die
  Nachricht **in die DLQ** (dauerhaft nicht verarbeitbar: Validierungsfehler,
  Integritätskonflikte).

## Poison-Message-Policy (Outcome → Aktion)

| Outcome | Aktion |
|---|---|
| `APPLIED`, `TRANSPORT_DUPLICATE`, `BUSINESS_DUPLICATE` | **löschen** (nach Commit / idempotent) |
| `DB_FAILURE`, Queue-/Delete-Fehler, Failure-Injection, transiente Infra-Fehler | **nicht löschen → Retry** (Main-Queue) |
| `VALIDATION_ERROR`, `EVENT_ID_CONFLICT`, `BUSINESS_CONFLICT` | **nicht löschen → nach `maxReceiveCount` per Redrive in die DLQ** |

Keine ungültige oder widersprüchliche Nachricht wird still bestätigt.

## DLQ manuell prüfen und kontrolliert replayen
1. DLQ-Tiefe beobachten (`consumer_dlq_depth_approximate`, Alert `DLQNotEmpty`).
2. Nachrichten in der DLQ **lesen/inspizieren** (Receive ohne Delete), Ursache
   bestimmen — **keine** automatische Mutation.
3. **Erst nach Ursachenbehebung** kontrolliert zurückspielen (SQS „Start DLQ
   redrive" bzw. gezieltes Re-Enqueue in die Main-Queue).
4. **Replay behält die ursprüngliche `event_id`** — es wird **keine** Event-ID
   automatisch mutiert oder ersetzt. Dadurch greift die Consumer-Idempotenz: bereits
   angewandte Events enden als Duplikat, nicht als Zweiteffekt.
5. **Keine Exactly-once-Garantie** — at-least-once bleibt; Replay kann Duplikate
   erzeugen, die idempotent aufgefangen werden.

## Monitoring-Erreichbarkeit & Abhängigkeit vom Consumer
- **Scrape-Pfad:** Der Consumer läuft in k3d; sein `/metrics` ist nur dann vom
  Prometheus-Container (monitoring-Compose auf dem Host) erreichbar, wenn der
  k3d-Cluster den NodePort `30090` auf den **Host** veröffentlicht. Ein
  `type: NodePort`-Service **allein** genügt nicht. Die Portabbildung wird **beim
  Clusteraufbau** gesetzt (`ops/bootstrap/create-site-cloud-cluster.sh`,
  `--port '30090:30090@server:0'`); Prometheus scrapt `host.docker.internal:30090`.
  `ops/deploy/deploy-consumer.sh` prüft das **fail closed** und erzeugt das file-sd-Target
  atomar.
- **Queue-/DLQ-Tiefe hängt am lebenden Consumer:** `consumer_queue_depth_approximate`
  und `consumer_dlq_depth_approximate` werden **vom Consumer** via `GetQueueAttributes`
  exponiert. **Stirbt der Consumer, fehlen diese Metriken** — `MainQueueBacklog`/
  `DLQNotEmpty` können während des Ausfalls **nicht** ausgewertet werden. Das
  **primäre Ausfallsignal ist dann `ConsumerDown`** (`up{job="consumer"}==0`), das
  unabhängig vom Consumer-Prozess aus dem Scrape entsteht — Voraussetzung ist ein
  **immer vorhandenes** file-sd-Target (sonst gäbe es keine `up`-Serie; deshalb der
  fail-closed Target-Generator). Es gibt bewusst **keine** separate
  Queue-Exporter-Anwendung; eine unabhängige Queue-Überwachung wird **nicht** behauptet.
- **Oldest-Message-Age** wird nicht geführt (s. u.).

## Konsequenzen
- Poison Messages verlassen die Main-Queue nach beschränkter Wiederholung und blockieren
  den Durchsatz nicht dauerhaft; sie bleiben in der DLQ analysierbar.
- Monitoring deckt Main-/DLQ-Tiefe (solange der Consumer lebt), Consumer-Liveness/
  Readiness, Receive-/DB-Fehler, Validierungs-/Integritätskonflikte und
  Mehrfachzustellungen ab.
- **Oldest-Message-Age** wird **nicht** als Metrik geführt: weder
  `GetQueueAttributes` noch ElasticMQ liefern es (in echtem AWS nur über CloudWatch
  `ApproximateAgeOfOldestMessage`). Es wird daher bewusst nicht behauptet.
- Phase 3 bleibt **nicht deployed**, der Publisher **nicht implementiert**;
  `EVENTS_ENABLED` unverändert.

## Verworfene Alternativen
- Anwendungseigenes „Send-to-DLQ": dupliziert native SQS-Funktionalität, fehleranfällig.
- Client-seitige DLQ-Entscheidung anhand `ApproximateReceiveCount`: würde die Source
  of Truth verdoppeln; Receive Count dient hier ausschließlich der Observability.
- FIFO-Queue: nicht nötig (Idempotenz statt Ordering) und teurer/limitierter.
