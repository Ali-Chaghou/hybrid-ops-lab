-- 0003_create_event_outbox — Transactional Outbox (Phase 2B, site-dc).
-- Als Owner/Admin-Rolle (inventory_admin) in EINER Transaktion ausgefuehrt;
-- inventory_app muss bereits existieren (Bootstrap).
--
-- Ziel: stock_movements und event_outbox bilden eine strikte 1-zu-1-Beziehung,
-- die die DATENBANK selbst erzwingt — nicht die Anwendung. Ein Movement darf nur
-- gemeinsam mit genau einem Outbox-Event committen und umgekehrt.
--
-- Reihenfolge (wichtig wegen der wechselseitigen, zyklischen Foreign Keys):
--   1. zusammengesetzten UNIQUE-Schluessel auf stock_movements anlegen (FK-Ziel),
--   2. event_outbox anlegen, inkl. unmittelbarem FK -> stock_movements(id, event_id),
--   3. Bestandszeilen backfillen (je Movement genau ein pending-Event),
--   4. ERST DANACH den rueckwaerts gerichteten DEFERRABLE-FK
--      stock_movements(event_id) -> event_outbox(event_id) ergaenzen.
-- Der Backfill muss vor Schritt 4 fertig sein, sonst verletzen Bestandszeilen den
-- rueckwaerts gerichteten FK.

-- 1. FK-Ziel: (id, event_id) eindeutig auf stock_movements ----------------------
-- id ist bereits PRIMARY KEY; die zusammengesetzte UNIQUE wird als exaktes Ziel
-- des zusammengesetzten FK aus event_outbox benoetigt.
ALTER TABLE stock_movements
    ADD CONSTRAINT stock_movements_id_event_id_key UNIQUE (id, event_id);

-- 2. event_outbox: dauerhafte Uebergabegrenze ----------------------------------
CREATE TABLE event_outbox (
    event_id       uuid        PRIMARY KEY,
    -- genau ein Outbox-Event pro Movement (UNIQUE) und ...
    movement_id    bigint      NOT NULL UNIQUE,
    event_type     text        NOT NULL,
    schema_version integer     NOT NULL,
    occurred_at    timestamptz NOT NULL,
    source         text        NOT NULL,
    payload        jsonb       NOT NULL,
    status         text        NOT NULL DEFAULT 'pending',
    attempt_count  integer     NOT NULL DEFAULT 0,
    available_at   timestamptz NOT NULL DEFAULT now(),
    created_at     timestamptz NOT NULL DEFAULT now(),
    published_at   timestamptz NULL,
    last_error     text        NULL,
    -- Feste Contract-Werte (Envelope v1) auf DB-Ebene gebunden.
    CONSTRAINT event_outbox_type_chk      CHECK (event_type = 'inventory.movement.recorded'),
    CONSTRAINT event_outbox_schema_chk    CHECK (schema_version = 1),
    CONSTRAINT event_outbox_source_chk    CHECK (source = 'inventory-service'),
    CONSTRAINT event_outbox_status_chk    CHECK (status IN ('pending', 'published')),
    CONSTRAINT event_outbox_attempt_chk   CHECK (attempt_count >= 0),
    CONSTRAINT event_outbox_payload_chk   CHECK (jsonb_typeof(payload) = 'object'),
    -- published_at genau dann gesetzt, wenn status = published.
    CONSTRAINT event_outbox_published_chk CHECK ((status = 'published') = (published_at IS NOT NULL)),
    -- ... und das Event MUSS exakt auf (id, event_id) seines Movements zeigen.
    -- Unmittelbar (nicht deferred): zum Insert-Zeitpunkt existiert die Movement-Zeile
    -- bereits in derselben Transaktion. Eine falsche (movement_id, event_id)-Kombination
    -- oder ein Event ohne Movement scheitert damit sofort.
    CONSTRAINT event_outbox_movement_fk
        FOREIGN KEY (movement_id, event_id) REFERENCES stock_movements (id, event_id)
);

-- 2b. Partieller Index fuer den spaeteren Publisher -----------------------------
-- Der Publisher liest faellige pending-Events (WHERE status = 'pending') und
-- ordnet voraussichtlich nach available_at/created_at. Der partielle Index haelt
-- nur pending-Zeilen vor und verhindert Full-Table-Scans, wenn die Outbox waechst
-- (published-Zeilen bleiben im Lab als Archiv liegen). event_id als drittes
-- Indexfeld macht die Ordnung deterministisch und deckt die Lookup-Spalte mit ab.
CREATE INDEX event_outbox_pending_available_idx
    ON event_outbox (available_at, created_at, event_id)
    WHERE status = 'pending';

-- 3. Backfill: je Bestands-Movement genau ein pending-Event ---------------------
-- Bestehende Movements wurden nie produktiv publiziert -> dieselbe event_id,
-- movement_id = id, occurred_at = created_at, payload mit exakt vier Feldern.
-- status/attempt_count/available_at/created_at/published_at/last_error kommen aus
-- den Spalten-Defaults (pending, 0, now(), now(), NULL, NULL).
INSERT INTO event_outbox (event_id, movement_id, event_type, schema_version, occurred_at, source, payload)
SELECT
    m.event_id,
    m.id,
    'inventory.movement.recorded',
    1,
    m.created_at,
    'inventory-service',
    jsonb_build_object(
        'movement_id', m.id,
        'sku',         m.sku,
        'quantity',    m.quantity,
        'warehouse',   m.warehouse
    )
FROM stock_movements m;

-- 4. Rueckwaerts gerichteter FK: Movement nur MIT Event committbar --------------
-- DEFERRABLE INITIALLY DEFERRED, weil der Runtime-Pfad das Movement VOR seinem
-- Outbox-Event einfuegt; die Pruefung erfolgt erst beim Commit. Ein Movement ohne
-- passendes Event scheitert damit spaetestens beim Commit.
ALTER TABLE stock_movements
    ADD CONSTRAINT stock_movements_event_outbox_fk
    FOREIGN KEY (event_id) REFERENCES event_outbox (event_id)
    DEFERRABLE INITIALLY DEFERRED;

-- 5. Least privilege fuer die Inventory-Runtime-Rolle ---------------------------
-- inventory_app darf das Outbox-Event ausschliesslich EINFUEGEN und dabei NUR die
-- Producer-Spalten setzen. Die operativen Publisher-Spalten (status, attempt_count,
-- available_at, created_at, published_at, last_error) bleiben einer getrennten
-- Publisher-Rolle in Phase 3 vorbehalten und kommen beim Runtime-Insert aus ihren
-- Defaults. Kein SELECT, kein UPDATE, kein DELETE. Auf stock_movements bleibt es bei
-- SELECT + INSERT (aus 0002).
REVOKE ALL ON event_outbox FROM inventory_app;
GRANT INSERT (event_id, movement_id, event_type, schema_version, occurred_at, source, payload)
    ON event_outbox TO inventory_app;
