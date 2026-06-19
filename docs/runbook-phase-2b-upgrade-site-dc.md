# Runbook — Phase-2B-Upgrade von site-dc (bestehende Installation)

Kontrollierter, idempotenter Upgrade einer **bestehenden** site-dc-Installation auf
den Phase-2B-Stand (Migrationen 0001–0003, `event_outbox` inkl. Backfill).
Orchestriert durch `ops/deploy/upgrade-site-dc.sh`.

## Geltungsbereich / Ist-Zustand

- Live-Compose-Projekt: `hol-site-dc`, PostgreSQL-Volume `hol-site-dc_pgdata`.
- Bestehende DB `inventory`: Tabelle `stock_movements` (genau 1 Datensatz), **keine**
  `schema_migrations`, Owner = Alt-Rolle `inventory` (zugleich Cluster-Superuser).
- Rollen `inventory_admin` / `inventory_app` existieren noch **nicht**.
- Altes, schmutziges Live-Repo `/home/ops/hybrid-ops-lab` — wird **nie** beschrieben.
- Geprüftes Backup: `/home/ops/backups/hybrid-ops-lab/20260618T222629Z`.

## Kernproblem (warum ein reiner `up` scheitert)

`db-prepare` setzt nur den **Datenbank-Owner** (`ALTER DATABASE … OWNER`). Die
bestehende Tabelle `stock_movements` und ihre Identity-Sequenz gehören weiterhin
der Alt-Rolle `inventory`. Die Migrationen laufen als `inventory_admin` und führen
`ALTER TABLE` / `GRANT` / `REVOKE` aus — alle setzen **Objekt-Ownership** voraus.
Ohne Gegenmaßnahme bricht Migration `0002` mit „must be owner of table
stock_movements" ab.

**Warum `REASSIGN OWNED` in dieser Topologie verboten ist:** Die Alt-Rolle
`inventory` ist zugleich der **Bootstrap-Superuser** und besitzt deshalb
**geteilte, cluster-weite Objekte** — die Datenbanken `postgres`, `template0`,
`template1` sowie die Tablespaces `pg_default`/`pg_global`. `REASSIGN OWNED BY
inventory` wirkt **cluster-weit** auch auf diese geteilten Objekte und scheitert
mit `DependentObjectsStillExist`. Der erste Rollout brach genau daran ab (Phase
`prepare-done`). Der Produktionscode führt `REASSIGN OWNED` daher **nicht** aus.

**Lösung — gezielte Ownership-Übertragung (`ops.db.reassign`):**
- Enumeriert **alle** Objekte der Alt-Rolle in der Ziel-DB und bricht **vor** jeder
  Mutation ab, wenn ein unerwartetes Schema, ein unerwarteter Name oder ein
  unerwarteter Relationstyp auftaucht (Allowlist: `stock_movements%`, Schema
  `public`, Relkinds Tabelle/partitioniert/Sequenz/Index).
- Überträgt nur mit **objekt-spezifischen** Statements:
  `ALTER TABLE public.stock_movements OWNER TO inventory_admin` und
  `ALTER SEQUENCE public.stock_movements_id_seq OWNER TO inventory_admin`.
  Der **Primary-Key-Index** wird **nicht** separat verändert — sein Owner folgt
  automatisch der Tabelle und wird danach **verifiziert**.
- Führt die ALTERs in **einer Transaktion** aus, verifiziert vor dem Commit, dass
  keine allowlistete Relation mehr der Alt-Rolle gehört und alle erwarteten
  Relationen `inventory_admin` gehören, und **rollt bei Fehler zurück**.
- **Geteilte Objekte** (`postgres`, `template0`, `template1`, `pg_default`,
  `pg_global`) werden nur **inspiziert/protokolliert, niemals verändert**. Ein
  **unerwartetes** geteiltes Objekt (zusätzliche DB/Tablespace der Alt-Rolle)
  führt zum **Abbruch** vor jeder Mutation.
- **Idempotent:** bereits korrekte Ownership → No-op; fehlende Alt-Rolle → No-op;
  Wiederholung nach erfolgreicher Übertragung → No-op.

Zusammen mit `db-prepare` (DB-Owner → `inventory_admin` → Mitglied von
`pg_database_owner` → Owner des `public`-Schemas) sind danach alle Ownership-Fälle
abgedeckt — ohne Eingriff in geteilte/cluster-weite Objekte.

## Release-Prozess (Deployment-Quelle)

> Die neuen Dateien (`ops/db/reassign.py`, `ops/deploy/upgrade-site-dc.sh`,
> Tests, dieses Runbook) gehören **noch nicht** zum bestehenden Release `7cd85fc`.
> Dieses Release bleibt unverändert. Es gibt **keinen** ausführbaren Pfad
> `…/releases/hybrid-ops-lab/7cd85fc/ops/deploy/upgrade-site-dc.sh`.

Korrekter Weg von der Änderung bis zur Deployment-Quelle:

1. Änderungen auf einem **Feature-Branch** prüfen (Tests grün, pre-commit, shellcheck).
2. **Commit + Push** erst nach Freigabe.
3. **Pull Request** öffnen, **CI** durchlaufen lassen.
4. **Merge nach `main`** → ergibt einen freigegebenen **Merge-Commit** `<NEW>`.
5. Ein **unveränderliches Release-Verzeichnis** aus `<NEW>` erstellen, z. B.:
   ```bash
   git -C /home/ops/src/hybrid-ops-lab fetch origin
   git -C /home/ops/src/hybrid-ops-lab worktree add \
       /home/ops/releases/hybrid-ops-lab/<NEW> <NEW>
   ```
   (oder `git archive <NEW> | tar -x -C …/<NEW>` für ein reines Snapshot).
6. **Erst dieses neue Release** als Deployment-Quelle nutzen — `RELEASE_DIR` und
   `EXPECTED_COMMIT` zeigen auf `<NEW>`, nicht auf `7cd85fc`.

Der **Preflight** erzwingt diese Herkunft:

- `RELEASE_DIR` ist ein **sauberes** Git-Worktree (`git status --porcelain` leer;
  ignorierte Dateien wie `.env` zählen nicht).
- `HEAD` == `EXPECTED_COMMIT`.
- `ops/db/reassign.py` **und** `ops/deploy/upgrade-site-dc.sh` gehören zu diesem
  Commit (`git cat-file -e HEAD:<pfad>`).
- `RELEASE_DIR`/`STATE_FILE` liegen **nicht** im alten Repo; `.env` liegt **nur**
  im Release. Das alte Repo wird nicht beschrieben.

## Voraussetzungen vor dem Rollout

1. Release wie oben aus dem freigegebenen Merge-Commit erstellt.
2. **Release-`.env`** (separat, Modus `0600`, niemals die alte `.env` ändern) mit
   den **bestehenden** Cluster-Admin-Credentials der laufenden DB:
   `POSTGRES_USER=inventory`, `POSTGRES_PASSWORD=<bestehend>`, `POSTGRES_DB=postgres`,
   `INVENTORY_DB=inventory`.
   - `POSTGRES_PASSWORD` wird **nie** generiert (muss zum Volume passen); Preflight
     verifiziert die Verbindung.
   - `INVENTORY_ADMIN_PASSWORD` / `INVENTORY_APP_PASSWORD` werden vom Skript sicher
     erzeugt, falls leer (keine Rotation bestehender Werte, keine Secret-Ausgabe).
3. Geprüftes Backup. Empfohlen: ein Custom-Format-Dump `*.dump` (+ optional
   `SHA256SUMS` oder `EXPECTED_BACKUP_SHA256`). Preflight prüft **SHA256** und
   **`pg_restore -l`**.

## Rollout-Reihenfolge

```bash
RELEASE_DIR=/home/ops/releases/hybrid-ops-lab/<NEW>
export RELEASE_DIR EXPECTED_COMMIT=<NEW> \
       BACKUP_DIR=/home/ops/backups/hybrid-ops-lab/20260618T222629Z

# 1. Nur prüfen (rein lesend, keine destruktive Mutation):
"$RELEASE_DIR/ops/deploy/upgrade-site-dc.sh" preflight

# 2. Nach Freigabe – vollständiger, kontrollierter Upgrade:
"$RELEASE_DIR/ops/deploy/upgrade-site-dc.sh" rollout
```

Die **Zustandsmaschine** (Statusdatei außerhalb des Worktrees) durchläuft:

| Phase | Aktion | Pre-2B-Rückstart sicher? |
|---|---|---|
| `preflight-ok` | Herkunft/Backup/DB-Ist geprüft | ja |
| `built` | Image gebaut **und Image-Inhalt verifiziert** (kein Downtime) | ja |
| `old-runtime-stopped` | alte Runtime gestoppt (Downtime beginnt) | ja |
| `bootstrap-done` | Rollen angelegt | ja |
| `prepare-done` | DB-Owner → inventory_admin | ja |
| `reassign-done` | Altobjekte → inventory_admin (gezielte `ALTER … OWNER`, Allowlist) | ja |
| **`migrate-started`** | **Migration läuft — Grenze** | **nein** |
| `migrate-done` | 0001/0002/0003 angewandt | nein |
| `runtime-up` | neue Runtime gestartet | nein |
| `verified` / `complete` | Verifikation bestanden | nein |

> **Unterbrochener Erst-Rollout (`prepare-done`) ist ein unterstützter, idempotenter
> Wiederholungs-Zustand.** Nach dem ersten Abbruch existieren bereits die Rollen und
> `inventory`-DB-Owner = `inventory_admin` (additiv, mit der alten Runtime
> kompatibel). Ein erneuter `rollout` ist gefahrlos: `db-bootstrap` (Rollen) und
> `db-prepare` (DB-Owner) sind idempotent, und die gezielte Ownership-Übertragung
> ist ebenfalls idempotent (bereits korrekte Ownership → No-op). Das Schema ist
> weiterhin Pre-2B (keine `schema_migrations`, kein `event_outbox`).

### Image-Build & -Verifikation (vor dem Downtime)

Das gemeinsame Image `hol-inventory:dev` (Setup **und** Runtime) wird über den
Service **`db-bootstrap`** gebaut — den **einzigen** Service mit `build:`-Sektion in
`sites/dc/docker-compose.yml`. Der Service `inventory` referenziert das Image nur
(`image: hol-inventory:dev`), hat **keine** `build:`-Sektion; ein `dc build
inventory` würde daher nichts Buildbares treffen und könnte stillschweigend ein
**altes** Image wiederverwenden. Deshalb: `dc build db-bootstrap`.

**Unmittelbar nach dem Build, noch VOR jedem Stop/Downtime**, verifiziert der
Rollout den Image-Inhalt gegen die freigegebenen Release-Dateien: SHA256-Vergleich
für `ops/db/reassign.py` (→ `/app/ops/db/reassign.py`), `apps/inventory/app/main.py`
(→ `/app/app/main.py`) und `apps/inventory/migrations/0003_create_event_outbox.sql`
(→ `/app/migrations/0003_create_event_outbox.sql`). Die Prüfung pinnt auf die
unveränderliche **Image-ID** (nicht nur das Tag, das geloggt wird), liest die
Image-Dateien in einem kurzlebigen `--rm`-Container (kein Stop/Restart laufender
Container) und **bricht vor dem Downtime ab**, falls eine Datei fehlt oder ein Hash
abweicht. Erst danach wird Phase `built` gesetzt und die alte Runtime gestoppt.

Die DB-Setup-Schritte laufen als kurzlebige `docker compose run --rm --no-deps`
Container im selben Projekt/Volume; der `db`-Container wird **nie** neu erstellt.

## Verifikation (automatisch im `verify`)

- `schema_migrations` enthält genau `0001`, `0002`, `0003`.
- **DB-Owner** = `inventory_admin`; **Tabellen-Owner** (`stock_movements`,
  `event_outbox`) und **Sequenz-Owner** = `inventory_admin`.
- **Rollenattribute**: `inventory_admin`/`inventory_app` sind **kein** Superuser,
  ohne `CREATEROLE`/`CREATEDB`/`BYPASSRLS`.
- **Outbox-Backfill 1:1**: `count(event_outbox) == count(stock_movements)`, alle
  `pending`, jede Bewegung hat ein passendes `(movement_id, event_id)`-Event.
- **Runtime ohne Admin-Rechte**: läuft als `inventory_app`; `CREATE`/`UPDATE`/
  `DELETE` und jeder `SELECT`/`UPDATE`/`DELETE` auf `event_outbox` werden verweigert.
- **Echter `POST /movements`** (durch die App) liefert 201 und erzeugt **atomar**
  genau ein Movement **und** ein passendes Outbox-Event mit gleicher `event_id`.

## Rollback — phasenabhängig

> **Wichtig:** Nach Migration `0003` existiert der FK
> `stock_movements(event_id) → event_outbox(event_id)`. Die **Pre-2B-Runtime**
> schreibt kein Outbox-Event und würde beim Commit scheitern. Ein automatischer
> Start der alten Runtime ist deshalb **nur vor Migrationsbeginn** erlaubt.

```bash
"$RELEASE_DIR/ops/deploy/upgrade-site-dc.sh" state      # aktuelle Phase
"$RELEASE_DIR/ops/deploy/upgrade-site-dc.sh" rollback   # phasenabhängige Anleitung
```

### A) Vor Migrationsbeginn (Phase ≤ `reassign-done`)

Bootstrap/Prepare/Ownership-Übertragung sind für die als Cluster-Superuser
verbundene Pre-2B-App neutral. **Robuster Rückstart des bestehenden alten
Containers:**

```bash
"$RELEASE_DIR/ops/deploy/upgrade-site-dc.sh" rollback --restart-old
```

Ein **gemeinsamer Helfer** (`restart_old_runtime`) wird sowohl vom automatischen
Fehler-Handler (`die`) als auch vom manuellen `--restart-old`-Pfad genutzt. Er:
- startet **ausschließlich** den **bestehenden** alten Container
  `${PROJECT}-inventory-1` mit `docker start` (exakter Container, altes Image);
- verwendet **niemals** `docker compose up` / `dc up` — das würde den Service vor
  der Migration mit dem **neuen** Phase-2B-Image **neu erstellen**;
- **unterdrückt keine Fehler** (kein `|| true`): er prüft, dass der Container
  existiert, nach `docker start` **läuft** und innerhalb eines begrenzten Timeouts
  **healthy** wird (hat das alte Image keinen Healthcheck, gilt „running" als
  Erfolg);
- meldet **klaren Erfolg oder Fehlschlag**. Schlägt die Wiederherstellung fehl,
  bleibt der ursprüngliche Rollout-Fehler bestehen und es wird ausdrücklich
  **manueller Eingriff** verlangt — die alte Runtime wird **nicht** stillschweigend
  als „down" zurückgelassen.

### B) Ab `migrate-started` (Schema ist Phase-2B)

**Kein** automatischer Pre-2B-Start. `--restart-old` wird hier **verweigert**. Es gibt:

- **Option 1 — Forward-Fix:** neue, Outbox-fähige Runtime erneut starten
  `docker compose -p hol-site-dc -f <RELEASE>/sites/dc/docker-compose.yml --env-file <RELEASE>/sites/dc/.env up -d --no-deps inventory`

- **Option 2 — Vollständiger DB-Restore** (manuell bestätigt). Vollständige Schritte:
  1. **Neue Runtime stoppen:** `docker compose -p hol-site-dc -f <COMPOSE> --env-file <ENV> stop inventory`
  2. **DB-Verbindungen beenden / db stoppen:** `… stop db`
  3. **Aktuellen (migrierten) Zustand sichern oder verwerfen** (forensische Sicherung):
     `docker run --rm -v hol-site-dc_pgdata:/v -v "$PWD":/out alpine tar czf /out/pre-rollback-pgdata.tgz -C /v .`
  4. **Backup aus `BACKUP_DIR` wiederherstellen** — eine der beiden Varianten:
     - *Volume-Snapshot:* `docker run --rm -v hol-site-dc_pgdata:/v -v <BACKUP_DIR>:/b:ro alpine sh -c 'rm -rf /v/* && tar xzf /b/pgdata.tgz -C /v'`
     - *Logischer Dump:* frische DB anlegen und `pg_restore -d inventory /b/<dump>.dump`
       im `db`-Container einspielen (Owner/Rollen aus dem Dump bzw. via Bootstrap).
  5. **Owner & Rollen des Alt-Zustands prüfen:** `db` starten (`… start db`), dann
     `pg_get_userbyid(datdba)` für `inventory` und Owner von `stock_movements`
     kontrollieren (Alt-Rolle `inventory`), `schema_migrations` ist wieder abwesend.
  6. **Alte Runtime starten:** `cd /home/ops/hybrid-ops-lab/sites/dc && docker compose up -d inventory`
  7. **Health + echter Schreib-/Lesevorgang prüfen:** `/readyz` == 200, ein
     `POST /movements` (201) und ein `GET /movements` gegen die alte App.

Der eigentliche Daten-Restore (Schritt 4) bleibt bewusst **manuell** (kein
automatisches Überschreiben des Volumes durch das Skript).

## Bewusste Grenzen / Restrisiken

- Die Alt-Rolle `inventory` bleibt (sie ist der Cluster-Superuser/`PG_ADMIN`) und
  damit weiterhin Superuser — out of scope dieses Upgrades. **Nicht löschen**,
  solange sie als Cluster-Admin genutzt wird; Deprivilegierung ist ein Folgeschritt.
- Die gezielten `ALTER … OWNER` benötigen kurz `AccessExclusiveLock` auf
  `stock_movements`; deshalb wird die alte Runtime **vor** Ownership-Übertragung/
  Migration gestoppt (kurzes Downtime-Fenster). `REASSIGN OWNED` wird **nicht**
  verwendet (siehe oben).
- `POSTGRES_PASSWORD` muss dem **bestehenden** Cluster-Superuser-Passwort
  entsprechen (im Volume fixiert); der Preflight verifiziert die Verbindung.
- Backup-Layout: das Skript erwartet bevorzugt einen `*.dump` (Custom-Format) zur
  `pg_restore -l`-Validierung; ein reiner `*.sql`/`*.sql.gz` wird nur auf
  Integrität (Existenz/gzip) geprüft.
