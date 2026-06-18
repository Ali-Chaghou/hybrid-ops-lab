#!/usr/bin/env bash
#
# smoke-test.sh — reproduzierbarer, ISOLIERTER site-dc-Compose-Smoke.
# Eigener Compose-Projektname + eigenes Volume + frische Zufalls-Passwoerter (nichts
# committet, nichts ausgegeben). Cleanup per trap. Loescht NIE das normale Lab-Volume
# hol-site-dc_pgdata — nur das projekt-eigene Volume des Smoke-Laufs.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE="$HERE/docker-compose.yml"
PROJECT="hol-dc-smoke-$$-${RANDOM}"
ENVFILE="$(mktemp)"
HOST_PORT="${SMOKE_HOST_PORT:-18080}"
BASE="http://127.0.0.1:${HOST_PORT}"

dc() { docker compose -p "$PROJECT" -f "$COMPOSE" --env-file "$ENVFILE" "$@"; }

cleanup() {
  rc=$?
  trap - EXIT
  # Nur das isolierte Projekt entfernen (Container, Netz, projekt-eigenes Volume).
  dc down -v --remove-orphans >/dev/null 2>&1 || true
  rm -f "$ENVFILE"
  [ "$rc" -eq 0 ] && echo "SMOKE PASS" || echo "SMOKE FAIL (rc=$rc)"
  exit "$rc"
}
trap cleanup EXIT

# Robust unter `set -euo pipefail`: feste 48 Bytes direkt aus /dev/urandom (bounded,
# kein SIGPIPE), Base64 in eine Variable, unerwuenschte Zeichen via Bash-Parameter-
# ersetzung entfernen (kein frueh schliessender letzter Pipeline-Konsument), auf
# mindestens 20 alnum pruefen, exakt 20 ausgeben. Werte werden NIE geloggt.
rand() {
  local raw alnum
  raw="$(head -c 48 /dev/urandom | base64)"
  alnum="${raw//[^A-Za-z0-9]/}"
  if [ "${#alnum}" -lt 20 ]; then
    echo "rand: zu wenig alphanumerische Zeichen (${#alnum})" >&2
    return 1
  fi
  printf '%s' "${alnum:0:20}"
}

# Frische, nicht-platzhalter Passwoerter pro Lauf (werden NICHT ausgegeben).
cat > "$ENVFILE" <<EOF
POSTGRES_USER=hol_admin
POSTGRES_PASSWORD=$(rand)
POSTGRES_DB=postgres
INVENTORY_DB=inventory
INVENTORY_ADMIN_PASSWORD=$(rand)
INVENTORY_APP_PASSWORD=$(rand)
INVENTORY_HOST_PORT=${HOST_PORT}
EVENTS_ENABLED=false
SQS_ENDPOINT_URL=
SQS_QUEUE_URL=
AWS_REGION=eu-central-1
EOF

echo "[smoke] build + up (isoliertes Projekt: $PROJECT)"
# Nur inventory + Abhaengigkeitskette (db, bootstrap, prepare, migrate). node_exporter
# (network_mode: host, Port 9100) bewusst NICHT starten -> keine Host-Port-Kollision.
dc up -d --build --wait inventory

# 1. One-Shot-Setup-Services muessen mit Exit 0 beendet sein.
for svc in db-bootstrap db-prepare inventory-migrate; do
  cid="$(dc ps -aq "$svc")"
  code="$(docker inspect -f '{{.State.ExitCode}}' "$cid")"
  [ "$code" = "0" ] || { echo "FAIL $svc exit=$code"; exit 1; }
  echo "OK $svc exit=0"
done

# 2. HTTP-Contract.
assert_code() { [ "$2" = "$1" ] || { echo "FAIL $3: expected $1 got $2"; exit 1; }; echo "OK $3 ($2)"; }
assert_code 200 "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/healthz")" healthz
assert_code 200 "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/readyz")" readyz
assert_code 201 "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/movements" \
  -H 'Content-Type: application/json' -d '{"sku":"DC-001","quantity":7,"warehouse":"DC"}')" post_valid
assert_code 422 "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/movements" \
  -H 'Content-Type: application/json' -d '{"sku":"X","quantity":0,"warehouse":"DC"}')" post_invalid
assert_code 200 "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/movements")" get_list
# Metrics mit kurzer Retry-Schleife (robust gegen Kaltstart unmittelbar nach --wait).
metrics_ok=0
for _ in 1 2 3 4 5; do
  if curl -fsS "$BASE/metrics" | grep -qE '^inventory_movements_created_total '; then
    metrics_ok=1
    break
  fi
  sleep 1
done
if [ "$metrics_ok" = "1" ]; then
  echo "OK metrics"
else
  echo "FAIL metrics"; exit 1
fi

# 3. Runtime laeuft als inventory_app und ist Least-Privilege.
echo "[smoke] Runtime-Rolle + Privilegien pruefen"
dc exec -T inventory python - <<'PY'
import os, sys, psycopg
errs = []
with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as c:
    user = c.execute("SELECT current_user").fetchone()[0]
    if user != "inventory_app":
        errs.append(f"current_user={user} (erwartet inventory_app)")
    for sql, label in [
        ("CREATE TABLE smoke_evil (i int)", "CREATE"),
        ("UPDATE stock_movements SET quantity=quantity", "UPDATE"),
        ("DELETE FROM stock_movements", "DELETE"),
    ]:
        try:
            c.execute(sql); errs.append(f"{label} ALLOWED (!)")
        except psycopg.errors.InsufficientPrivilege:
            pass
        except psycopg.Error as e:
            errs.append(f"{label} unerwarteter Fehler {type(e).__name__}")
if errs:
    print("FAIL privileges:", "; ".join(errs)); sys.exit(1)
print("OK runtime=inventory_app; CREATE/UPDATE/DELETE denied")
PY

# 4. Idempotenz: Setup-Kette erneut ausfuehren (frische One-Shots) -> Exit 0.
echo "[smoke] zweiter Setup-/Migrationslauf (idempotent)"
for svc in db-bootstrap db-prepare inventory-migrate; do
  dc run --rm --no-deps "$svc" >/dev/null
  echo "OK re-run $svc exit=0"
done

echo "[smoke] alle Pruefungen bestanden"
