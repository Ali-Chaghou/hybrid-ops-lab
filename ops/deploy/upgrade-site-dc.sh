#!/usr/bin/env bash
#
# upgrade-site-dc.sh — kontrollierter, idempotenter Phase-2B-Upgrade von site-dc.
#
# Bringt eine BESTEHENDE alte Installation (DB-/Tabellen-Owner = Alt-Rolle
# `inventory`, kein schema_migrations, Rollen inventory_admin/_app fehlen) sicher
# auf den Phase-2B-Stand (Migrationen 0001..0003, event_outbox-Backfill).
#
# Kernproblem: `db-prepare` setzt nur den DATENBANK-Owner. Bestehende Tabellen/
# Sequenzen gehoeren weiter der Alt-Rolle, sodass die als inventory_admin laufenden
# Migrationen an ALTER/GRANT scheitern. Schritt "reassign" uebertraegt die Objekte
# kontrolliert (mit Allowlist) an inventory_admin.
#
# Deployment-Quelle ist ein UNVERAENDERLICHES Release-Verzeichnis (RELEASE_DIR), das
# aus einem freigegebenen Merge-Commit (EXPECTED_COMMIT) exportiert wurde. Das alte,
# schmutzige Live-Repo (OLD_REPO_DIR) wird NIE beschrieben. Keine Secrets im Log.
#
# Rollback ist PHASEN-ABHAENGIG: vor Migrationsbeginn ist ein automatischer
# Rueckstart der alten Runtime sicher; nach Migrationsbeginn NICHT (Phase-2B-Schema
# hat den FK stock_movements.event_id -> event_outbox; die Pre-2B-App wuerde beim
# Commit scheitern). Siehe docs/runbook-phase-2b-upgrade-site-dc.md.
#
# Aufruf (RELEASE_DIR/EXPECTED_COMMIT/BACKUP_DIR explizit setzen):
#   RELEASE_DIR=/home/ops/releases/hybrid-ops-lab/<commit> \
#   EXPECTED_COMMIT=<commit> \
#   BACKUP_DIR=/home/ops/backups/hybrid-ops-lab/20260618T222629Z \
#     "$RELEASE_DIR/ops/deploy/upgrade-site-dc.sh" preflight
set -euo pipefail

# --- Konfiguration (per ENV) --------------------------------------------------
RELEASE_DIR="${RELEASE_DIR:-}"            # Pflicht: unveraenderliches Release-Worktree
EXPECTED_COMMIT="${EXPECTED_COMMIT:-}"    # Pflicht fuer preflight/rollout: freigegebener Commit
BACKUP_DIR="${BACKUP_DIR:-}"             # Pflicht fuer preflight/rollout: geprueftes Backup
BACKUP_DUMP="${BACKUP_DUMP:-}"           # optional: konkreter Dump-Dateiname/-Pfad
EXPECTED_BACKUP_SHA256="${EXPECTED_BACKUP_SHA256:-}"  # optional: erwarteter Hash
PROJECT="${PROJECT:-hol-site-dc}"
VOLUME="${VOLUME:-hol-site-dc_pgdata}"
OLD_REPO_DIR="${OLD_REPO_DIR:-/home/ops/hybrid-ops-lab}"
OLD_OWNER_ROLE="${OLD_OWNER_ROLE:-inventory}"
PG_IMAGE="${PG_IMAGE:-postgres:16-alpine}"
EXPECTED_MIGRATIONS="0001_create_stock_movements 0002_add_stable_event_id 0003_create_event_outbox"

[ -n "$RELEASE_DIR" ] || { echo "FEHLER: RELEASE_DIR nicht gesetzt." >&2; exit 2; }
COMPOSE="${RELEASE_DIR}/sites/dc/docker-compose.yml"
ENV_FILE="${ENV_FILE:-${RELEASE_DIR}/sites/dc/.env}"
# Statusdatei AUSSERHALB des Worktrees (sonst wuerde sie das saubere Release trueben).
STATE_FILE="${STATE_FILE:-${RELEASE_DIR%/*}/.hol-upgrade-$(basename "$RELEASE_DIR").state}"

INVENTORY_DB=""
IMAGE_ID=""

log()  { printf '\n[upgrade-site-dc] %s\n' "$*"; }
fail() { printf '\n[upgrade-site-dc] FEHLER: %s\n' "$*" >&2; exit 1; }

dc() { docker compose -p "$PROJECT" -f "$COMPOSE" --env-file "$ENV_FILE" "$@"; }

admin_py() {
  dc run --rm --no-deps -T \
    -e CHECK_DB="$INVENTORY_DB" \
    -e EXPECTED_MIGRATIONS="$EXPECTED_MIGRATIONS" \
    -e OLD_OWNER_ROLE="$OLD_OWNER_ROLE" \
    db-prepare python -
}

# --- Zustandsmaschine ---------------------------------------------------------
set_state() { printf '%s\n' "$1" > "$STATE_FILE"; log "Phase -> $1"; }
get_state() { [ -f "$STATE_FILE" ] && cat "$STATE_FILE" || printf 'init'; }
migration_started() {
  case "$(get_state)" in
    migrate-started|migrate-done|runtime-up|verified|complete) return 0 ;;
    *) return 1 ;;
  esac
}

# Phasenabhaengiger Fehler-Abbruch. Startet die alte Runtime NUR vor Migrationsbeginn.
die() {
  local msg="$1"
  printf '\n[upgrade-site-dc] FEHLER: %s (Phase: %s)\n' "$msg" "$(get_state)" >&2
  if migration_started; then
    cat >&2 <<EOF

[upgrade-site-dc] KEIN automatischer Rueckstart der alten Runtime: das Schema ist
bereits (teil-)migriert (Phase 2B). Die Pre-2B-App wuerde wegen
  stock_movements.event_id -> event_outbox (FK)
beim Commit scheitern. Optionen:
  1) Neue, Outbox-faehige Runtime reparieren/erneut starten:
       docker compose -p ${PROJECT} -f ${COMPOSE} --env-file ${ENV_FILE} up -d --no-deps inventory
  2) Vollstaendiger DB-Restore aus dem Backup, dann alte Runtime:
       "$0" rollback        (siehe docs/runbook-phase-2b-upgrade-site-dc.md)
EOF
  else
    log "Pre-Migration-Fehler -> sichere Wiederherstellung: alte Runtime (re)starten"
    dc start inventory >/dev/null 2>&1 || true
  fi
  exit 1
}

gen_pw() {
  local raw
  raw="$(head -c 18 /dev/urandom | base64)"
  raw="${raw//[^A-Za-z0-9]/}"
  [ "${#raw}" -ge 20 ] || fail "Passwortgenerierung lieferte zu wenig Entropie"
  printf '%s' "${raw:0:24}"
}

env_get() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- || true; }

require_cmds() {
  command -v docker >/dev/null 2>&1 || fail "docker nicht gefunden"
  docker compose version >/dev/null 2>&1 || fail "docker compose Plugin nicht gefunden"
  command -v git >/dev/null 2>&1 || fail "git nicht gefunden"
  command -v sha256sum >/dev/null 2>&1 || fail "sha256sum nicht gefunden"
}

container_running() {
  [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null || echo false)" = "true" ]
}

# --- Release-Herkunft pruefen -------------------------------------------------
verify_release_source() {
  [ -d "$RELEASE_DIR" ] || fail "RELEASE_DIR fehlt: ${RELEASE_DIR}"
  [ -f "$COMPOSE" ]     || fail "Compose-Datei fehlt im Release: ${COMPOSE}"
  [ -n "$EXPECTED_COMMIT" ] || fail "EXPECTED_COMMIT nicht gesetzt (freigegebener Merge-Commit)"

  # Schreibgrenzen: niemals in das alte Repo schreiben; .env nur im Release.
  case "$RELEASE_DIR" in
    "$OLD_REPO_DIR"|"$OLD_REPO_DIR"/*) fail "RELEASE_DIR darf nicht im alten Repo liegen: ${OLD_REPO_DIR}" ;;
  esac
  case "$ENV_FILE" in
    "$RELEASE_DIR"/*) : ;;
    *) fail "ENV_FILE muss innerhalb des Release liegen: ${ENV_FILE}" ;;
  esac
  case "$STATE_FILE" in
    "$OLD_REPO_DIR"/*) fail "STATE_FILE darf nicht im alten Repo liegen" ;;
  esac

  git -C "$RELEASE_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
    || fail "RELEASE_DIR ist kein Git-Worktree: ${RELEASE_DIR}"
  # Sauberes Worktree (ignorierte Dateien wie .env zaehlen nicht).
  [ -z "$(git -C "$RELEASE_DIR" status --porcelain)" ] \
    || fail "RELEASE_DIR ist kein sauberes Git-Worktree (uncommittete Aenderungen)"
  # HEAD == freigegebener Commit.
  local head
  head="$(git -C "$RELEASE_DIR" rev-parse HEAD)"
  case "$head" in
    "$EXPECTED_COMMIT"*) : ;;
    *) fail "RELEASE_DIR HEAD (${head}) != EXPECTED_COMMIT (${EXPECTED_COMMIT})" ;;
  esac
  # Die Upgrade-Dateien muessen Teil dieses Commits sein.
  for f in ops/db/reassign.py ops/deploy/upgrade-site-dc.sh; do
    git -C "$RELEASE_DIR" cat-file -e "HEAD:${f}" 2>/dev/null \
      || fail "Datei gehoert nicht zum freigegebenen Commit: ${f}"
  done
  log "Release verifiziert: sauberes Worktree @ ${head} (Upgrade-Tooling im Commit)"
  log "Schreibgrenzen ok: nur RELEASE_DIR/.env + STATE_FILE; OLD_REPO_DIR bleibt unberuehrt."
}

# --- Secrets: Release-.env (0600), Cluster-Admin erwarten, Rollen-PW erzeugen ---
ensure_env() {
  if [ ! -f "$ENV_FILE" ]; then
    umask 077
    : > "$ENV_FILE"
    log "Neue Release-.env angelegt (Modus 600). Cluster-Admin-Credentials eintragen."
  fi
  chmod 600 "$ENV_FILE"

  local pu pp
  pu="$(env_get POSTGRES_USER)"; pp="$(env_get POSTGRES_PASSWORD)"
  INVENTORY_DB="$(env_get INVENTORY_DB)"; : "${INVENTORY_DB:=inventory}"
  [ -n "$pu" ] || fail "POSTGRES_USER fehlt in ${ENV_FILE} (bestehender Cluster-Admin erwartet, NICHT generiert)"
  [ -n "$pp" ] || fail "POSTGRES_PASSWORD fehlt in ${ENV_FILE} (muss zum bestehenden Volume passen, NICHT generiert)"

  # Maintenance-DB fuer Cluster-Admin-Verbindungen.
  [ -n "$(env_get POSTGRES_DB)" ]   || printf 'POSTGRES_DB=postgres\n'   >> "$ENV_FILE"
  [ -n "$(env_get INVENTORY_DB)" ]  || printf 'INVENTORY_DB=%s\n' "$INVENTORY_DB" >> "$ENV_FILE"
  # Rollen-Passwoerter NUR erzeugen, wenn nicht vorhanden (keine Rotation).
  local changed=0
  [ -n "$(env_get INVENTORY_ADMIN_PASSWORD)" ] || { umask 077; printf 'INVENTORY_ADMIN_PASSWORD=%s\n' "$(gen_pw)" >> "$ENV_FILE"; changed=1; }
  [ -n "$(env_get INVENTORY_APP_PASSWORD)" ]   || { umask 077; printf 'INVENTORY_APP_PASSWORD=%s\n'   "$(gen_pw)" >> "$ENV_FILE"; changed=1; }
  [ -n "$(env_get INVENTORY_HOST_PORT)" ] || printf 'INVENTORY_HOST_PORT=8000\n' >> "$ENV_FILE"
  [ -n "$(env_get EVENTS_ENABLED)" ]      || printf 'EVENTS_ENABLED=false\n'      >> "$ENV_FILE"
  [ -n "$(env_get AWS_REGION)" ]          || printf 'AWS_REGION=eu-central-1\n'   >> "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  [ "$changed" -eq 1 ] && log "Rollen-Passwoerter erzeugt (nicht ausgegeben). POSTGRES_PASSWORD unveraendert."
  dc config --quiet || fail "Compose-Konfiguration ungueltig"
}

# --- Backup validieren (SHA256 + pg_restore -l) -------------------------------
locate_dump() {
  if [ -n "$BACKUP_DUMP" ]; then
    case "$BACKUP_DUMP" in /*) printf '%s' "$BACKUP_DUMP" ;; *) printf '%s/%s' "$BACKUP_DIR" "$BACKUP_DUMP" ;; esac
    return
  fi
  local f
  f="$(find "$BACKUP_DIR" -maxdepth 1 -type f -name '*.dump' | sort | head -1)"
  [ -n "$f" ] || f="$(find "$BACKUP_DIR" -maxdepth 1 -type f \( -name '*.sql' -o -name '*.sql.gz' \) | sort | head -1)"
  printf '%s' "$f"
}

validate_backup() {
  [ -n "$BACKUP_DIR" ] || fail "BACKUP_DIR nicht gesetzt (geprueftes Backup erwartet)"
  [ -d "$BACKUP_DIR" ] || fail "BACKUP_DIR existiert nicht: ${BACKUP_DIR}"
  local dump; dump="$(locate_dump)"
  [ -n "$dump" ] && [ -f "$dump" ] || fail "Kein Dump in BACKUP_DIR (erwartet *.dump/*.sql[.gz]); ggf. BACKUP_DUMP setzen"

  if [ -f "${BACKUP_DIR}/SHA256SUMS" ]; then
    ( cd "$BACKUP_DIR" && sha256sum -c SHA256SUMS ) >/dev/null || fail "SHA256SUMS-Verifikation fehlgeschlagen"
    log "Backup-Integritaet via SHA256SUMS bestaetigt"
  elif [ -n "$EXPECTED_BACKUP_SHA256" ]; then
    local got; got="$(sha256sum "$dump" | cut -d' ' -f1)"
    [ "$got" = "$EXPECTED_BACKUP_SHA256" ] || fail "Backup-SHA256 weicht vom erwarteten Wert ab"
    log "Backup-SHA256 gegen EXPECTED_BACKUP_SHA256 bestaetigt"
  else
    local got; got="$(sha256sum "$dump" | cut -d' ' -f1)"
    log "Backup-SHA256 (protokolliert): ${got}  $(basename "$dump")"
    log "WARN: kein SHA256SUMS/EXPECTED_BACKUP_SHA256 -> Integritaet nur protokolliert"
  fi

  case "$dump" in
    *.dump)
      docker run --rm -v "$BACKUP_DIR":/b:ro "$PG_IMAGE" pg_restore -l "/b/$(basename "$dump")" >/dev/null \
        || fail "pg_restore -l fehlgeschlagen (Dump unlesbar/korrupt)"
      log "Backup-Struktur via 'pg_restore -l' bestaetigt ($(basename "$dump"))" ;;
    *.sql.gz) gzip -t "$dump" || fail "gzip-Integritaet des SQL-Dumps fehlgeschlagen"; log "SQL.gz-Integritaet ok" ;;
    *.sql)    [ -s "$dump" ] || fail "SQL-Dump ist leer"; log "SQL-Dump vorhanden (kein pg_restore -l moeglich)" ;;
  esac
}

# --- Preflight: rein lesend, keine destruktive Mutation -----------------------
preflight() {
  log "PREFLIGHT"
  require_cmds
  verify_release_source
  ensure_env
  validate_backup

  docker volume inspect "$VOLUME" >/dev/null 2>&1 || fail "PostgreSQL-Volume fehlt: ${VOLUME}"
  for c in "${PROJECT}-db-1" "${PROJECT}-inventory-1"; do
    container_running "$c" || fail "Erwarteter Live-Container laeuft nicht: ${c}"
  done
  log "Live-Container laufen: ${PROJECT}-db-1, ${PROJECT}-inventory-1, Volume ${VOLUME}"

  log "DB-Ist-Zustand + Objekt-Inventar protokollieren (Cluster-Admin, rein lesend)"
  admin_py <<'PY' || fail "Preflight-DB-Pruefung fehlgeschlagen"
import os, sys
import psycopg
errs, warn = [], []
dsn = os.environ["PG_ADMIN_DSN"]; db = os.environ["CHECK_DB"]; old = os.environ["OLD_OWNER_ROLE"]
try:
    with psycopg.connect(dsn, dbname="postgres", autocommit=True) as c:
        owner = c.execute("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=%s", (db,)).fetchone()
        if owner is None:
            errs.append(f"Datenbank {db} existiert nicht")
        else:
            print(f"  db-owner(ist)={owner[0]}")
    with psycopg.connect(dsn, dbname=db, autocommit=True) as c:
        sm = c.execute("SELECT to_regclass('public.stock_movements')").fetchone()[0]
        if sm is None:
            errs.append("stock_movements fehlt")
        else:
            o = c.execute("SELECT pg_get_userbyid(relowner) FROM pg_class WHERE relname='stock_movements'").fetchone()[0]
            n = c.execute("SELECT count(*) FROM stock_movements").fetchone()[0]
            print(f"  stock_movements-owner(ist)={o}, rows={n}")
        if c.execute("SELECT to_regclass('public.schema_migrations')").fetchone()[0] is not None:
            warn.append("schema_migrations existiert bereits (Re-Run/teilmigriert?)")
        for role in ("inventory_admin", "inventory_app"):
            if c.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone() is not None:
                warn.append(f"Rolle {role} existiert bereits (Re-Run?)")
        # Objekt-Inventar der Alt-Rolle protokollieren (keine Secrets).
        rows = c.execute(
            "SELECT n.nspname, x.relname, x.relkind FROM pg_class x "
            "JOIN pg_namespace n ON n.oid=x.relnamespace JOIN pg_roles r ON r.oid=x.relowner "
            "WHERE r.rolname=%s AND n.nspname NOT IN ('pg_catalog','information_schema') "
            "AND n.nspname !~ '^pg_toast' ORDER BY 1,2", (old,)
        ).fetchall()
        print(f"  Objekte im Besitz von {old}: " + (", ".join(f"{a}.{b}({c_})" for a,b,c_ in rows) or "(keine)"))
        for nsp, name, _k in rows:
            if nsp != "public" or not name.startswith("stock_movements"):
                errs.append(f"unerwartetes Objekt im Besitz von {old}: {nsp}.{name}")
except psycopg.OperationalError as e:
    errs.append(f"Cluster-Admin-Verbindung fehlgeschlagen ({type(e).__name__})")
for w in warn: print(f"  WARN: {w}")
if errs:
    print("  FAIL: " + "; ".join(errs), file=sys.stderr); sys.exit(1)
print("  PREFLIGHT-DB OK")
PY
  log "PREFLIGHT bestanden. Fuer den Rollout (nach Freigabe): '$0 rollout'."
}

# --- Image-Inhalt verifizieren (nach Build, VOR Downtime) ---------------------
# Vergleicht SHA256 der Release-Dateien mit ihren Kopien IM frisch gebauten Image
# hol-inventory:dev. Pinnt auf die unveraenderliche Image-ID (nicht nur das Tag) und
# liest die Image-Dateien in einem kurzlebigen --rm-Container (kein Stop/Restart
# laufender Container). Bricht VOR jedem Downtime ab, wenn etwas fehlt/abweicht.
_check_image_file() {
  local host_rel="$1" img_path="$2" host_file host_sum img_sum
  host_file="${RELEASE_DIR}/${host_rel}"
  [ -f "$host_file" ] || die "Release-Datei fehlt: ${host_file}"
  host_sum="$(sha256sum "$host_file" | cut -d' ' -f1)"
  # Hash im Image via Python (Bestandteil des Images); fehlende Datei -> nonzero.
  img_sum="$(docker run --rm --entrypoint python "$IMAGE_ID" -c \
    "import hashlib; print(hashlib.sha256(open('${img_path}','rb').read()).hexdigest())" 2>/dev/null)" \
    || die "Datei im Image fehlt/unlesbar: ${img_path} (Abbruch VOR Downtime)"
  [ -n "$img_sum" ] || die "Kein Hash im Image fuer ${img_path}"
  [ "$host_sum" = "$img_sum" ] \
    || die "Image-Inhalt weicht ab: ${host_rel} != ${img_path} (Release != Image, Abbruch VOR Downtime)"
  log "OK Image-Inhalt: ${host_rel} == ${img_path}"
}

verify_image_content() {
  IMAGE_ID="$(docker image inspect -f '{{.Id}}' hol-inventory:dev 2>/dev/null)" \
    || die "Image hol-inventory:dev nach Build nicht gefunden"
  [ -n "$IMAGE_ID" ] || die "Konnte Image-ID von hol-inventory:dev nicht ermitteln"
  log "Gebautes Image: hol-inventory:dev = ${IMAGE_ID}"
  _check_image_file "ops/db/reassign.py"                                "/app/ops/db/reassign.py"
  _check_image_file "apps/inventory/app/main.py"                        "/app/app/main.py"
  _check_image_file "apps/inventory/migrations/0003_create_event_outbox.sql" "/app/migrations/0003_create_event_outbox.sql"
  log "Image-Inhalt gegen Release verifiziert (3/3 Dateien, SHA256 identisch) — vor Downtime."
}

# --- Rollout: Zustandsmaschine ------------------------------------------------
rollout() {
  preflight
  set_state preflight-ok

  # Das gemeinsame Image hol-inventory:dev wird ueber db-bootstrap gebaut — den
  # EINZIGEN Service mit 'build:'-Sektion in der Compose-Datei. (Ein 'build inventory'
  # haette mangels build:-Sektion ggf. ein altes Image stillschweigend wiederverwendet.)
  log "BUILD: gemeinsames Image hol-inventory:dev via db-bootstrap aus ${RELEASE_DIR} (kein Downtime)"
  dc build db-bootstrap || die "Image-Build fehlgeschlagen"
  # Inhalt des frisch gebauten Images gegen die Release-Dateien verifizieren — VOR
  # jedem Stop/Downtime. Bricht bei fehlender Datei oder Hash-Abweichung sofort ab.
  verify_image_content
  set_state built

  log "STOP: alte Runtime kontrolliert anhalten (db bleibt online -> Downtime beginnt)"
  dc stop inventory || die "Konnte alte Runtime nicht stoppen"
  set_state old-runtime-stopped

  log "SETUP 1/4: db-bootstrap (Rollen anlegen)"
  dc run --rm --no-deps db-bootstrap || die "db-bootstrap fehlgeschlagen"
  set_state bootstrap-done

  log "SETUP 2/4: db-prepare (Datenbank-Owner -> inventory_admin)"
  dc run --rm --no-deps db-prepare || die "db-prepare fehlgeschlagen"
  set_state prepare-done

  log "SETUP 3/4: reassign (Altobjekte ${OLD_OWNER_ROLE} -> inventory_admin, mit Allowlist)"
  dc run --rm --no-deps db-prepare \
    python -m ops.db.reassign --database "$INVENTORY_DB" --from-role "$OLD_OWNER_ROLE" \
    || die "Ownership-Reassign fehlgeschlagen (vor Migration) — alte Runtime wird sicher gestartet"
  set_state reassign-done

  # --- Ab hier ist das Schema nach Erfolg Phase-2B und Pre-2B-inkompatibel. ---
  log "SETUP 4/4: inventory-migrate (0001..0003 als inventory_admin)"
  set_state migrate-started
  dc run --rm --no-deps inventory-migrate || die "Migration fehlgeschlagen — KEIN Pre-2B-Rueckstart"
  set_state migrate-done

  log "RUNTIME: neuen inventory-Service starten (nur dieser Service, --no-deps)"
  dc up -d --no-deps inventory || die "Konnte neue Runtime nicht starten"
  set_state runtime-up

  log "Auf Readiness warten"
  local ok=0 cid st
  for _ in $(seq 1 24); do
    cid="$(dc ps -q inventory)"
    st="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid" 2>/dev/null || echo none)"
    [ "$st" = "healthy" ] && { ok=1; break; }
    sleep 2
  done
  [ "$ok" -eq 1 ] || die "inventory wurde nicht healthy"

  verify
  set_state verified
  log "ROLLOUT ERFOLGREICH. Downtime-Fenster beendet."
  set_state complete
}

# --- Verifikation -------------------------------------------------------------
verify() {
  [ -n "$INVENTORY_DB" ] || ensure_env
  log "VERIFY: Migrationen, DB-/Tabellen-/Sequenz-Owner, Rollenattribute, Outbox-Backfill"
  admin_py <<'PY' || fail "Verifikation (Admin) fehlgeschlagen"
import os, sys
import psycopg
errs = []
dsn = os.environ["PG_ADMIN_DSN"]; db = os.environ["CHECK_DB"]
expected = set(os.environ["EXPECTED_MIGRATIONS"].split())
with psycopg.connect(dsn, dbname="postgres", autocommit=True) as c:
    dbo = c.execute("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=%s", (db,)).fetchone()[0]
    if dbo != "inventory_admin":
        errs.append(f"db-owner={dbo} (erwartet inventory_admin)")
    for role in ("inventory_admin", "inventory_app"):
        r = c.execute(
            "SELECT rolsuper, rolcreaterole, rolcreatedb, rolbypassrls FROM pg_roles WHERE rolname=%s",
            (role,)).fetchone()
        if r is None:
            errs.append(f"Rolle {role} fehlt")
        elif any(r):
            errs.append(f"Rolle {role} hat unerwartete Admin-Attribute {r}")
with psycopg.connect(dsn, dbname=db, autocommit=True) as c:
    applied = {x[0] for x in c.execute("SELECT version FROM schema_migrations").fetchall()}
    if not expected <= applied:
        errs.append(f"Migrationen unvollstaendig: fehlt {sorted(expected - applied)}")
    if applied - expected:
        errs.append(f"unbekannte Migrationen: {sorted(applied - expected)}")
    # Tabellen-Owner + Sequenz-Owner.
    for tbl in ("stock_movements", "event_outbox"):
        if c.execute("SELECT to_regclass(%s)", (f"public.{tbl}",)).fetchone()[0] is None:
            errs.append(f"Tabelle {tbl} fehlt"); continue
        o = c.execute("SELECT pg_get_userbyid(relowner) FROM pg_class WHERE relname=%s", (tbl,)).fetchone()[0]
        if o != "inventory_admin":
            errs.append(f"{tbl}-owner={o} (erwartet inventory_admin)")
    seqs = c.execute(
        "SELECT x.relname, pg_get_userbyid(x.relowner) FROM pg_class x "
        "JOIN pg_namespace n ON n.oid=x.relnamespace WHERE x.relkind='S' AND n.nspname='public'"
    ).fetchall()
    for name, owner in seqs:
        if owner != "inventory_admin":
            errs.append(f"Sequenz {name}-owner={owner} (erwartet inventory_admin)")
    # 1:1 Outbox-Backfill.
    nm = c.execute("SELECT count(*) FROM stock_movements").fetchone()[0]
    no = c.execute("SELECT count(*) FROM event_outbox").fetchone()[0]
    if nm != no:
        errs.append(f"Outbox-Backfill unvollstaendig: movements={nm}, outbox={no}")
    pend = c.execute("SELECT count(*) FROM event_outbox WHERE status<>'pending'").fetchone()[0]
    if pend:
        errs.append(f"{pend} Outbox-Zeilen nicht pending")
    mism = c.execute(
        "SELECT count(*) FROM stock_movements m "
        "LEFT JOIN event_outbox o ON o.movement_id=m.id AND o.event_id=m.event_id "
        "WHERE o.event_id IS NULL").fetchone()[0]
    if mism:
        errs.append(f"{mism} Movements ohne passendes (movement_id,event_id)-Event")
if errs:
    print("  FAIL: " + "; ".join(errs), file=sys.stderr); sys.exit(1)
print(f"  VERIFY-DB OK (movements==outbox=={nm}, alle pending, Owner/Rollen korrekt)")
PY

  log "VERIFY: Runtime laeuft als inventory_app + Least-Privilege"
  dc exec -T inventory python - <<'PY' || fail "Verifikation (Runtime-Rechte) fehlgeschlagen"
import os, sys
import psycopg
errs = []
with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as c:
    if c.execute("SELECT current_user").fetchone()[0] != "inventory_app":
        errs.append("current_user != inventory_app")
    if c.execute("SELECT rolsuper FROM pg_roles WHERE rolname='inventory_app'").fetchone()[0]:
        errs.append("inventory_app ist Superuser (!)")
    for stmt, label in [
        ("CREATE TABLE up_evil (i int)", "CREATE"),
        ("UPDATE stock_movements SET quantity=quantity", "UPDATE stock_movements"),
        ("DELETE FROM stock_movements", "DELETE stock_movements"),
        ("SELECT 1 FROM event_outbox", "SELECT event_outbox"),
        ("UPDATE event_outbox SET status='published'", "UPDATE event_outbox"),
        ("DELETE FROM event_outbox", "DELETE event_outbox"),
    ]:
        try:
            c.execute(stmt); errs.append(f"{label} ERLAUBT (!)")
        except psycopg.errors.InsufficientPrivilege:
            pass
        except psycopg.Error as e:
            errs.append(f"{label} unerwartet {type(e).__name__}")
if errs:
    print("  FAIL: " + "; ".join(errs), file=sys.stderr); sys.exit(1)
print("  VERIFY-RUNTIME OK (inventory_app; CREATE/UPDATE/DELETE + event_outbox verweigert)")
PY

  log "VERIFY: echter POST erzeugt atomar Movement + Outbox-Event"
  local newid
  newid="$(dc exec -T inventory python - <<'PY'
import json, sys, urllib.request
req = urllib.request.Request(
    "http://127.0.0.1:8000/movements", method="POST",
    data=json.dumps({"sku": "VERIFY-1", "quantity": 1, "warehouse": "DC"}).encode(),
    headers={"Content-Type": "application/json"})
with urllib.request.urlopen(req, timeout=5) as r:
    if r.status != 201:
        print(f"FAIL http={r.status}", file=sys.stderr); sys.exit(1)
    print(json.load(r)["id"])
PY
)" || fail "Echter POST /movements fehlgeschlagen"
  newid="$(printf '%s' "$newid" | tr -dc '0-9')"
  [ -n "$newid" ] || fail "POST lieferte keine Movement-ID"
  NEWID="$newid" admin_py <<'PY' || fail "Atomaritaets-Check (POST) fehlgeschlagen"
import os, sys
import psycopg
dsn = os.environ["PG_ADMIN_DSN"]; db = os.environ["CHECK_DB"]; nid = int(os.environ["NEWID"])
with psycopg.connect(dsn, dbname=db, autocommit=True) as c:
    row = c.execute(
        "SELECT m.event_id, o.event_id FROM stock_movements m "
        "JOIN event_outbox o ON o.movement_id=m.id WHERE m.id=%s", (nid,)).fetchone()
    nm = c.execute("SELECT count(*) FROM stock_movements").fetchone()[0]
    no = c.execute("SELECT count(*) FROM event_outbox").fetchone()[0]
if row is None:
    print(f"  FAIL: kein Outbox-Event fuer Movement {nid}", file=sys.stderr); sys.exit(1)
if row[0] != row[1]:
    print("  FAIL: event_id von Movement und Outbox unterschiedlich", file=sys.stderr); sys.exit(1)
if nm != no:
    print(f"  FAIL: movements={nm} != outbox={no} nach POST", file=sys.stderr); sys.exit(1)
print(f"  VERIFY-POST OK (Movement {nid} + passendes Outbox-Event, movements==outbox=={nm})")
PY
  log "VERIFY bestanden."
}

# --- Rollback (phasenabhaengig) ----------------------------------------------
rollback() {
  local mode="${1:-}"
  local phase; phase="$(get_state)"
  log "ROLLBACK (aktuelle Phase: ${phase})"

  if migration_started; then
    cat <<EOF

Das Schema ist (teil-)migriert (Phase 2B). Ein automatischer Start der ALTEN
Pre-2B-Runtime ist NICHT erlaubt (sie schreibt kein Outbox-Event und scheitert am
FK). Es gibt nur:

  Option 1 — Forward-Fix: neue, Outbox-faehige Runtime erneut starten
    docker compose -p ${PROJECT} -f ${COMPOSE} --env-file ${ENV_FILE} up -d --no-deps inventory

  Option 2 — Vollstaendiger DB-Restore aus dem Backup, danach alte Runtime
    (manuell bestaetigen; siehe docs/runbook-phase-2b-upgrade-site-dc.md):
    1. Neue Runtime stoppen:
         docker compose -p ${PROJECT} -f ${COMPOSE} --env-file ${ENV_FILE} stop inventory
    2. DB-Verbindungen beenden / db stoppen:
         docker compose -p ${PROJECT} -f ${COMPOSE} --env-file ${ENV_FILE} stop db
    3. Aktuellen (migrierten) Volume-Zustand sichern oder eindeutig verwerfen:
         docker run --rm -v ${VOLUME}:/v -v "\$PWD":/out alpine \\
           tar czf /out/pre-rollback-pgdata.tgz -C /v .
    4. Backup aus ${BACKUP_DIR} wiederherstellen (Volume-Snapshot ODER logischer Dump):
         # Volume-Snapshot:
         docker run --rm -v ${VOLUME}:/v -v ${BACKUP_DIR}:/b:ro alpine \\
           sh -c 'rm -rf /v/* && tar xzf /b/pgdata.tgz -C /v'
         # ODER logischer Dump in eine frische DB (pg_restore), siehe Runbook.
    5. db starten und Owner/Rollen des Alt-Zustands pruefen:
         docker compose -p ${PROJECT} -f ${COMPOSE} --env-file ${ENV_FILE} start db
    6. Alte Runtime aus dem alten Repo starten:
         cd ${OLD_REPO_DIR}/sites/dc && docker compose up -d inventory
    7. Health + echten Schreib-/Lesevorgang gegen die alte App pruefen.
EOF
    [ "$mode" = "--restart-old" ] && fail "Verweigert: --restart-old ist nach Migrationsbeginn unsicher."
    return 0
  fi

  # Vor Migrationsbeginn: alte Runtime ist mit dem (unveraenderten/additiv-neutralen)
  # Zustand kompatibel; sicherer automatischer Rueckstart moeglich.
  cat <<EOF

Vor Migrationsbeginn (Phase: ${phase}). Die alte Runtime ist sicher rueckstartbar
(Bootstrap/Prepare/Reassign sind fuer die als Superuser verbundene Pre-2B-App
neutral). Mit '--restart-old' wird automatisch zurueckgestartet.
EOF
  if [ "$mode" = "--restart-old" ]; then
    log "Sicherer Rueckstart: alte Runtime (re)starten"
    dc start inventory >/dev/null 2>&1 || dc up -d --no-deps inventory || fail "Rueckstart fehlgeschlagen"
    log "Alte Runtime laeuft wieder. Kein Schema wurde inkompatibel veraendert."
  fi
}

usage() {
  cat <<EOF
Usage: RELEASE_DIR=... EXPECTED_COMMIT=... BACKUP_DIR=... $0 <cmd>

  preflight   Nur Pruefungen (Release-Herkunft, Backup, DB-Ist). Keine destruktive Mutation.
  rollout     Vollstaendiger, kontrollierter Upgrade (Zustandsmaschine).
  verify      Verifikation gegen den aktuellen Stand.
  rollback    Phasenabhaengige Rollback-Anleitung; '--restart-old' nur VOR Migrationsbeginn.
  state       Aktuelle Rollout-Phase anzeigen.

  Pflicht-ENV: RELEASE_DIR, EXPECTED_COMMIT (preflight/rollout), BACKUP_DIR (preflight/rollout).
  Optional: BACKUP_DUMP, EXPECTED_BACKUP_SHA256, PROJECT(=${PROJECT}), VOLUME(=${VOLUME}),
            OLD_REPO_DIR(=${OLD_REPO_DIR}), OLD_OWNER_ROLE(=${OLD_OWNER_ROLE}).
EOF
}

main() {
  case "${1:-}" in
    preflight) preflight ;;
    rollout)   rollout ;;
    verify)    verify ;;
    rollback)  shift || true; rollback "${1:-}" ;;
    state)     printf 'phase: %s\n' "$(get_state)" ;;
    -h|--help|help|"") usage ;;
    *) usage; exit 2 ;;
  esac
}

main "$@"
