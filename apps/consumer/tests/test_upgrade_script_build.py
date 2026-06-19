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


def test_only_db_bootstrap_has_build_section():
    # Sanity gegen die Compose-Datei: db-bootstrap ist der einzige Service mit build:.
    compose = COMPOSE.read_text(encoding="utf-8")
    assert "build:" in compose
    assert compose.count("build:") == 1


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
