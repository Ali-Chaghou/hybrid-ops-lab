-- 0004_add_outbox_claim_fields — Publisher-Claim-Felder + Least-Privilege-Grants
-- fuer die getrennte Rolle inventory_publisher (Phase 3, Gate D3A).
--
-- Als Owner/Admin-Rolle (inventory_admin) in EINER Transaktion ausgefuehrt;
-- inventory_publisher muss bereits existieren (Bootstrap erstellt die Rolle VOR
-- der Migration). Rein ADDITIV: bestehende pending/published-Zeilen (inkl. des
-- Gate-A-Backfills) bleiben unveraendert und gueltig. KEIN neuer Status (status
-- bleibt 'pending'|'published'), KEINE Payload-/event_id-Mutation, KEINE Loeschung.

-- 1. Claim-Beobachtungsfelder (Lease wird ueber das vorhandene available_at
--    abgebildet; claimed_at/claim_owner dienen Fencing + Stuck-Claim-Sicht). --------
ALTER TABLE event_outbox
    ADD COLUMN claimed_at  timestamptz NULL,
    ADD COLUMN claim_owner text        NULL;

-- claim_owner ist ein opaker Token (kein Hostname/IP/Benutzer) und laengenbegrenzt.
ALTER TABLE event_outbox
    ADD CONSTRAINT event_outbox_claim_owner_len_chk
    CHECK (claim_owner IS NULL OR char_length(claim_owner) <= 64);

-- claimed_at und claim_owner sind immer GEMEINSAM gesetzt oder GEMEINSAM NULL.
ALTER TABLE event_outbox
    ADD CONSTRAINT event_outbox_claim_pair_chk
    CHECK ((claimed_at IS NULL) = (claim_owner IS NULL));

-- Eine bereits publizierte Zeile traegt KEINE Claim-Felder mehr.
ALTER TABLE event_outbox
    ADD CONSTRAINT event_outbox_published_no_claim_chk
    CHECK (status <> 'published' OR (claimed_at IS NULL AND claim_owner IS NULL));

-- 2. Least-Privilege fuer inventory_publisher --------------------------------------
-- Ausschliesslich: USAGE auf das Schema, SELECT auf event_outbox + schema_migrations
-- (Startup-Schema-Check) und SPALTENWEISES UPDATE nur auf die Publisher-Statusfelder.
-- KEIN Zugriff auf stock_movements, KEIN INSERT/DELETE auf event_outbox, keine DDL.
-- Bewusst KEIN zusaetzlicher Index: die Outbox ist im Lab klein; Claim nutzt den
-- bestehenden partiellen Index (available_at, created_at, event_id) WHERE pending,
-- Stuck-Claim-Metriken sind ein guenstiger Scan ueber wenige Zeilen.
GRANT USAGE ON SCHEMA public TO inventory_publisher;
GRANT SELECT ON event_outbox      TO inventory_publisher;
GRANT SELECT ON schema_migrations TO inventory_publisher;
GRANT UPDATE (status, attempt_count, available_at, published_at, last_error, claimed_at, claim_owner)
    ON event_outbox TO inventory_publisher;
