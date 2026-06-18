"""Event-Contract (Envelope v1): strikte Validierung + kanonischer Fingerprint.

Reines Modul ohne DB-/Netz-/Env-Abhaengigkeit, damit es isoliert testbar ist.
Der Consumer repariert ungueltige Events NICHT und erzeugt KEINE Ersatz-ID.

Envelope v1:
    {
      "event_id":      <kanonische UUID>,
      "event_type":    "inventory.movement.recorded",
      "schema_version": 1,
      "occurred_at":   <RFC3339, timezone-aware>,
      "source":        "inventory-service",
      "payload": { "movement_id": <bigint>, "sku": <str>, "quantity": <int32>, "warehouse": <str> }
    }
"""
from __future__ import annotations

import hashlib
import json
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

# --- feste, gebundene Auspraegungen (Schema-Version 1) -----------------------
EVENT_TYPE = "inventory.movement.recorded"
SOURCE = "inventory-service"
SCHEMA_VERSION = 1

# Lab-Grenzwert: Movement-Events sind winzig (<300 B). 16 KiB begrenzt Missbrauch,
# ohne legitime Nachrichten zu treffen. Begruendung steht in docs/idempotency.md.
MAX_BODY_BYTES = 16 * 1024

_TOP_FIELDS = {"event_id", "event_type", "schema_version", "occurred_at", "source", "payload"}
_PAYLOAD_FIELDS = {"movement_id", "sku", "quantity", "warehouse"}

# Grenzen aus dem bestehenden Source-Modell (inventory MovementIn: max_length=64;
# stock_movements.quantity ist INTEGER -> 32-bit; id ist BIGINT).
_STR_MAX = 64
_INT32_MIN, _INT32_MAX = -(2**31), 2**31 - 1
_INT64_MIN, _INT64_MAX = -(2**63), 2**63 - 1


class EnvelopeError(ValueError):
    """Validierungsfehler mit niedrig-kardinalem `reason` fuer Metriken/Logs."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


@dataclass(frozen=True)
class Envelope:
    event_id: uuid.UUID
    event_type: str
    schema_version: int
    occurred_at: datetime  # immer tz-aware, auf UTC normalisiert
    source: str
    payload: dict
    # bequemer Zugriff auf fachliche Felder:
    movement_id: int
    sku: str
    quantity: int
    warehouse: str


def _raise_on_duplicate_keys(pairs):
    seen: dict = {}
    for key, value in pairs:
        if key in seen:
            raise EnvelopeError("duplicate_key", key)
        seen[key] = value
    return seen


def _is_int(value) -> bool:
    # bool ist Subklasse von int -> explizit ausschliessen.
    return isinstance(value, int) and not isinstance(value, bool)


def validate(raw: bytes) -> Envelope:
    """Validiert rohe Bytes streng und gibt ein Envelope zurueck oder wirft EnvelopeError."""
    if not isinstance(raw, (bytes, bytearray)):
        raw = str(raw).encode("utf-8")
    # 1. Groesse VOR dem Parsen begrenzen.
    if len(raw) > MAX_BODY_BYTES:
        raise EnvelopeError("too_large", f"{len(raw)}>{MAX_BODY_BYTES}")

    # 2. JSON parsen, doppelte Keys ablehnen, NaN/Infinity verbieten.
    try:
        obj = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_raise_on_duplicate_keys,
            parse_constant=lambda c: (_ for _ in ()).throw(EnvelopeError("non_finite", c)),
        )
    except EnvelopeError:
        raise
    except (ValueError, UnicodeDecodeError) as exc:
        raise EnvelopeError("not_json", str(exc)) from None

    if not isinstance(obj, dict):
        raise EnvelopeError("not_object", "top-level")

    # 3. Exakte Top-Level-Felder.
    unknown = set(obj) - _TOP_FIELDS
    if unknown:
        raise EnvelopeError("unknown_field", ",".join(sorted(unknown)))
    missing = _TOP_FIELDS - set(obj)
    if missing:
        raise EnvelopeError("missing_field", ",".join(sorted(missing)))

    # 4. event_id: kanonische UUID (lowercase, hyphenated, ohne Klammern/urn).
    eid_raw = obj["event_id"]
    if not isinstance(eid_raw, str):
        raise EnvelopeError("bad_event_id", "not_string")
    try:
        eid = uuid.UUID(eid_raw)
    except (ValueError, AttributeError):
        raise EnvelopeError("bad_event_id", "not_uuid") from None
    if str(eid) != eid_raw:
        raise EnvelopeError("bad_event_id", "not_canonical")

    # 5. event_type / source exakt.
    if obj["event_type"] != EVENT_TYPE:
        raise EnvelopeError("bad_event_type", str(obj["event_type"]))
    if obj["source"] != SOURCE:
        raise EnvelopeError("bad_source", str(obj["source"]))

    # 6. schema_version exakt Integer 1 (Boolean ausgeschlossen).
    if not _is_int(obj["schema_version"]) or obj["schema_version"] != SCHEMA_VERSION:
        raise EnvelopeError("bad_schema_version", repr(obj["schema_version"]))

    # 7. occurred_at: timezone-aware RFC3339; intern auf UTC normalisieren.
    occ_raw = obj["occurred_at"]
    if not isinstance(occ_raw, str):
        raise EnvelopeError("bad_occurred_at", "not_string")
    try:
        occ = datetime.fromisoformat(occ_raw)
    except ValueError:
        raise EnvelopeError("bad_occurred_at", "not_rfc3339") from None
    if occ.tzinfo is None or occ.utcoffset() is None:
        raise EnvelopeError("bad_occurred_at", "naive")
    occ = occ.astimezone(timezone.utc)

    # 8. payload: Objekt, exakte Felder, typgenau.
    payload = obj["payload"]
    if not isinstance(payload, dict):
        raise EnvelopeError("payload_not_object", "")
    p_unknown = set(payload) - _PAYLOAD_FIELDS
    if p_unknown:
        raise EnvelopeError("unknown_field", "payload:" + ",".join(sorted(p_unknown)))
    p_missing = _PAYLOAD_FIELDS - set(payload)
    if p_missing:
        raise EnvelopeError("missing_field", "payload:" + ",".join(sorted(p_missing)))

    movement_id = payload["movement_id"]
    if not _is_int(movement_id) or not (_INT64_MIN <= movement_id <= _INT64_MAX):
        raise EnvelopeError("bad_movement_id", repr(movement_id))

    quantity = payload["quantity"]
    if not _is_int(quantity) or not (_INT32_MIN <= quantity <= _INT32_MAX):
        # Keine neue fachliche Regel (quantity>0): das Source-Modell erlaubt INTEGER.
        raise EnvelopeError("bad_quantity", repr(quantity))

    # Unicode NFC an der Contract-Grenze, VOR der Laengenpruefung. Visuell/semantisch
    # gleichwertige Darstellungen sollen nicht wegen unterschiedlicher Codepoint-
    # Zusammensetzung verschiedene Fingerprints erzeugen. KEIN case-/whitespace-Eingriff.
    sku = payload["sku"]
    if not isinstance(sku, str):
        raise EnvelopeError("bad_sku", "")
    sku = unicodedata.normalize("NFC", sku)
    if not (1 <= len(sku) <= _STR_MAX):
        raise EnvelopeError("bad_sku", "")
    warehouse = payload["warehouse"]
    if not isinstance(warehouse, str):
        raise EnvelopeError("bad_warehouse", "")
    warehouse = unicodedata.normalize("NFC", warehouse)
    if not (1 <= len(warehouse) <= _STR_MAX):
        raise EnvelopeError("bad_warehouse", "")

    return Envelope(
        event_id=eid,
        event_type=EVENT_TYPE,
        schema_version=SCHEMA_VERSION,
        occurred_at=occ,
        source=SOURCE,
        payload={"movement_id": movement_id, "sku": sku, "quantity": quantity, "warehouse": warehouse},
        movement_id=movement_id,
        sku=sku,
        quantity=quantity,
        warehouse=warehouse,
    )


def fingerprint(env: Envelope) -> str:
    """Stabiler SHA-256 (hex, lowercase) ueber die unveraenderlichen Eventdaten.

    Bezieht NICHT ein: event_id, (spaeter) correlation_id, Transport-/Laufzeit-Metadaten.
    Unabhaengig von Key-Reihenfolge, Whitespace und aequivalenter UTC-Schreibweise.
    Konsistenzpruefung — KEINE Signatur, kein Herkunftsbeweis.
    """
    material = {
        "event_type": env.event_type,
        "schema_version": env.schema_version,
        "occurred_at": env.occurred_at.astimezone(timezone.utc).isoformat(),
        "source": env.source,
        "payload": env.payload,
    }
    canonical = json.dumps(
        material, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
