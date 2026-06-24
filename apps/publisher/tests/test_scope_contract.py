"""Statische Scope-/Contract-Guards (kein laufendes System)."""
from __future__ import annotations

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[3]
PUB_APP = REPO / "apps/publisher/app"
INV_MAIN = REPO / "apps/inventory/app/main.py"


def _pub_sources() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in PUB_APP.glob("*.py")}


def test_publisher_does_not_import_inventory_or_consumer():
    for name, src in _pub_sources().items():
        assert "apps.inventory" not in src and "apps.consumer" not in src, name
        # Keine Service-uebergreifenden Importe (eigener app-Namespace ist ok).
        assert not re.search(r"\bimport\s+(inventory|consumer)\b", src), name
        assert "from inventory" not in src and "from consumer" not in src, name


def test_publisher_does_not_reference_events_enabled_or_inventory_stub():
    for name, src in _pub_sources().items():
        # EVENTS_ENABLED darf NICHT als Konfig gelesen/genutzt werden (eigener
        # Flag PUBLISHER_ENABLED); eine erklaerende Erwaehnung im Docstring ist ok.
        assert 'getenv("EVENTS_ENABLED")' not in src and 'EVENTS_ENABLED"]' not in src, name
        assert "app.events" not in src and "publish_movement" not in src, name


def test_inventory_request_path_has_no_send_message():
    src = INV_MAIN.read_text(encoding="utf-8")
    assert "send_message" not in src
    assert "publish_movement" not in src


def test_publisher_uses_single_send_message_not_batch():
    src = (PUB_APP / "publisher.py").read_text(encoding="utf-8")
    assert "send_message" in src
    assert "send_message_batch" not in src  # Gate D3A: nur einzelnes SendMessage


def test_contract_constants_are_the_canonical_envelope():
    from app.envelope import EVENT_TYPE, SCHEMA_VERSION, SOURCE

    assert EVENT_TYPE == "inventory.movement.recorded"
    assert SOURCE == "inventory-service"
    assert SCHEMA_VERSION == 1  # keine neue parallele Event-Version


def test_publisher_disabled_default_in_config():
    src = (PUB_APP / "config.py").read_text(encoding="utf-8")
    # Default false (zweites Argument von _bool) fuer PUBLISHER_ENABLED.
    assert re.search(r'PUBLISHER_ENABLED.*\),\s*False\)', src) or 'enabled=_bool(os.getenv("PUBLISHER_ENABLED"), False)' in src
