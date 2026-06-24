# Runbook — Phase-3-Runtime-Upgrade (site-dc, Outbox-Publisher)

Kontrolliertes Upgrade von site-dc auf die neue Inventory-Version (kennt Migration
`0004`) und einen **deaktivierten** Outbox-Publisher. **Aktiviert nichts** — weder den
Publisher noch den Event-Versand. Orchestriert durch
`ops/deploy/upgrade-phase-3-runtime.sh`.

> All infrastructure names, roles, records and runtime evidence shown here belong to
> an isolated synthetic lab environment and do not represent an employer, customer or
> production system. Keine echten Adressen/Secrets in diesem Dokument.

## D3B1 vs. D3B2
- **D3B1 (dieser Stand, im Repository):** Service-Wiring (Publisher disabled), sichere
  Credential-Verdrahtung, Route-Konfiguration, Prometheus-Scrape + Alerts, das
  kontrollierte Upgrade-Skript, Fail-closed-Guards, Tests, Doku. **Kein** Live-Zugriff,
  **keine** Migration einer bestehenden Umgebung, **keine** Aktivierung.
- **D3B2 (späteres, getrenntes Live-Gate):** tatsächliches Live-Deployment, Migration
  `0004` auf der VM, Monitoring-Verifikation, **bewusste** Aktivierung
  (`PUBLISHER_ENABLED=true`), End-to-End-Nachweis, Disable-/Rollback-Test.

## Voraussetzungen
- `sites/dc/.env` (gitignored, Modus `0600`) mit gesetztem `INVENTORY_PUBLISHER_PASSWORD`
  und definierten `PUBLISHER_SQS_*`-Route-Variablen (leer = disabled).
- Geprüftes Backup (wie bei Gate A).
- Gate D1 und D2 **live deployed und verifiziert** (Voraussetzung für die spätere
  Aktivierung in D3B2 — nicht für D3B1).

## Warum Variante B
`check_schema()` ist **fail closed in beide Richtungen**: die neue Inventory-Version
verlangt `0004` (sonst Startverweigerung), die alte Version lehnt `0004` als
„unbekannt neuer" ab. Variante C (neue Version vor der Migration) ist daher unmöglich;
Variante A (alte Version läuft während der Migration weiter) ist riskant (jeder
Neustart nach `0004` schlägt fehl; `ADD CONSTRAINT` nimmt kurz `ACCESS EXCLUSIVE`).
**Gewählt: Variante B** — altes Inventory kontrolliert stoppen, `0004` ausführen, neue
Version starten.

## Downtime-Fenster
Zwischen dem Stoppen des alten Inventory (Schritt „inventory-stopped") und dem Ready
der neuen Version (Schritt „inventory-ready") besteht ein kurzes, definiertes
Inventory-Downtime-Fenster. Der Outbox-Schreibpfad ist in dieser Zeit nicht erreichbar;
Bestandsdaten bleiben unverändert.

## Preflight
`./ops/deploy/upgrade-phase-3-runtime.sh preflight` prüft u. a.: richtiges Repository,
benötigte Dateien, `.env` vorhanden + nicht world-readable, `INVENTORY_PUBLISHER_PASSWORD`
gesetzt (ohne Wertausgabe), Route-Variablen definiert, **Basis-Compose hat den Publisher
hart `disabled`**, **keine aktivierende Override-Datei**, docker/compose verfügbar.

## Upgrade-Schritte (Variante B)
`./ops/deploy/upgrade-phase-3-runtime.sh run` durchläuft die State-Maschine:
`preflight → images-built → roles-ready → inventory-stopped → migration-complete →
inventory-ready → publisher-disabled-ready → complete`.
1. Inventory- und Publisher-Image bauen. 2. `db` starten + `db-bootstrap` (legt
`inventory_publisher` an) + `db-prepare` (idempotent). 3. **Altes Inventory stoppen.**
4. `inventory-migrate` (idempotent; wendet `0004` an). 5. Neue Inventory-Version starten
+ `/readyz` (validiert Schema inkl. `0004`). 6. Publisher **disabled** starten +
`/healthz` + `publisher_enabled=0` nachweisen. 7. State atomar auf `complete`.

## State & Resume
- Lokaler State: `sites/dc/.phase3-runtime/state.json` (gitignored), atomar geschrieben
  (Tempdatei + `mv`), **kein** Secret, bildet nur den **Ablaufstatus** ab (nie das
  Schema). Strukturell validiert via `ops/deploy/check-phase-3-runtime-state.py`.
- `resume` setzt nach dem letzten abgeschlossenen Schritt fort; Phasen sind idempotent
  (Bootstrap/Prepare/Migrate). **Beschädigter/unbekannter State → fail closed Abbruch**
  (kein blindes Fortsetzen). Migration wird **nicht** doppelt ausgeführt (Runner ist
  Source of Truth). Bei jedem Resume bleibt der Publisher **disabled**.
- `flock` serialisiert konkurrierende Läufe.

## Fehler vor/nach Migration
- **Fehler ≤ `inventory-stopped`:** altes Schema bleibt; das gestoppte alte Inventory
  darf kontrolliert wieder gestartet werden (`docker compose up -d inventory`, altes
  Image).
- **Fehler ≥ `migration-complete`:** altes Inventory **nicht** starten (lehnt `0004`
  fail closed ab) — mit `resume` und der **neuen** Version fortsetzen. Das Skript gibt
  den passenden Hinweis aus.

## Publisher bleibt disabled
Das Skript nutzt ausschließlich das Basis-Compose mit `PUBLISHER_ENABLED: "false"` und
**verweigert** den Lauf bei einer aktivierenden Override-Datei. Es setzt **nie**
`PUBLISHER_ENABLED=true` und sendet **keine** Queue-Nachricht.

## Monitoring-Target (Lifecycle — wichtig)
Prometheus (auf site-cloud) scrapt den Publisher über `file_sd`
(`/etc/prometheus/targets/publisher.json`, gitignored). Die Datei wird vom Orchestrator
atomar erzeugt (`ops/deploy/render-publisher-target.sh`, aus `DC_HOST`+`PUBLISHER_HOST_PORT`),
**keine** harte Adresse im Repo.

**Das Target wird bewusst NICHT durch den generischen Code-Sync verteilt.** Andernfalls
entstünde `up{job="publisher"}==0` und damit ein falscher `PublisherDown`-Voralarm,
bevor der Publisher überhaupt (disabled) deployed ist. Daher:
- **`sync`/`cloud-up`** erzeugen/installieren **kein** Publisher-Target (`cloud-up` darf
  vor D3B2 Gate D1/D2 live vorbereiten, ohne PublisherDown auszulösen). Der generische
  `rsync` schließt `monitoring/prometheus/targets/publisher.json` explizit aus.
- **`phase3-upgrade`** installiert das Target **erst nach** erfolgreichem site-dc-Upgrade
  (Publisher disabled läuft, `/healthz` ok, `/metrics` erreichbar, State `complete` &
  `publisher_enabled=false`): es erzeugt die Datei lokal atomar und synct **ausschließlich
  diese eine Datei** an den gemounteten Monitoring-Pfad auf site-cloud. Ein Fehler im
  Upgrade installiert **kein** Target.
- **`up`** erzeugt/aktualisiert das Target **nur nach** erfolgreicher Remote-State-Prüfung;
  fehlender/ungültiger State bricht **vor** der Target-Erzeugung ab.

Daraus folgt: **Vor D3B2 existiert absichtlich keine `up{job="publisher"}`-Serie.** Nach
dem ersten erfolgreichen disabled-Deployment muss das Target **dauerhaft** bestehen
bleiben; ab dann ist `PublisherDown` (`up==0`) das primäre Prozessausfallsignal. Ein
**fehlendes** Target nach erfolgreichem Upgrade ist ein **Fehler**.

**Monitoring-Grenze:** Backlog-/Age-/Claim-Gauges stammen vom Publisher und verschwinden
bei dessen Ausfall; der Outbox-Backlog ist dann **nicht** unabhängig beobachtbar (kein
PostgreSQL-/Outbox-Exporter).

## ElasticMQ-Credentials (Lab)
Der Publisher-Service setzt **synthetische** Emulator-Credentials (`AWS_ACCESS_KEY_ID`/
`AWS_SECRET_ACCESS_KEY` = `test`) und `AWS_EC2_METADATA_DISABLED=true` — dasselbe
öffentliche Lab-Muster wie der Consumer. ElasticMQ ignoriert die Werte; boto3 braucht
nur nicht-leere Credentials, sonst durchsucht botocore die Provider-Chain (inkl.
EC2-Metadata) und scheitert beim Enable. **Keine echten Secrets, nicht konfigurierbar.**
Gegen echtes AWS wären **Workload Identity / eine IAM-Rolle** erforderlich. Die Werte
gehen **nur** an den Publisher (nicht an Inventory/Migration/Bootstrap).

## Lab-Secret-Grenze
Das Publisher-Passwort wird im Compose über `${INVENTORY_PUBLISHER_PASSWORD}` aus der
gitignorten `.env` in die DSN substituiert (nur `db-bootstrap` und `publisher`). Wie bei
Inventory ist es dadurch in `docker inspect`/Container-Metadaten sichtbar — das ist die
akzeptierte **Lab-Grenze** des Compose-Musters. Im echten Betrieb wären Docker Secrets
oder ein externer Secret-Manager vorzuziehen.

## Disable-/Rollback-Modell
- **Disable (Notaus):** `PUBLISHER_ENABLED=false` + Publisher neu starten → keine neuen
  Claims, laufender Publish läuft begrenzt fertig, Pending-Zeilen bleiben, **keine**
  Outbox-Zeile gelöscht.
- **Runtime-Rollback:** alte Publisher-Version **nicht** starten (kennt `0004` nicht);
  Publisher-Service entfernen/disablen; Inventory läuft weiter; Schema `0004` bleibt
  (kein Down-Migration-Zwang).
- **Stale Claims:** Lease ablaufen lassen → automatisch re-claimbar; **keine** manuelle
  Mutation von `event_id`/Payload.
- **Queue-Ausfall:** Publisher disablen, Backlog beobachten, nach Ursachenbehebung
  kontrolliert re-enablen.

## D3B2-Aktivierungs-Gate (später, manuell)
Vor `PUBLISHER_ENABLED=true`: Release-Artefakt bestätigt; Inventory auf Version mit
`KNOWN_MIGRATIONS=0004`; `0004` angewandt; Publisher-Rolle vorhanden + Least Privilege;
Publisher disabled & healthy; Queue-Route erreichbar; Consumer live & ready; Main-Queue +
DLQ korrekt; Prometheus scrapt Consumer **und** Publisher; Alerts geladen; **keine**
unerklärten DLQ-Nachrichten; kontrollierte Probe **nur** über den echten Outbox-Pfad;
dokumentierter Disable-Befehl; benannter Beobachtungs-Verantwortlicher. **Aktivierung
erfolgt bewusst, nie automatisch im Deploy.**

## Grenzen
- **Keine** Exactly-once-Garantie (at-least-once; Consumer-Idempotenz fängt Duplikate ab).
- **Keine** direkte Queue-Testnachricht außerhalb des Outbox-Pfads.
- D3B1 ist **commit-ready, nicht deployment-ready**; die Live-Verifikation erfolgt in D3B2.
