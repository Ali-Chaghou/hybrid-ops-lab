-- 0001_create_stock_movements — Basistabelle (site-dc).
-- Idempotent: existiert die Tabelle bereits (z. B. aus einer aelteren Installation),
-- ist dies ein No-op. Die stabile event_id kommt in 0002 hinzu.
CREATE TABLE IF NOT EXISTS stock_movements (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sku         TEXT        NOT NULL,
    quantity    INTEGER     NOT NULL,
    warehouse   TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
