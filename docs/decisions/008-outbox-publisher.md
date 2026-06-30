# 008 — Separater Outbox-Publisher (Lease-Claiming, Fencing, disabled by default)

## Status
Akzeptiert (2026-06-22) — Gate D3A (Kern; Deployment/Monitoring folgen in D3B)

### Status-Nachtrag vom 28. Juni 2026

- D3A (Publisher-Kern, Migration `0004`) implementiert.
- D3B1 (Publisher-Service-Wiring) implementiert.
- D3B2.1 (Consumer-Rollout, D1/D2 auf site-cloud) im Lab verifiziert
  (siehe [D3B2.1-Abschlussnachweis](../handoff-d3b2.1-complete.md)).
- Der Publisher ist weiterhin nicht aktiviert; die Aktivierung gehört zu D3B2.3.
- D3B2.2 (site-dc-Migration `0004`) und D3B2.3 (Publisher-Aktivierung und
  End-to-End-Nachweis) sind geplant (siehe [Roadmap](../roadmap.md)).

### Status-Nachtrag vom 30. Juni 2026

- D3B2.2 wurde im Lab abgeschlossen und live verifiziert.
- Migration `0004`, neue Inventory-Runtime und deaktivierter Publisher sind aktiv.
- Die Rolle `inventory_publisher` erfüllt Least Privilege.
- Keine Outbox-Zeile wurde geclaimt oder publiziert.
- Main Queue und DLQ blieben leer.
- Das Publisher-Prometheus-Target ist aktiv und `up`.
- D3B2.3 bleibt ausstehend; der vollständige Eventfluss ist nicht aktiviert.
- Nachweis: [D3B2.2-Abschlussnachweis](../evidence-d3b2.2.md).

## Kontext
`event_outbox` (site-dc) ist die dauerhafte Übergabegrenze: `POST /movements`
schreibt Movement + Outbox-Event atomar, **ohne** im HTTP-Request-Pfad zu
publizieren ([ADR-006](006-transactional-outbox.md)). Für Phase 3 wird ein
Komponent benötigt, der fällige Outbox-Zeilen an die Queue (SQS/ElasticMQ) sendet,
ohne den Request-Pfad zu berühren und ohne die Source of Truth (PostgreSQL) zu
gefährden. Der Consumer ist über die `event_id` idempotent
([Idempotenz](../idempotency.md)); DLQ/Redrive existieren ([ADR-007](007-dlq-and-redrive.md)).

## Entscheidung

### Separater Publisher
Eigener Prozess/Image (`apps/publisher/`), **nicht** Teil der Inventory-API und
**kein** Import aus Inventory/Consumer. Genau ein Poller-Thread. **Standardmäßig
deaktiviert** über einen **eigenen** Flag `PUBLISHER_ENABLED=false` (unabhängig von
`EVENTS_ENABLED`). Deaktiviert: kein Claim, kein SQS-Client, keine DB-Mutation — nur
Health/Metrics.

### Lease statt langem DB-Lock
Verworfen: ein DB-`FOR UPDATE`-Lock über den gesamten Netzwerk-Publish zu halten
(lange Transaktionen, Lock-Stau, schlechte Crash-Recovery). Gewählt: **kurzer Claim
mit Lease**. Eine kurze Transaktion claimt fällige Zeilen
(`status='pending' AND available_at<=now()`, stabil sortiert nach
`available_at, created_at, event_id`, `FOR UPDATE SKIP LOCKED`, begrenzt auf
Batch-Größe), setzt `attempt_count+1`, schiebt `available_at` per Lease in die
Zukunft, markiert `claimed_at`/`claim_owner` und **committet sofort**. Erst danach
wird **außerhalb** der Transaktion publiziert. Alle Zeitwerte stammen aus der
**PostgreSQL-DB-Zeit** (`now()`), nicht aus lokaler Systemzeit. Abgelaufene Leases
werden automatisch wieder claimbar (Crash-Recovery ohne Sonderpfad).

### Claim-Fencing
Erfolg/Fehler werden nur auf die Zeile angewandt, die **noch `pending`** ist UND
denselben `claim_owner`/`claimed_at` trägt. Ein veralteter Worker (Lease abgelaufen,
Zeile neu geclaimt) trifft 0 Zeilen → Finalize-Conflict-Metrik, **kein** Überschreiben.
`claim_owner` ist ein opaker, prozess-zufälliger Token (kein Host/IP/Benutzer; kein
Prometheus-Label).

### Status & Migration
Kein neuer Status (bleibt `pending|published`). Migration `0004` ergänzt nur die
nullable Beobachtungs-/Fencing-Felder `claimed_at`/`claim_owner` (+ Konsistenz-CHECKs:
beide gemeinsam gesetzt/NULL, ≤64 Zeichen, published-Zeilen ohne Claim) und die
Least-Privilege-Grants. Rein additiv: Gate-A-/Bestands-Pending-Zeilen bleiben gültig.
`event_id` und Payload werden **nie** mutiert. Kein zusätzlicher Index (kleines Lab).

### Erfolg/Fehler
- **Erfolg** (nur nach bestätigtem Queue-Publish): `status='published'`,
  `published_at=now()`, Claim-Felder NULL. **Keine** Zeile wird gelöscht.
- **Fehler**: Status bleibt `pending`, `available_at=now()+backoff`,
  `last_error`=begrenzter Code, Claim-Felder NULL.
- Backoff: `min(base * 2^(attempt-1), max)`, overflow-sicher begrenzt.
- `last_error` ist **nur** ein begrenzter Fehlercode/Exception-Typ (feste Maximallänge),
  **nie** eine Exception-Nachricht (könnte Payload/DSN enthalten).
- **Kein** dauerhafter `failed`-Status in D3A; dauerhaft fehlerhafte Zeilen bleiben
  sichtbar/alarmierbar (Alerting in D3B).

### Einzelnes `SendMessage`
Gate D3A nutzt **ausschließlich** einzelnes `SendMessage` (kein `SendMessageBatch`):
keine partiellen Batch-Erfolge, einfachere Retry-Semantik, klar prüfbares
Duplicate-Window, ausreichender Lab-Durchsatz. SQS-Client mit kurzen expliziten
Timeouts und ohne unendliche SDK-Retries.

### Duplicate-Window & Consumer-Idempotenz
Geht eine Queue-Antwort verloren (Publish ok, Client sieht Fehler) oder stirbt der
Prozess nach Publish vor dem Status-Update, bleibt die Zeile `pending` und wird
später **erneut** publiziert — mit **identischer** `event_id` und identischem
Envelope. Das resultierende Duplikat fängt die **Consumer-Idempotenz** ab
(`TRANSPORT_DUPLICATE`). **Keine Exactly-once-Garantie.**

### Least Privilege
Eigene Rolle `inventory_publisher` (LOGIN, NOSUPERUSER, NOCREATEDB, NOCREATEROLE,
NOBYPASSRLS, NOREPLICATION). Migration `0004` erteilt ausschließlich: `USAGE` Schema,
`SELECT` auf `event_outbox` + `schema_migrations`, spaltenweises `UPDATE` nur auf
`status, attempt_count, available_at, published_at, last_error, claimed_at,
claim_owner`. **Kein** Zugriff auf `stock_movements`, **kein** INSERT/DELETE auf
`event_outbox`, keine DDL, keine Ownership, kein Consumer-DB-Zugriff. Trennung:
`inventory_admin` (Migration/Ownership), `inventory_app` (nur Producer-INSERT),
`inventory_publisher` (nur Claim/Status-UPDATE). Reihenfolge: Bootstrap erstellt die
Rolle **vor** der Migration, die Migration erteilt **danach** die Tabellenrechte.

## Gate-D3A-Abgrenzung
Enthalten: Migration `0004`, Rolle, Publisher-Paket + Image, Claiming/Publish/
Finalize, Health/Metriken, Tests, dieses ADR. **Nicht** enthalten (Gate D3B):
Publisher-Service in `sites/dc/docker-compose.yml`, Queue-/Netzwerk-Route für
site-dc, Prometheus-Scrape, Alert-Regeln, Deployment-Skript, Live-Verifikation,
Aktivierung. `EVENTS_ENABLED` bleibt unverändert; Phase 3 ist nicht aktiviert.

## Gate-D3B1-Ergänzung (Repository-Wiring)
- **Compose-Service** `publisher` in `sites/dc/docker-compose.yml`: eigenes Image
  `hol-publisher:dev` (Kontext `apps/publisher`), **`PUBLISHER_ENABLED: "false"` hart
  kodiert** (keine Env-Substitution), eigene `inventory_publisher`-DSN, **eigene**
  Publisher-Route-Variablen (`PUBLISHER_SQS_*`, nicht die Inventory-`SQS_*`), Host-Port
  für /metrics, Healthcheck `/healthz`, `depends_on` db+inventory-migrate; non-root,
  keine Volumes/Socket/privileged/Host-Net.
- **Secret-Isolation:** `INVENTORY_PUBLISHER_PASSWORD` geht **nur** an `db-bootstrap`
  und `publisher`; `.env.example` enthält nur einen leeren Platzhalter. Lab-Grenze:
  Compose-Env ist in Container-Metadaten sichtbar (Docker Secrets/Secret-Manager wären
  produktiv vorzuziehen).
- **Monitoring:** Prometheus-`file_sd`-Job `publisher` + committetes
  `publisher.json.example`; reale `publisher.json` gitignored und atomar erzeugt
  (`ops/deploy/render-publisher-target.sh`, Input-validiert). Eigene Alert-Gruppe;
  **`PublisherDown` via `up{job="publisher"}==0`** (dauerhaftes Target nötig), alle
  fachlichen Alerts mit `publisher_enabled == 1` gegatet (ein disabled Publisher
  alarmiert nicht). Backlog-/Age-/Claim-Sicht hängt am lebenden Publisher → keine
  unabhängige Backlog-Überwachung.
- **Fail-closed-Guards:** `make up` startet site-dc **nur** nach erfolgreichem,
  strukturell validiertem Phase-3-State (`check-phase-3-runtime-state.py`); kein
  Make-Ziel aktiviert den Publisher. **Getrenntes** Upgrade-Skript
  `ops/deploy/upgrade-phase-3-runtime.sh` (flock, atomarer JSON-State, Resume,
  Variante B, kein Enable, keine Override-Aktivierung) — `upgrade-site-dc.sh` (Gate A)
  bleibt byte-identisch.
- **Getrenntes D3B2-Live-Gate:** tatsächliches Deployment, Migration `0004` live,
  Monitoring-Verifikation, **bewusste** Aktivierung, E2E-Nachweis, Disable-/Rollback-Test.

## Konsequenzen
- Robuste, einfach testbare Publish-Mechanik ohne lange DB-Transaktionen.
- Duplikate möglich, aber idempotent abgefangen — keine Exactly-once-Zusage.
- Eine Inventory-Migration `0004` koppelt an `KNOWN_MIGRATIONS` (Inventory lehnt
  unbekannt-neueres Schema fail closed ab) → koordinierter Inventory-Rebuild/-Recreate
  (Variante B) im Phase-3-Upgrade-Skript.
