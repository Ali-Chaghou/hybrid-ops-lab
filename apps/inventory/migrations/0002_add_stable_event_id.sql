-- 0002_add_stable_event_id — stabile, persistierte event_id auf stock_movements.
-- Idempotent und fuer BEIDE Wege geeignet:
--   * Clean Install: 0001 hat die Basistabelle (ohne event_id) angelegt; hier kommt sie hinzu.
--   * Existing Upgrade: bestehende Tabelle mit Daten -> Spalte ergaenzen + backfillen.
-- Bestehende Movement-Daten bleiben unveraendert.
--
-- Lab-Hinweis: Auf einer grossen produktiven Tabelle waeren Backfill in Batches,
-- CREATE UNIQUE INDEX CONCURRENTLY und SET NOT NULL ohne langes Lock getrennt zu
-- bewerten. Im Lab ist die Tabelle klein -> alle Schritte in einer Transaktion ok.

-- 1. Spalte zunaechst NULLABLE hinzufuegen (idempotent).
ALTER TABLE stock_movements ADD COLUMN IF NOT EXISTS event_id uuid;

-- 2. Bestandszeilen einmalig mit je eigener UUID backfillen.
UPDATE stock_movements SET event_id = gen_random_uuid() WHERE event_id IS NULL;

-- 3. Pruefen: keine NULLs, keine Duplikate verbleiben (sonst Abbruch der Migration).
DO $$
DECLARE
    n_null int;
    n_dup  int;
BEGIN
    SELECT count(*) INTO n_null FROM stock_movements WHERE event_id IS NULL;
    IF n_null > 0 THEN
        RAISE EXCEPTION 'backfill incomplete: % NULL event_id', n_null;
    END IF;
    SELECT count(*) INTO n_dup FROM (
        SELECT event_id FROM stock_movements GROUP BY event_id HAVING count(*) > 1
    ) d;
    IF n_dup > 0 THEN
        RAISE EXCEPTION 'duplicate event_id detected: % groups', n_dup;
    END IF;
END $$;

-- 4. Default fuer neue Zeilen (genau einmal pro Insert, nicht pro Publish).
ALTER TABLE stock_movements ALTER COLUMN event_id SET DEFAULT gen_random_uuid();

-- 5. Unique-Index (idempotent; im Lab nicht CONCURRENTLY noetig).
CREATE UNIQUE INDEX IF NOT EXISTS stock_movements_event_id_key ON stock_movements (event_id);

-- 6. Erst jetzt NOT NULL (idempotent).
ALTER TABLE stock_movements ALTER COLUMN event_id SET NOT NULL;

-- 7. Least privilege fuer die Inventory-Runtime-Rolle: nur lesen + einfuegen.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO inventory_app;
-- id ist GENERATED ALWAYS AS IDENTITY -> INSERT-Recht genuegt, keine Sequenz-Grants noetig.
GRANT SELECT, INSERT ON stock_movements TO inventory_app;
-- Runtime muss die Schema-Version lesen koennen (check_schema beim Start).
GRANT SELECT ON schema_migrations TO inventory_app;
