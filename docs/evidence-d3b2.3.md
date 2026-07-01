# D3B2.3 – Runtime-Nachweise: Publisher, E2E, Disable und Redelivery

[Übersicht](../README.md) · [Dokumentation](README.md) · [Status & Roadmap](roadmap.md) · [Nachweise](evidence-index.md) · [Security](../SECURITY.md)

**Status: in progress — nicht abgeschlossen.**
Runtime-Läufe: 2026-06-30 und 2026-07-02 (Europe/Berlin).
Umgebung: synthetisches Lab, keine Produktionsumgebung. Das System bleibt at-least-once;
Exactly-once wird nicht behauptet.

Dieses Dokument hält die bisher live belegten Teile von D3B2.3 fest. Es dokumentiert die
entscheidenden Werte und IDs strukturiert; es enthält keine vollständigen Terminal-Logs.
Die genannten Metrikwerte sind zeitgebundene Evidence-Snapshots und kein dauerhafter
Projektstatus.

## 1. Scope des bereits bewiesenen Teils

Live im Lab verifiziert:

- kontrollierter D3B2.3-Preflight;
- kontrollierte Publisher-Aktivierung;
- echter Ende-zu-Ende-Pfad: Inventory API → Transactional Outbox → Publisher → Main Queue
  → Consumer Inbox → Movement Projection;
- kontrollierter Disable-Pfad und ein während Disable pending gebliebenes Event ohne Claim;
- kontrollierte Reaktivierung mit sicherem Nachlauf des wartenden Events;
- One-shot Failure Injection nach DB-Commit und vor Queue-Delete;
- Redelivery nach Visibility Timeout;
- Transport-Duplikat mit derselben `event_id` und derselben Queue-Nachricht;
- genau eine fachliche Projection-Wirkung trotz zweifacher Zustellung;
- kontrollierter Rollback der Failure Injection auf `0`.

Noch nicht als vollständiger Runtime-Nachweis erbracht (siehe [Abschnitt 9](#9-offene-tests)):

- Validation-/Poison-Fall;
- vollständiger DLQ-Endnachweis;
- nativer Redrive-Nachweis oder dokumentierte ElasticMQ-Grenze;
- abschließende D3B2.3-Gesamtverifikation.

## 2. Release-Identitäten

| Bestandteil | Referenz |
|---|---|
| Publisher-Aktivierungspfad | `b9f2f72083bfe598a5d704562f07cf8e96169125` |
| Kontrollierter Failure-Injection-Deploy-Pfad | `a177f47b201baeadd8a0d30b3f0513b675a7f28c` |
| Consumer-Runtime (D3B2.1) | Release `5d319cad54e5a26dd59baacc1c269780ee90b1e4`, Image `inventory-consumer:5d319cad54e5` |
| Dokumentationsstand vor diesem Lauf | `4eda055951fa2ed08ba8a7225fe169bf932b4009` |

Der Failure-Injection-Deploy-Pfad verwendet das vorhandene immutable Consumer-Image
wieder. Beim Runtime-Lauf erfolgte kein Neubau und kein Image-Import unter dem bestehenden
Release-Tag. Die Injection war nur mit explizitem Reuse-Modus aktivierbar, Image-Mismatch
führte fail-closed zum Abbruch, und der sichere Zielzustand wurde anschließend wieder
explizit auf `0` gesetzt.

## 3. Preflight und Aktivierung

Vor den Runtime-Mutationen wurden Git, CI und beide Sites read-only geprüft:

- lokaler Worktree sauber;
- `HEAD == origin/main == 4eda055951fa2ed08ba8a7225fe169bf932b4009`;
- GitHub Actions `CI` und `build-and-test` für exakt diesen Commit erfolgreich;
- Consumer Deployment ready;
- Consumer Image exakt `inventory-consumer:5d319cad54e5`;
- Injection vor dem Lauf effektiv deaktiviert;
- Main Queue und DLQ leer;
- Redrive-Policy mit `maxReceiveCount = 5` vorhanden;
- Publisher, Inventory, Datenbanken und Consumer healthy;
- Publisher aktiviert (`publisher_enabled = 1`);
- keine Pending-Outbox-Events und keine aktiven Claims;
- Prometheus und Alertmanager ready;
- Consumer- und Publisher-Targets up;
- keine relevanten firing Alerts.

## 4. Echter E2E-Nachweis (erster Lauf)

Movement:

| Feld | Wert |
|---|---|
| movement_id | 3 |
| SKU | `D3B23-E2E-001` |
| quantity | 1 |
| warehouse | `phase3-e2e` |
| event_id | `34b530a8-b25e-4544-a14e-2dcd1fb479fb` |
| HTTP-Status `POST /movements` | 201 |

site-dc:

- Outbox-Status: `published`;
- kein offener Claim.

site-cloud:

- Consumer-Disposition: `applied`;
- `source_movement_id`: 3;
- Projection-Werte stimmten mit SKU, quantity und warehouse überein;
- Main Queue und DLQ waren nach der Verarbeitung leer.

Zeitgebundener Metrik-Snapshot nach insgesamt drei Events:

- Publisher: `claimed_total` 3, `publish_success_total` 3, `publish_errors_total` 0,
  `retries_total` 0, `finalize_conflicts_total` 0.
- Consumer: `events_received_total` 3, `events_applied_total` 3,
  `transport_duplicates_total` 0, `business_duplicates_total` 0,
  `database_failures_total` 0, `message_delete_failures_total` 0, `redeliveries_total` 0.

## 5. Disable-/Re-enable-Nachweis (zweiter Lauf)

Movement:

| Feld | Wert |
|---|---|
| movement_id | 4 |
| SKU | `D3B23-DISABLE-001` |
| quantity | 1 |
| warehouse | `disabled-test` |
| event_id | `b6d52953-125e-4723-b647-efa52a09b852` |
| HTTP-Status `POST /movements` | 201 |

Während Disable:

- Activation-State: `disabled`, `publisher_enabled: false`;
- Outbox-Status des Events: `pending`, kein Claim;
- Main Queue leer, DLQ leer;
- Consumer-Zähler blieben bei `events_received_total` 3 und
  `events_applied_total` 3 unverändert.

Nach Re-enable:

- erwartete Pending-Anzahl: exakt 1;
- Outbox-Status wechselte zu `published`, kein offener Claim;
- Publisher seit Recreate: `claimed_total` 1, `publish_success_total` 1,
  `publish_errors_total` 0, `retries_total` 0, `finalize_conflicts_total` 0;
- Consumer-Disposition: `applied`, `source_movement_id`: 4;
- Projection enthielt `D3B23-DISABLE-001 / 1 / disabled-test`;
- Main Queue und DLQ waren danach wieder leer.

## 6. Failure-Injection-, Redelivery- und Transport-Duplicate-Nachweis

Der Lauf wurde am 2026-07-02 (Europe/Berlin) mit genau einem neuen API-Movement
durchgeführt.

Movement und Transport:

| Feld | Wert |
|---|---|
| movement_id | 5 |
| SKU | `D3B23-REDELIVERY-001` |
| quantity | 1 |
| warehouse | `redelivery-test` |
| event_id | `584484dd-936c-4ef8-814e-9e5b0161b6cf` |
| Queue message_id | `895690cf-9657-4e23-8366-6e5a150b8db5` |
| HTTP-Status `POST /movements` | 201 |
| Visibility Timeout | 30 Sekunden |

Kontrollierter Ablauf:

1. Consumer wurde mit demselben Image `inventory-consumer:5d319cad54e5` und
   `LAB_FAIL_AFTER_COMMIT_ONCE=1` neu ausgerollt.
2. Der Deploy-Pfad verwendete das vorhandene Image; kein Build und kein Import wurden
   ausgeführt.
3. Genau ein ready Consumer-Pod war aktiv.
4. Die erste Zustellung wurde fachlich committed und als `applied` protokolliert.
5. Danach feuerte die One-shot Injection exakt einmal vor dem Queue-Delete.
6. Die Nachricht blieb bis zum Visibility Timeout im Transport bestehen.
7. Dieselbe Queue-Nachricht wurde mit `receive_count=2` erneut zugestellt.
8. Die Inbox erkannte dieselbe `event_id` als Transport-Duplikat.
9. Es entstand keine zweite Projection-Wirkung.
10. Nach erfolgreicher Duplicate-Behandlung wurde die Queue-Nachricht gelöscht.

Zeitgebundener Metrik-Snapshot des Test-Pods:

| Metrik | Wert |
|---|---:|
| `consumer_events_received_total` | 2 |
| `consumer_events_applied_total` | 1 |
| `consumer_failure_injections_total` | 1 |
| `consumer_transport_duplicates_total` | 1 |
| `consumer_redeliveries_total` | 1 |
| `consumer_business_duplicates_total` | 0 |
| `consumer_database_failures_total` | 0 |
| `consumer_message_delete_failures_total` | 0 |

Die Werte blieben über mehrere Beobachtungszeitpunkte stabil. Main Queue und DLQ waren
danach jeweils `visible=0`, `inflight=0` und `delayed=0`.

### 6.1 site-dc Datenintegrität

- Movement `5` existiert exakt einmal.
- Outbox-Event `584484dd-936c-4ef8-814e-9e5b0161b6cf` existiert exakt einmal.
- `movement_id = 5`.
- `event_type = inventory.movement.recorded`.
- `schema_version = 1`.
- `source = inventory-service`.
- Payload: `D3B23-REDELIVERY-001 / 1 / redelivery-test / movement_id 5`.
- Outbox-Status: `published`.
- `attempt_count = 1`.
- `last_error = null`.
- `claim_owner = null`.
- `claimed_at = null`.

### 6.2 site-cloud Datenintegrität

Consumer-Inbox:

- exakt eine Zeile für die `event_id`;
- `source_movement_id = 5`;
- `disposition = applied`;
- `canonical_event_id = null`.

Movement Projection:

- exakt eine Zeile für `source_movement_id = 5`;
- `source_event_id` entspricht exakt der Event-ID;
- SKU `D3B23-REDELIVERY-001`;
- quantity `1`;
- warehouse `redelivery-test`.

Damit ist live bewiesen, dass die zweifache Transportzustellung nur eine fachliche Wirkung
erzeugte. Das ist Idempotenz unter at-least-once, keine Exactly-once-Aussage.

### 6.3 Sicherer Endzustand

Nach dem Lauf wurde die Injection unabhängig vom Testergebnis über den kontrollierten
Reuse-Pfad wieder auf `0` gesetzt.

Final verifiziert:

- Consumer Image weiterhin exakt `inventory-consumer:5d319cad54e5`;
- `LAB_FAIL_AFTER_COMMIT_ONCE = 0`;
- genau ein aktiver ready Consumer-Pod;
- Consumer `healthz = 200`, `readyz = 200`;
- Main Queue leer;
- DLQ leer;
- Publisher, Inventory und beide Datenbanken healthy;
- Publisher weiterhin aktiviert;
- Outbox `pending = 0`;
- aktive Claims `0`;
- keine unerwarteten Outbox-Statuswerte;
- Prometheus und Alertmanager ready;
- Consumer- und Publisher-Targets up;
- keine relevanten firing Alerts.

## 7. Zeitgebundene Metrik-Snapshots

Die in den Abschnitten 4, 5 und 6 genannten Zähler sind Momentaufnahmen aus den jeweiligen
Runtime-Läufen. Sie belegen den beobachteten Ablauf, sind aber kein dauerhafter
Projektstatus und dürfen nicht als fortlaufende README-Werte übernommen werden.
Insbesondere wurden Consumer-Zähler durch kontrollierte Pod-Rollouts zurückgesetzt.

## 8. Sicherheit und Datenintegrität

- Keine beobachteten Publish-Fehler, Datenbank- oder Delete-Fehler.
- Keine Finalize-Konflikte.
- Keine offenen oder zurückgebliebenen Claims nach den Läufen.
- Kein Queue-Purge.
- Keine direkte Testnachricht in die Queue.
- Keine manuelle DB- oder Outbox-Mutation.
- Das Failure-Szenario wurde ausschließlich über ein echtes API-Movement ausgelöst.
- Kein Consumer-Image wurde unter dem alten Release-Tag neu gebaut.
- Projektionen entsprachen exakt den auslösenden Movements.
- Die Failure Injection war nach dem Test wieder explizit deaktiviert.
- Das System bleibt at-least-once; es wird keine Exactly-once-Semantik behauptet.

## 9. Offene Tests

Für den Abschluss von D3B2.3 noch als Runtime-Nachweis zu erbringen:

- kontrollierter Validation-/Poison-Fall;
- vollständiger Weg einer dauerhaft nicht verarbeitbaren Nachricht in die DLQ;
- Redrive aus der DLQ oder belastbare Dokumentation einer echten ElasticMQ-Grenze;
- abschließende D3B2.3-Gesamtverifikation.

## 10. Abschlussbedingung für D3B2.3

D3B2.3 gilt erst dann als abgeschlossen, wenn zusätzlich zu den bereits belegten
Aktivierungs-, E2E-, Disable-/Re-enable-, Failure-Injection-, Redelivery- und
Transport-Duplicate-Nachweisen die offenen Tests aus Abschnitt 9 vorliegen und die
abschließende Gesamtverifikation erfasst ist. Erst danach darf Phase 3 formal als
abgeschlossen markiert werden.

Dieses Dokument beschreibt einen synthetischen Lab-Nachweis. Es ist keine
Production-Ready- und keine Exactly-once-Aussage.
