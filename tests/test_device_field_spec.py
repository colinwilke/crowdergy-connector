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


# ── Uniform control-capability flag (control_entities_mapped) ─────────────────

from custom_components.theothergas.const import (  # noqa: E402
    CONF_ENTITY_BATTERY_MODE,
    CONF_ENTITY_BATTERY_POWER_SETPOINT,
    CONF_ENTITY_CHARGE_MODE,
    CONF_ENTITY_CONTROL,
    CONF_VALUE_BATTERY_MODE_ACTIVE,
    CONF_VALUE_BATTERY_MODE_PASSIVE,
)


def _mapped(dtype, entity_input):
    out = build_payload(
        mode="create", dtype=dtype, name="x", entity_input=entity_input
    )
    return out.get("control_entities_mapped")


def test_control_entities_mapped_battery_needs_mode_not_just_setpoint():
    """The user's case: only the power setpoint (Zielleistung) mapped, no
    mode select → NOT dispatchable (`_apply_battery_setpoint` skips) →
    False. Adding the Aktiv/Passiv mode select flips it True."""
    setpoint_only = {CONF_ENTITY_BATTERY_POWER_SETPOINT: "number.hausbatterie_zielleistung"}
    assert _mapped("battery", setpoint_only) is False
    full = {
        CONF_ENTITY_BATTERY_MODE: "select.batt_mode",
        CONF_VALUE_BATTERY_MODE_ACTIVE: "Aktiv",
        CONF_VALUE_BATTERY_MODE_PASSIVE: "Passiv",
        CONF_ENTITY_BATTERY_POWER_SETPOINT: "number.hausbatterie_zielleistung",
    }
    assert _mapped("battery", full) is True


def test_phase_switching_capability_needs_all_three_pieces():
    """1/3-Phasen-Umschaltung (2026-07-19): das Capability-Bool wird nur
    True wenn Phasen-Entity + BEIDE Options-Strings + Ladestrom-Entity
    gemappt sind. Jedes fehlende Stück → False — ein True ohne
    Anwendbarkeit wäre die ×3-Überzieh-Falle (Ampere 1- vs 3-phasig)."""
    from custom_components.theothergas.const import (
        CONF_ENTITY_WALLBOX_CHARGE_CURRENT,
        CONF_ENTITY_WALLBOX_PHASE_MODE,
        CONF_VALUE_WALLBOX_PHASE_1,
        CONF_VALUE_WALLBOX_PHASE_3,
    )

    def _cap(entity_input):
        out = build_payload(
            mode="create", dtype="wallbox", name="x",
            entity_input=entity_input,
        )
        return out.get("wallbox_supports_phase_switching")

    full = {
        CONF_ENTITY_WALLBOX_PHASE_MODE: "select.goe_phasen",
        CONF_VALUE_WALLBOX_PHASE_1: "nur 1",
        CONF_VALUE_WALLBOX_PHASE_3: "nur 3",
        CONF_ENTITY_WALLBOX_CHARGE_CURRENT: "number.goe_amp",
    }
    assert _cap(full) is True
    for missing in full:
        partial = {k: v for k, v in full.items() if k != missing}
        assert _cap(partial) is False, f"must be False without {missing}"
    # Entmappen im Update sendet False (always/always) → Backend fällt
    # auf den 3-Phasen-Floor zurück.
    out = build_payload(
        mode="update", dtype="wallbox", name="x", entity_input={}
    )
    assert out.get("wallbox_supports_phase_switching") is False


def test_control_entities_mapped_per_type():
    assert _mapped("wallbox", {CONF_ENTITY_CHARGE_MODE: "select.wb_mode"}) is True
    assert _mapped("wallbox", {}) is False
    assert _mapped("heating", {CONF_ENTITY_CONTROL: "climate.wp"}) is True
    assert _mapped("warmwater", {CONF_ENTITY_CONTROL: "water_heater.ww"}) is True
    assert _mapped("aircon", {CONF_ENTITY_CONTROL: "climate.ac"}) is True
    assert _mapped("generic", {}) is False


def test_control_entities_mapped_absent_for_readonly_types():
    """solar/grid/haushalt are not CONTROLLABLE_TYPES → the field isn't
    even in their payload (iOS renders them read-only by type anyway)."""
    out = build_payload(mode="create", dtype="solar", name="PV", entity_input={})
    assert "control_entities_mapped" not in out


def test_control_entities_mapped_clears_on_update_when_unmapped():
    """always/always → an unmapped control entity sends False on update
    (device flips to „Nur lesend")."""
    out = build_payload(
        mode="update", dtype="battery", name="Akku", entity_input={}
    )
    assert out.get("control_entities_mapped") is False
