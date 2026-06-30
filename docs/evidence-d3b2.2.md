# D3B2.2 — Runtime evidence

Synthetic lab environment, not production.

Verified on 30 June 2026:

- Migration `0004_add_outbox_claim_fields` applied on `site-dc`.
- Inventory and Publisher are healthy.
- Publisher remains disabled: `publisher_enabled=0.0`.
- Existing data remained unchanged.
- Outbox state: 2 pending, 0 published, 0 claimed.
- Main queue and DLQ remained empty.
- `inventory_publisher` has no administrative or unnecessary data privileges.
- Publisher Prometheus target is `up`.
- All Prometheus targets are healthy: `8/8`.
- Existing D3B2.1 runtime state remains valid.

Runtime state:

```text
step: complete (valid/complete)
```

D3B2.2 is complete. The Publisher is not activated and no end-to-end event flow
has been validated. Activation and E2E validation remain part of D3B2.3.
