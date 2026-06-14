# 005 — ElasticMQ statt LocalStack als SQS-Endpoint

## Status
Akzeptiert (2026-06-14)

## Kontext
site-cloud braucht einen lokalen, SQS-kompatiblen Endpoint für den
Event-Flow. Ursprünglich war LocalStack vorgesehen (siehe Phasenplan).

Seit dem 23.03.2026 hat LocalStack die Community-Edition eingestellt:
Beide Images sind zu einem konsolidiert, der Start erfordert einen
LOCALSTACK_AUTH_TOKEN (kostenloser Account nötig). Der Notausgang
LS_ACKNOWLEDGE_ACCOUNT_REQUIREMENT=1 lief nur bis 06.04.2026.

Das widerspricht ADR-001 (lokal/autark, keine externe Abhängigkeit)
und dem Projektprinzip "keine Secrets, minimaler nötiger Zugriff":
ein Vendor-Token in der .env würde das gesamte Lab gaten.

## Entscheidung
site-cloud nutzt ElasticMQ (softwaremill/elasticmq, gepinnt auf 1.6.16)
als SQS-Endpoint. ElasticMQ ist zweckgebunden (nur SQS), ausgereift,
ohne Account/Token, und stellt einen echten HTTP-Endpoint bereit, der
hinter Toxiproxy (Phase 5) gesetzt und vom Consumer (Phase 4) erreicht
werden kann.

Verworfene Alternativen:
- LocalStack + kostenloser Token: externer Account + Secret in .env,
  gegen ADR-001.
- LocalStack 4.14 pinnen: archiviert, keine Security-Patches.
- floci/fakecloud (token-freie Klone): zu jung/wenig erprobt.

## Konsequenzen
- Die App bleibt unverändert (SQS_ENDPOINT_URL ist bereits Variable).
- ElasticMQ kann nur SQS. Falls eine spätere Phase weitere AWS-Dienste
  braucht, ist diese Entscheidung neu zu bewerten.
- ElasticMQ meldet Queue-URLs standardmäßig mit localhost. Für
  Cross-Site-Zugriff (andere VM) ist node-address zu konfigurieren —
  wird in Step 3 (Queue-Anlage) gelöst.
