# 006 — Transactional Outbox statt Publish im Request-Pfad

## Status
Akzeptiert (2026-06-18)

## Kontext
`POST /movements` persistierte bisher die Lagerbewegung und rief danach
`publish_movement()` auf (in Phase 2 ein No-op hinter `EVENTS_ENABLED`).
Sobald in Phase 3 wirklich an eine Queue publiziert wird, entsteht das
klassische Dual-Write-Problem: DB-Commit und Queue-Send sind zwei getrennte
Systeme. Faellt der Send nach dem Commit aus (oder umgekehrt), driften
Source-of-Truth (Postgres) und Event-Strom auseinander — ein Movement ohne
Event oder ein Event ohne Movement.

## Entscheidung
site-dc nutzt das **Transactional-Outbox-Muster**. `POST /movements` schreibt
in EINER PostgreSQL-Transaktion sowohl die Bewegung (`stock_movements`) als auch
ihr Event (`event_outbox`). Beide committen gemeinsam oder gar nicht.

Die 1-zu-1-Invariante erzwingt die **Datenbank**, nicht die Anwendung:

- `event_outbox(movement_id, event_id)` → FK auf `stock_movements(id, event_id)`
  (unmittelbar): ein Event ohne passendes Movement bzw. mit falscher Kombination
  scheitert sofort.
- `stock_movements(event_id)` → FK auf `event_outbox(event_id)`
  `DEFERRABLE INITIALLY DEFERRED`: ein Movement ohne Event scheitert spaetestens
  beim Commit. Deferred, weil der Runtime-Pfad das Movement vor seinem Event
  einfuegt.

`event_outbox` ist damit die dauerhafte Uebergabegrenze. Ein separater Publisher
liest die `pending`-Zeilen spaeter und markiert sie als `published` (kein
Loeschen im Lab — Archivierung ueber `status`). Im Request-Pfad gibt es **keine**
Netzwerk-/Queue-Operation mehr.

`inventory_app` erhaelt Least Privilege: `SELECT, INSERT` auf `stock_movements`
und auf `event_outbox` **nur spaltenbezogenes INSERT** auf die Producer-Spalten
(`event_id, movement_id, event_type, schema_version, occurred_at, source,
payload`). Die operativen Status-, Retry- und Publish-Spalten (`status`,
`attempt_count`, `available_at`, `created_at`, `published_at`, `last_error`)
darf `inventory_app` nicht explizit setzen — sie kommen beim Insert aus ihren
Defaults und bleiben der spaeteren Publisher-Rolle vorbehalten. Lesen/Archivieren
(SELECT/UPDATE) bekommt in Phase 3 eine getrennte Rolle.

Fuer die spaetere Publisher-Abfrage traegt die pending-Outbox einen **partiellen
Index** `event_outbox_pending_available_idx` auf `(available_at, created_at,
event_id) WHERE status = 'pending'`. Er haelt nur faellige, noch nicht
publizierte Zeilen vor und verhindert Full-Table-Scans, wenn das Archiv
(published-Zeilen) waechst.

Verworfene Alternativen:
- Publish nach Commit (Status quo): Dual-Write, keine Atomaritaetsgarantie.
- Listen/Notify oder CDC/Debezium: zusaetzliche Infrastruktur, fuer das Lab
  ueberdimensioniert; Outbox + Polling reicht und ist nachvollziehbar.

## Konsequenzen
- Erfolgsmetriken (`movements_created`, `outbox_events_written`) zaehlen erst
  nach dem Commit; eine niedrig-kardinale Fehler-Metrik (`movement_tx_failures`)
  zaehlt zurueckgerollte Transaktionen.
- `events.py`/`EVENTS_ENABLED`/`SQS_*` bleiben fuer Phase 3 bestehen, steuern
  Phase 2B aber nicht und werden vom Request-Pfad nicht mehr aufgerufen.
- Queue (ElasticMQ/SQS), Toxiproxy und Consumer-Verkabelung folgen in Phase 3.
