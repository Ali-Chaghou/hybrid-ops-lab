# Idempotenz im Consumer (site-cloud)

Wie der Consumer Events **at-least-once** sicher verarbeitet, ohne fachliche
Doppeleffekte. Beschreibt den **aktuellen** Stand: der Consumer verarbeitet
idempotent über die `event_id`. Ein **Publisher** (Outbox → Queue) ist Teil von
Phase 3 und **noch nicht** live; bis dahin fließen keine Events automatisch aus
`event_outbox` in die Queue.

> All infrastructure names, roles, records and runtime evidence shown here belong
> to an isolated synthetic lab environment and do not represent an employer,
> customer or production system.

## At-least-once-Transport

SQS/ElasticMQ garantiert *mindestens einmalige* Zustellung, nicht *genau einmal*.
Eine Nachricht kann mehrfach ankommen — z. B. nach Ablauf des Visibility-Timeouts,
wenn das Löschen (Delete/Ack) nicht stattfand. Der Consumer muss deshalb jede
Nachricht so behandeln, dass eine **Wiederholung keinen zweiten fachlichen Effekt**
erzeugt. **Es gibt keine Exactly-once-Garantie** — sie wird auch nicht behauptet.

## `event_id` als stabile Idempotenz-ID

Jedes Event trägt eine beim Produzenten vergebene, unveränderliche `event_id`
(UUID). Sie ist die **Idempotenzschlüssel-Identität**: dieselbe `event_id` steht
für dasselbe fachliche Ereignis, egal wie oft es zugestellt wird. `event_id` ist
PRIMARY KEY der Inbox — die Datenbank, nicht die Anwendung, entscheidet Rennen.

## Kanonischer Fingerprint

Zusätzlich wird ein **SHA-256-Fingerprint** über die unveränderlichen Eventdaten
(`event_type`, `schema_version`, `occurred_at` in UTC, `source`, `payload`)
gebildet — unabhängig von Schlüsselreihenfolge, Whitespace und äquivalenter
UTC-Schreibweise, Unicode auf NFC normalisiert. Er bezieht die `event_id`
**nicht** ein. Damit lässt sich bei erneut auftauchender `event_id` unterscheiden,
ob es sich um *dieselbe* Nachricht (gleicher Fingerprint → Transport-Duplikat)
oder um einen *Widerspruch* (andere Daten unter gleicher ID → Konflikt) handelt.
Der Fingerprint ist eine Konsistenzprüfung, **keine** Signatur/Herkunftsgarantie.

## Transactional Inbox + Projection in einer Transaktion

Pro Event werden in **derselben** PostgreSQL-Transaktion geschrieben:

- `movement_projection` — der fachliche Effekt (`(source, source_movement_id)` als
  PK; `source_event_id` UNIQUE + FK),
- `event_inbox` — der Idempotenznachweis (`event_id` PK, `fingerprint`,
  `disposition`).

Beide committen gemeinsam oder gar nicht; jeder Nicht-Erfolgspfad rollt die
**gesamte** Transaktion zurück (kein Teilzustand). Die Runtime-Rolle `consumer_app`
besitzt nur `SELECT, INSERT` — **kein** `UPDATE`/`DELETE`, keine DDL.

## Transport-Duplikat vs. Business-Duplikat vs. Konflikt

| Fall | Bedingung | Ergebnis | Queue-Delete? |
|---|---|---|---|
| **applied** | erstmaliges Event, neuer Effekt | `movement_projection` + `event_inbox` neu | ja |
| **transport duplicate** | gleiche `event_id`, gleicher Fingerprint | kein zweiter Effekt | ja |
| **business duplicate** | neue `event_id`, bereits angewandtes Movement, identische Fachdaten | kein zweiter Effekt, kanonische Referenz | ja |
| **event-id conflict** | gleiche `event_id`, **anderer** Fingerprint | abgelehnt (fail closed) | **nein** |
| **business conflict** | gleiches Movement, **geänderte** Fachdaten | abgelehnt (fail closed) | **nein** |
| **validation error** | Envelope/Contract ungültig | abgelehnt | **nein** |
| **db failure** | DB-Fehler/-Ausfall während der Verarbeitung | kein Effekt | **nein** |

Konflikte, Validierungs- und DB-Fehler werden **nicht** bestätigt — die Nachricht
bleibt sichtbar bzw. wird nach Visibility-Timeout erneut zugestellt und kann
untersucht werden.

### Warum ein Business-Duplikat sicher bestätigt werden darf

Ein **Business-Duplikat** trägt eine *neue* `event_id`, bezieht sich aber auf ein
bereits angewandtes Movement (`(source, source_movement_id)`). Ob die Fachdaten
identisch sind, wird durch **direkten Vergleich der bereits projizierten Felder**
(`sku`, `quantity`, `warehouse`, `occurred_at`) der bestehenden Projektion mit dem
neuen Event entschieden — **nicht** über einen separaten Fingerprint der Fachfelder
(der Fingerprint dient nur der Transport-Duplikat-/Event-ID-Konflikt-Klassifikation
bei *gleicher* `event_id`). Sind die projizierten Felder identisch, existiert der
fachliche Effekt bereits; das zweite Event erzeugt **keinen** neuen Effekt. Die abweichende `event_id`
wird dabei **bewusst verworfen** (nur als Inbox-Eintrag mit Verweis auf das kanonische
Event protokolliert) — der fachliche Schlüssel ist das Movement, nicht die `event_id`.
Genau deshalb ist das Löschen hier sicher: erneute Zustellung derselben Nachricht
würde wieder als Duplikat enden. Ändert sich dagegen unter neuer `event_id` die
**Fachdaten** desselben Movements, ist das ein **Business-Konflikt** → fail closed,
**kein** Delete.

## Ack/Delete erst nach erfolgreichem Commit

Die Queue-Nachricht wird **ausschließlich** dann gelöscht, wenn die Verarbeitung
einen *löschbaren* Outcome erreicht hat: `applied`, `transport duplicate` oder
`business duplicate` — also nach erfolgreichem DB-Commit bzw. nach idempotent
nachgewiesenem Duplikat. Die Reihenfolge ist strikt: **erst Commit, dann Delete.**

## Fehlerfenster „DB-Commit erfolgreich, Queue-Delete fehlgeschlagen"

Zwischen Commit und Delete liegt ein unvermeidbares Fenster: Der fachliche Effekt
ist bereits dauerhaft committed, aber das Delete kann scheitern (Netzwerk,
Prozess-Neustart, Ablauf des Visibility-Timeouts). Dann wird die Nachricht **erneut
zugestellt**. Das ist die direkte Folge von at-least-once und wird bewusst in Kauf
genommen — der Consumer reißt dafür weder den Prozess ab noch erfindet er eine
„genau einmal"-Zusage.

## Warum Wiederholung sicher ist

Bei der Redelivery findet der Consumer die `event_id` bereits in der Inbox: gleicher
Fingerprint ⇒ **Transport-Duplikat** ⇒ **kein** zweiter Effekt, und die Nachricht
wird jetzt sauber gelöscht. Eine Lab-Failure-Injection (Commit ja, Delete nein) und
ein erzwungener Delete-Fehler werden integrationsgetestet und enden beide nach der
Wiederholung in genau einem Effekt. So bleibt das System trotz mehrfacher Zustellung
**effektiv idempotent** — aber, nochmals: **ohne Exactly-once-Garantie**.

## Poison Messages / Deployment-Reife

Validierungsfehler und Konflikte werden **fail closed** behandelt: nie automatisch
bestätigt, **keine** Nachricht geht verloren. Für jeden dieser Fälle existieren
niedrig-kardinale Metriken (`consumer_validation_failures_total{reason}`,
`consumer_integrity_conflicts_total{kind}`), sodass Dauer-Redeliveries sichtbar werden.

**DLQ/Redrive ist im Repository umgesetzt (Gate D2).** Die Main-Queue besitzt eine
**native Redrive-Policy** mit `maxReceiveCount = 5`: eine dauerhaft nicht
verarbeitbare „poison" Nachricht wird nach **fünf** Zustellungen automatisch von
SQS/ElasticMQ in die DLQ (`inventory-movements-dlq`) verschoben — die Anwendung
verschiebt nichts manuell. Damit endet die zuvor mögliche Endlos-Redelivery.
Details: [ADR-007](decisions/007-dlq-and-redrive.md).

**Reifegrenze:** Diese Schutzmechanismen (Consumer-Idempotenz = D1, DLQ/Redrive = D2) sind
mit Gate D3B2.1 **live auf site-cloud im synthetischen Lab verifiziert**
([Abschlussnachweis D3B2.1](handoff-d3b2.1-complete.md)). Der **Outbox-Publisher ist
implementiert/gemerged, aber nicht aktiviert** (Default `PUBLISHER_ENABLED=false`); der
vollständige Phase-3-Eventfluss ist **nicht aktiviert**, und es gibt weiterhin
**keine Exactly-once-Garantie** (at-least-once + Consumer-Idempotenz).

## Deployment-Voraussetzung (nicht stillschweigend)

Der k3d-Consumer verbindet als `consumer_app` gegen eine **bereits vorbereitete**
Consumer-DB. **Vor** `ops/deploy/deploy-consumer.sh` müssen daher Datenbank, Rollen
(`consumer_admin`/`consumer_app`) und die Migration `0001_init` existieren — über den
site-cloud-Compose-Stack (`consumer-db` + `consumer-db-bootstrap` +
`consumer-db-prepare` + `consumer-migrate`). Das Deploy-Skript richtet **keine** DB
ein und weist zu Beginn ausdrücklich auf diese Voraussetzung hin; fehlt sie, schlägt
der `verify_schema()`-Startup-Gate des Consumers **fail closed** fehl.

## Abgrenzung

- Der **API-Request-Pfad** (site-dc) publiziert weiterhin **nie** direkt an die
  Queue; er schreibt Movement + Outbox-Event atomar (siehe
  [ADR-006](decisions/006-transactional-outbox.md)).
- Der **Publisher** (`event_outbox` → Queue) ist **noch nicht** implementiert/aktiv;
  `EVENTS_ENABLED=false` bleibt. Dieses Dokument beschreibt nur die
  **Consumer-seitige** Idempotenz.
