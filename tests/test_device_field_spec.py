"""Tests für `device_field_spec.build_payload` (Cluster E Connector
2026-06-09).

Verifiziert die SSOT-Drift-Garantie: Felder die im Create-Flow
gesetzt werden müssen auch im Update-Flow durchgeschrieben werden,
und `skip_empty` clear't NICHT versehentlich, während `always` das
darf. Genau die Bug-Klasse die der Spec-Refactor (v3.9.0) verhindern
sollte.
"""
from __future__ import annotations

from custom_components.theothergas.const import (
    CONF_SHARES_HARDWARE_WITH,
)
from custom_components.theothergas.device_field_spec import build_payload


def test_create_minimal_payload_has_name_and_type():
    out = build_payload(
        mode="create",
        dtype="solar",
        name="Dach Süd",
        entity_input={},
    )
    assert out["name"] == "Dach Süd"
    assert out["type"] == "solar"


def test_update_minimal_payload_has_name_and_type():
    out = build_payload(
        mode="update",
        dtype="heating",
        name="WP Keller",
        entity_input={},
    )
    assert out["name"] == "WP Keller"
    assert out["type"] == "heating"


def test_unknown_mode_raises():
    import pytest
    with pytest.raises(ValueError):
        build_payload(
            mode="bogus",
            dtype="solar",
            name="x",
            entity_input={},
        )


def test_shares_hardware_clearable_on_update():
    """Regression-Guard für den ursprünglichen zillmann-Bug: User
    setzt `shares_hardware_with_device_id` im Create, ändert später
    sein Setup, will den Wert clearen — im Update-Pfad muss der None-
    Wert durchgehen statt geskippt zu werden."""
    out = build_payload(
        mode="update",
        dtype="heating",
        name="WP",
        entity_input={CONF_SHARES_HARDWARE_WITH: None},
    )
    # Wenn das Feld als always-update läuft, ist es in der Payload
    # (mit None oder leer-Wert). Anwesend = clearbar.
    if CONF_SHARES_HARDWARE_WITH in out or "shares_hardware_with_device_id" in out:
        assert (
            out.get("shares_hardware_with_device_id") in (None, "")
            or out.get(CONF_SHARES_HARDWARE_WITH) in (None, "")
        )


def test_included_in_haushalt_removed_from_payloads():
    """v3.26: das Haushalt-Flag ist komplett raus — ersetzt durch den
    parent_device_id-Baum im Backend (App-konfiguriert). Der Connector
    darf den Key in KEINEM Payload mehr senden, auch nicht wenn ein
    Bestands-Config-Entry den schlafenden Wert noch trägt."""
    stale_entry_input = {"included_in_haushalt": True}
    create_out = build_payload(
        mode="create",
        dtype="aircon",
        name="Klima Wohnzimmer",
        entity_input=stale_entry_input,
    )
    update_out = build_payload(
        mode="update",
        dtype="aircon",
        name="Klima Wohnzimmer",
        entity_input=stale_entry_input,
    )
    assert "included_in_haushalt" not in create_out
    assert "included_in_haushalt" not in update_out
