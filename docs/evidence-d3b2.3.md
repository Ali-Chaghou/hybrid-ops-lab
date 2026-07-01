# D3B2.3 – Zwischenstand Publisher-Aktivierung und E2E-Nachweis

[Übersicht](../README.md) · [Dokumentation](README.md) · [Status & Roadmap](roadmap.md) · [Nachweise](evidence-index.md) · [Security](../SECURITY.md)

**Status: in progress — nicht abgeschlossen.**
Datum des Runtime-Laufs: 2026-06-30.
Umgebung: synthetisches Lab, keine Produktionsumgebung. Das System bleibt at-least-once;
Exactly-once wird nicht behauptet.

Dieses Dokument hält den bereits belegten Teil von D3B2.3 fest. Es dokumentiert die
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
- kontrollierte Reaktivierung mit sicherem Nachlauf des wartenden Events.

Noch nicht als Runtime-Nachweis erbracht (siehe [Abschnitt 8](#8-offene-tests)).

## 2. Release-Identitäten

| Bestandteil | Referenz |
|---|---|
| Publisher-Aktivierungspfad | `b9f2f72083bfe598a5d704562f07cf8e96169125` |
| Kontrollierter Failure-Injection-Deploy-Pfad | `a177f47b201baeadd8a0d30b3f0513b675a7f28c` |
| Consumer-Runtime (D3B2.1) | Release `5d319cad54e5a26dd59baacc1c269780ee90b1e4`, Image `inventory-consumer:5d319cad54e5` |

Der Failure-Injection-Deploy-Pfad ist implementiert und lokal getestet: er verwendet das
vorhandene immutable Consumer-Image wieder (kein Neubau unter dem alten Release-Tag), die
Lab-Injection ist ausschließlich explizit aktivierbar, der Default bleibt sicher
deaktiviert, bei Image-Mismatch wird fail-closed abgebrochen, und Contract-Tests sind
vorhanden. Der eigentliche Failure-Injection-Runtime-Lauf ist noch offen.

## 3. Preflight und Aktivierung

- Der kontrollierte Aktivierungs-Preflight lief vor der Aktivierung durch.
- Der Publisher wurde über den kontrollierten Aktivierungspfad bewusst aktiviert
  (`publisher_enabled: true`).
- Aktivierung und Deaktivierung erfolgten kontrolliert; der sichere Default bleibt
  deaktiviert.

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

Zeitgebundener Metrik-Snapshot nach insgesamt drei Events (kein dauerhafter Status):

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
- Consumer-Zähler blieben bei `events_received_total` 3 und `events_applied_total` 3 unverändert.

Nach Re-enable:

- erwartete Pending-Anzahl: exakt 1;
- Outbox-Status wechselte zu `published`, kein offener Claim;
- Publisher seit Recreate (zeitgebundener Snapshot): `claimed_total` 1,
  `publish_success_total` 1, `publish_errors_total` 0, `retries_total` 0,
  `finalize_conflicts_total` 0;
- Consumer-Disposition: `applied`, `source_movement_id`: 4;
- Projection enthielt `D3B23-DISABLE-001 / 1 / disabled-test`;
- Main Queue und DLQ waren danach wieder leer.

## 6. Zeitgebundene Metrik-Snapshots

Die in den Abschnitten 4 und 5 genannten Zähler sind Momentaufnahmen aus dem Runtime-Lauf
vom 2026-06-30. Sie belegen den beobachteten Ablauf, sind aber kein dauerhafter
Projektstatus und dürfen nicht als fortlaufende README-Werte übernommen werden.

## 7. Sicherheit und Datenintegrität

- Keine beobachteten Publish-Fehler, Datenbank- oder Delete-Fehler.
- Keine Finalize-Konflikte.
- Keine offenen oder zurückgebliebenen Claims nach den Läufen.
- Kein Queue-Purge und keine manuelle Datenlöschung.
- Projektionen entsprachen exakt den auslösenden Movements (SKU, quantity, warehouse).
- Das System bleibt at-least-once; es wird keine Exactly-once-Semantik behauptet.

## 8. Offene Tests

Für den Abschluss von D3B2.3 noch als Runtime-Nachweis zu erbringen:

- One-shot Failure Injection nach DB-Commit und vor Queue-Delete;
- Redelivery nach Visibility Timeout;
- Transport-Duplikat in der realen Laufzeit;
- Validation-/Poison-Event;
- vollständiger nativer DLQ-Redrive-Nachweis;
- abschließende D3B2.3-Gesamtverifikation.

## 9. Abschlussbedingung für D3B2.3

D3B2.3 gilt erst dann als abgeschlossen, wenn zusätzlich zum hier belegten Aktivierungs-,
E2E- und Disable-/Re-enable-Nachweis die offenen Tests aus Abschnitt 8 als Runtime-Nachweis
vorliegen (Failure Injection, Redelivery, Transport-Duplikat, Validation-/Poison-Fall,
nativer DLQ-Redrive) und die abschließende Gesamtverifikation erfasst ist. Erst danach darf
Phase 3 als formal abgeschlossen markiert werden. Dieses Dokument beschreibt einen
Zwischenstand; es ist keine Production-Ready- und keine Exactly-once-Aussage.
