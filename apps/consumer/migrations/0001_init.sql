-- 0001_init — Idempotency-Store des Consumers (site-cloud).
-- Als Owner/Admin-Rolle in EINER Transaktion ausgefuehrt; consumer_app muss existieren.
-- Wegen der wechselseitigen (zyklischen) Foreign Keys zwischen event_inbox und
-- movement_projection werden zuerst beide Tabellen ohne FK angelegt, dann die
-- benoetigten UNIQUE-Constraints, danach die FKs per ALTER.

-- 1. Tabellen ohne Foreign Keys -------------------------------------------------
CREATE TABLE event_inbox (
    event_id            uuid        PRIMARY KEY,
    event_type          text        NOT NULL,
    source              text        NOT NULL,
    source_movement_id  bigint      NOT NULL,
    schema_version      smallint    NOT NULL,
    fingerprint         char(64)    NOT NULL,
    disposition         text        NOT NULL,
    canonical_event_id  uuid        NULL,
    -- Zeitpunkt der Verarbeitung innerhalb der spaeter erfolgreich
    -- abgeschlossenen Transaktion (kein exakter Commit-Zeitpunkt).
    processed_at        timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT event_inbox_type_chk        CHECK (event_type = 'inventory.movement.recorded'),
    CONSTRAINT event_inbox_source_chk      CHECK (source = 'inventory-service'),
    CONSTRAINT event_inbox_schema_chk      CHECK (schema_version = 1),
    CONSTRAINT event_inbox_fingerprint_chk CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT event_inbox_disposition_chk CHECK (disposition IN ('applied', 'business_duplicate')),
    -- applied: erstmaliger Effekt (keine kanonische Referenz).
    -- business_duplicate: zeigt auf das urspruengliche angewandte Event.
    CONSTRAINT event_inbox_canonical_chk CHECK (
        (disposition = 'applied'            AND canonical_event_id IS NULL)
        OR (disposition = 'business_duplicate' AND canonical_event_id IS NOT NULL)
    )
);

CREATE TABLE movement_projection (
    source             text        NOT NULL,
    source_movement_id bigint      NOT NULL,
    source_event_id    uuid        NOT NULL,
    sku                text        NOT NULL,
    quantity           integer     NOT NULL,
    warehouse          text        NOT NULL,
    occurred_at        timestamptz NOT NULL,
    applied_at         timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- (source, movement_id) statt nur movement_id: IDs verschiedener kuenftiger
    -- Quellen kollidieren nicht versehentlich.
    PRIMARY KEY (source, source_movement_id)
);

-- 2. UNIQUE-Constraints (Voraussetzung fuer die FKs) ----------------------------
-- Ein Event wendet hoechstens eine Projektion an.
ALTER TABLE movement_projection
    ADD CONSTRAINT movement_projection_source_event_id_key UNIQUE (source_event_id);
-- Ziel des zusammengesetzten FK aus event_inbox (muss exakt diese Spalten abdecken).
ALTER TABLE movement_projection
    ADD CONSTRAINT movement_projection_src_mov_evt_key UNIQUE (source, source_movement_id, source_event_id);

-- 3. Foreign Keys per ALTER -----------------------------------------------------
-- (a) Erstmalig angewandtes Event: die Projektion zeigt auf ihr Event.
--     DEFERRABLE INITIALLY DEFERRED, weil im applied-Pfad die Projektion VOR der
--     event_inbox-Zeile eingefuegt wird; die Pruefung erfolgt erst beim Commit.
ALTER TABLE movement_projection
    ADD CONSTRAINT movement_projection_event_fk
    FOREIGN KEY (source_event_id) REFERENCES event_inbox (event_id)
    DEFERRABLE INITIALLY DEFERRED;

-- (b) Business-Duplicate: die kanonische Referenz MUSS auf die existierende
--     Projektion DESSELBEN source + source_movement_id zeigen. MATCH SIMPLE:
--     bei canonical_event_id IS NULL (applied) wird der FK nicht geprueft.
--     NICHT deferred: im business_duplicate-Pfad existiert die referenzierte,
--     bereits committete Projektion zum Insert-Zeitpunkt.
ALTER TABLE event_inbox
    ADD CONSTRAINT event_inbox_canonical_projection_fk
    FOREIGN KEY (source, source_movement_id, canonical_event_id)
    REFERENCES movement_projection (source, source_movement_id, source_event_id);

-- 4. Least privilege fuer die Runtime-Rolle -------------------------------------
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO consumer_app;
GRANT SELECT, INSERT ON event_inbox         TO consumer_app;
GRANT SELECT, INSERT ON movement_projection TO consumer_app;
GRANT SELECT ON schema_migrations           TO consumer_app;
