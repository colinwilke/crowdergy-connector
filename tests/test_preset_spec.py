"""Tests für `preset_spec` (Crowd-Preset-Store-Vertrag, 2026-06-12).

Sichert die Vertrags-Invarianten aus docs/crowd-preset-store.md:

* Jeder Entity-Slot im Schema steht in `MAPPABLE_ENTITY_DOMAINS`
  (Default-DENY) — sonst würde `box_add_device` ein Store-Preset beim
  Anwenden ablehnen.
* `split_device_record` trennt portable Entity-/Value-Slots und
  verwirft installationsspezifische Keys (shares_hardware_with,
  Standort, included_in_haushalt, Form-only-Felder).
* Boolean-Flags reisen als String "true"; False/leer = Slot fehlt.
"""
from __future__ import annotations

from custom_components.theothergas.const import (
    CONF_ENTITY_CLIMATE,
    CONF_ENTITY_CONTROL_HOLD,
    CONF_ENTITY_POWER,
    CONF_ENTITY_WATER_HEATER,
    CONF_INVERT_POWER_SIGN,
    CONF_SUPPORTS_COOLING,
    DEVICE_TYPES,
    MAPPABLE_ENTITY_DOMAINS,
)
from custom_components.theothergas.preset_spec import (
    PRESET_SLOT_SPEC,
    split_device_record,
)


def test_spec_covers_every_device_type():
    assert set(PRESET_SLOT_SPEC) == set(DEVICE_TYPES)


def test_power_slot_is_required_everywhere():
    """Box registriert kein Gerät ohne auflösbaren Power-Slot —
    der Pflicht-Slot muss in jedem Typ Pflicht sein."""
    for dtype, spec in PRESET_SLOT_SPEC.items():
        assert CONF_ENTITY_POWER in spec.required_entities, dtype


def test_entity_slots_subset_of_mappable_allowlist():
    """Default-DENY-Invariante: ein Entity-Slot außerhalb von
    MAPPABLE_ENTITY_DOMAINS würde von box_add_device abgelehnt —
    so ein Preset wäre auf der Box unbrauchbar."""
    for dtype, spec in PRESET_SLOT_SPEC.items():
        unknown = spec.entity_slots - set(MAPPABLE_ENTITY_DOMAINS)
        assert not unknown, f"{dtype}: {sorted(unknown)}"


def test_form_only_slots_are_not_in_spec():
    """entity_climate / entity_water_heater kollabieren beim Speichern
    auf entity_control — sie gehören nicht in Presets."""
    for spec in PRESET_SLOT_SPEC.values():
        assert CONF_ENTITY_CLIMATE not in spec.entity_slots
        assert CONF_ENTITY_WATER_HEATER not in spec.entity_slots


def test_entity_and_value_slots_are_disjoint():
    for dtype, spec in PRESET_SLOT_SPEC.items():
        assert not spec.entity_slots & spec.value_slots, dtype


def test_split_battery_record():
    record = {
        "device_type": "battery",
        "device_name": "Keller-Speicher",
        "entity_current_power_kw": "sensor.plenticore_battery_power",
        "entity_soc_percent": "sensor.plenticore_battery_soc",
        "entity_battery_mode": "select.plenticore_battery_charge_usage_mode",
        "value_battery_mode_active": "External",
        "value_battery_mode_passive": "Internal",
        # installationsspezifisch — darf NICHT in den Beitrag:
        "district": "Altstadt",
        "city": "Heidelberg",
        "included_in_haushalt": True,
        "shares_hardware_with_device_id": "abc-123",
    }
    entity_map, value_map = split_device_record("battery", record)
    assert entity_map == {
        "entity_current_power_kw": "sensor.plenticore_battery_power",
        "entity_soc_percent": "sensor.plenticore_battery_soc",
        "entity_battery_mode": "select.plenticore_battery_charge_usage_mode",
    }
    assert value_map == {
        "value_battery_mode_active": "External",
        "value_battery_mode_passive": "Internal",
    }


def test_split_serialises_true_flags_and_drops_false():
    record = {
        "entity_current_power_kw": "sensor.grid_power",
        "invert_power_sign": True,
    }
    entity_map, value_map = split_device_record("grid", record)
    assert value_map == {CONF_INVERT_POWER_SIGN: "true"}

    record["invert_power_sign"] = False
    _, value_map = split_device_record("grid", record)
    assert value_map == {}


def test_split_keeps_control_hold_and_cooling_values():
    record = {
        "entity_current_power_kw": "sensor.wp_power",
        "entity_control": "climate.wp",
        "value_on": "heat",
        "value_off": "off",
        "supports_cooling": True,
        "value_cool_on": "cool",
        "entity_control_hold": "never",
    }
    _, value_map = split_device_record("heating", record)
    assert value_map == {
        "value_on": "heat",
        "value_off": "off",
        CONF_SUPPORTS_COOLING: "true",
        "value_cool_on": "cool",
        CONF_ENTITY_CONTROL_HOLD: "never",
    }


def test_split_drops_empty_and_non_string_values():
    record = {
        "entity_current_power_kw": "sensor.pv",
        "entity_energy_total": "",
        "invert_power_sign": 1,  # kein bool True, kein string
    }
    entity_map, value_map = split_device_record("solar", record)
    assert entity_map == {"entity_current_power_kw": "sensor.pv"}
    assert value_map == {}


def test_unknown_device_type_yields_empty_maps():
    assert split_device_record("toaster", {"entity_current_power_kw": "sensor.x"}) == (
        {},
        {},
    )
