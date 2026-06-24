"""Regression: das Phase-2B-Deploy-Skript baut den BUILDBAREN Service und
verifiziert den Image-Inhalt VOR dem Stoppen der alten Runtime.

Hintergrund: nur der Service `db-bootstrap` hat in der Compose-Datei eine
`build:`-Sektion. `dc build inventory` haette mangels build:-Sektion ggf. ein
altes `hol-inventory:dev` stillschweigend wiederverwendet. Diese Tests pruefen die
Skript-QUELLE (kein Docker noetig).
"""
from __future__ import annotations

import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "ops/deploy/upgrade-site-dc.sh"
COMPOSE = REPO_ROOT / "sites/dc/docker-compose.yml"
TEXT = SCRIPT.read_text(encoding="utf-8")


def test_builds_db_bootstrap_service():
    assert "dc build db-bootstrap" in TEXT


def test_does_not_build_inventory_service():
    # Der fehlerhafte Build-Target darf nicht mehr vorkommen.
    assert "dc build inventory" not in TEXT


def test_only_db_bootstrap_builds_the_inventory_image():
    # Das INVENTORY-Image (hol-inventory:dev) wird genau EINMAL gebaut (db-bootstrap);
    # die uebrigen Inventory-Image-Services referenzieren es nur. Der Publisher hat
    # bewusst ein EIGENES Image (hol-publisher:dev) mit eigenem build: — das ist kein
    # zweiter Build des Inventory-Images.
    import pytest
    yaml = pytest.importorskip("yaml")
    services = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]
    inv_builders = [n for n, s in services.items()
                    if isinstance(s, dict) and "build" in s and s.get("image") == "hol-inventory:dev"]
    assert inv_builders == ["db-bootstrap"]
    # Jeder build:-Service baut entweder das Inventory- ODER das Publisher-Image.
    for name, s in services.items():
        if isinstance(s, dict) and "build" in s:
            assert s.get("image") in ("hol-inventory:dev", "hol-publisher:dev"), name


def test_image_verification_runs_before_stop():
    build_pos = TEXT.index("dc build db-bootstrap")
    verify_pos = TEXT.index("verify_image_content", build_pos)  # Aufruf nach dem Build
    stop_pos = TEXT.index("dc stop inventory", build_pos)
    assert build_pos < verify_pos < stop_pos, (build_pos, verify_pos, stop_pos)


def test_verification_covers_required_files():
    # Host- und Image-Pfade fuer die drei Pflichtdateien muessen geprueft werden.
    for image_path in (
        "/app/ops/db/reassign.py",
        "/app/app/main.py",
        "/app/migrations/0003_create_event_outbox.sql",
    ):
        assert image_path in TEXT
    for host_path in (
        "ops/db/reassign.py",
        "apps/inventory/app/main.py",
        "apps/inventory/migrations/0003_create_event_outbox.sql",
    ):
        assert host_path in TEXT


def test_verification_pins_image_id_not_only_tag():
    # Verifikation pinnt auf die unveraenderliche Image-ID, nicht nur das Tag.
    assert "docker image inspect -f '{{.Id}}'" in TEXT
    assert 'docker run --rm --entrypoint python "$IMAGE_ID"' in TEXT


# --- Pre-Migration-Recovery (robuster Rueckstart der alten Runtime) -----------


def test_recovery_uses_docker_start_on_exact_old_container():
    # Recovery startet AUSSCHLIESSLICH den bestehenden alten Container per name.
    assert 'docker start "$cname"' in TEXT
    assert 'cname="${PROJECT}-inventory-1"' in TEXT


def test_recovery_has_no_silent_true_fallback():
    # Kein stilles '|| true' im Recovery-Helfer oder im die()-Pre-Migration-Zweig.
    assert "dc start inventory >/dev/null 2>&1 || true" not in TEXT
    helper = TEXT[TEXT.index("restart_old_runtime() {"):TEXT.index("# Phasenabhaengiger Fehler-Abbruch")]
    die_body = TEXT[TEXT.index("die() {"):TEXT.index("gen_pw() {")]
    assert "|| true" not in helper
    assert "|| true" not in die_body


def test_recovery_never_uses_compose_up():
    # Der Recovery-Helfer darf NIEMALS 'docker compose up' / 'dc up' verwenden.
    helper = TEXT[TEXT.index("restart_old_runtime() {"):TEXT.index("# Phasenabhaengiger Fehler-Abbruch")]
    assert "dc up" not in helper
    assert "compose up" not in helper


def test_recovery_waits_for_healthy():
    helper = TEXT[TEXT.index("restart_old_runtime() {"):TEXT.index("# Phasenabhaengiger Fehler-Abbruch")]
    assert ".State.Health.Status" in helper
    assert "healthy)" in helper


def test_die_uses_shared_recovery_helper_pre_migration():
    # Der automatische Fehler-Handler nutzt denselben Helfer (geteilt).
    assert "restart_old_runtime" in TEXT
    die_body = TEXT[TEXT.index("die() {"):TEXT.index("gen_pw() {")]
    assert "restart_old_runtime" in die_body


def test_post_migration_failure_never_starts_old_runtime():
    # Nach migrate-started: kein Pre-2B-Rueckstart; weder Helfer noch docker start.
    die_body = TEXT[TEXT.index("die() {"):TEXT.index("gen_pw() {")]
    started_branch = die_body[die_body.index("if migration_started; then"):die_body.index("else")]
    assert "restart_old_runtime" not in started_branch
    assert "docker start" not in started_branch
    assert "KEIN automatischer Rueckstart" in started_branch


# --- Phase-2B-Verify: NEWID-Weitergabe an den admin_py-Pruefcontainer ---------
# Regression fuer den Verify-Harness-Bug: der POST-Atomaritaets-Check ruft als
# `NEWID="$newid" admin_py` auf und liest os.environ["NEWID"], aber admin_py gab
# NEWID nicht via `-e` an den Container weiter -> `KeyError: 'NEWID'`. Diese Tests
# pruefen die Skript-QUELLE (kein Docker noetig) und schlagen ohne den Fix fehl.

_ADMIN_PY_BODY = TEXT[TEXT.index("admin_py() {"):TEXT.index("# --- Zustandsmaschine")]


def test_admin_py_forwards_newid_to_check_container():
    # admin_py muss NEWID an den Pruefcontainer durchreichen (Wert-lose Form).
    assert "-e NEWID" in _ADMIN_PY_BODY


def test_post_atomicity_check_sets_newid_for_admin_py():
    # Aufrufkontext: NEWID wird als Env-Prefix vor admin_py gesetzt ...
    assert 'NEWID="$newid" admin_py' in TEXT
    # ... und der Python-Check im Container liest sie aus der Umgebung.
    assert 'os.environ["NEWID"]' in TEXT


def test_newid_passed_as_value_less_passthrough_not_inlined():
    # Pass-Through-Form (`-e NEWID` OHNE Wert): sicher unter `set -u` und der Wert
    # taucht nicht in der Kommandozeile auf. Die Wert-Inline-Form ist unerwuenscht.
    assert "-e NEWID=" not in _ADMIN_PY_BODY
    assert "-e NEWID \\" in _ADMIN_PY_BODY


def test_newid_value_is_never_logged():
    # Der Movement-ID-Wert darf nicht ueber log/echo/printf ausgegeben werden.
    for sink in ("log ", "echo ", "printf "):
        assert f"{sink}$NEWID" not in TEXT
        assert f'{sink}"$NEWID"' not in TEXT
        assert f"{sink}$newid" not in TEXT
        assert f'{sink}"$newid"' not in TEXT
