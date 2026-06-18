"""Unicode NFC an der Contract-Grenze: Aequivalenz, aber Case/Whitespace bleiben relevant.
Reine ASCII-Quelltext-Escapes, damit die Codepoint-Form eindeutig ist."""
from __future__ import annotations

from app.envelope import fingerprint, validate

import helpers

PRE = "caf\u00e9"        # vorkomponiert: e-acute (1 Codepoint)
COMB = "cafe\u0301"       # kombinierend: e + combining acute (2 Codepoints)
UPPER = "CAF\u00c9"       # vorkomponiert: E-acute


def test_nfc_equivalent_forms_same_value_and_fingerprint():
    assert PRE != COMB and len(PRE) == 4 and len(COMB) == 5   # roh verschieden
    pre = validate(helpers.body_with_payload(warehouse=PRE))
    comb = validate(helpers.body_with_payload(warehouse=COMB))
    assert pre.warehouse == comb.warehouse == PRE            # beide -> NFC (vorkomponiert)
    assert fingerprint(pre) == fingerprint(comb)             # gleicher Fingerprint


def test_nfc_applies_to_sku_too():
    a = validate(helpers.body_with_payload(sku=PRE))
    b = validate(helpers.body_with_payload(sku=COMB))
    assert a.sku == b.sku and fingerprint(a) == fingerprint(b)


def test_case_remains_significant():
    lower = validate(helpers.body_with_payload(warehouse=PRE))
    upper = validate(helpers.body_with_payload(warehouse=UPPER))
    assert lower.warehouse != upper.warehouse
    assert fingerprint(lower) != fingerprint(upper)


def test_whitespace_remains_significant():
    plain = validate(helpers.body_with_payload(sku="ABC"))
    spaced = validate(helpers.body_with_payload(sku=" ABC "))
    assert spaced.sku == " ABC "                             # nicht getrimmt
    assert fingerprint(plain) != fingerprint(spaced)


def test_length_checked_after_nfc():
    # 64x kombiniertes e-acute = 128 Codepoints dekomponiert, aber 64 nach NFC -> erlaubt.
    value = "e\u0301" * 64  # 128 Codepoints dekomponiert, 64 nach NFC
    env = validate(helpers.body_with_payload(warehouse=value))
    assert len(env.warehouse) == 64
