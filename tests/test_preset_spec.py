"""Mapping-Dictionary (preset_spec): Slot-Schema, Extraktion, Gates.

Die Spec ist der Vertrag zwischen Contribute-Flow, box_add_device und
dem Backend-Store (docs/crowd-preset-store.md) — diese Tests pinnen
die User-Entscheidungen vom 2026-06-11 fest.
"""
from __future__ import annotations

from custom_components.theothergas.const import MAPPABLE_ENTITY_DOMAINS
from custom_components.theothergas.preset_spec import (
    PRESET_CAPABLE_TYPES,
    PRESET_SLOT_SPEC,
    PRESET_VALUE_SLOTS,
    extract_preset_maps,
    missing_required_labels,
)

COMPLETE_BATTERY = {
    "device_type": "battery",
    "device_name": "Speicher Keller",
    "entity_current_power_kw": "sensor.anlage_battery_power",
    "entity_soc_percent": "sensor.anlage_battery_soc",
    "entity_battery_mode": "select.anlage_battery_mode",
    "value_battery_mode_active": "External",
    "value_battery_mode_passive": "Internal",
    "entity_battery_power_setpoint_w": "number.anlage_battery_setpoint",
    "entity_energy_total": "sensor.anlage_battery_discharge_total",
    "battery_setpoint_invert_sign": True,
    "invert_power_sign": False,
    "entity_control_hold": "always",
    # Install-spezifisches darf NIE in die Maps (Allowlist-Prinzip):
    "district": "Altona",
    "city": "Hamburg",
    "shares_hardware_with_device_id": "dev-77",
}


def test_capable_types_match_user_concept():
    assert PRESET_CAPABLE_TYPES == {"solar", "grid", "battery", "wallbox"}


def test_every_entity_slot_is_domain_allowlisted():
    """Trap: ein Entity-Slot in der Spec ohne Eintrag in
    MAPPABLE_ENTITY_DOMAINS würde beim box_add_device IMMER abgelehnt
    — Spec und Allowlist müssen synchron bleiben."""
    for dtype, slots in PRESET_SLOT_SPEC.items():
        for slot in slots:
            if slot.kind == "entity":
                assert slot.key in MAPPABLE_ENTITY_DOMAINS, (dtype, slot.key)


def test_value_slots_never_overlap_entity_allowlist():
    """Ein Key darf nicht gleichzeitig Entity- und Wert-Slot sein —
    box_add_device entscheidet die Prüfart über genau diese Mengen."""
    assert not (PRESET_VALUE_SLOTS & set(MAPPABLE_ENTITY_DOMAINS))


def test_required_sets_per_device_type():
    """User-Vorgabe 2026-06-11: Solar kW+kWh; Batterie kW+SoC+komplette
    Dispatch-Steuerung; Wallbox kW+kWh+Lademodus-Select. (kWh bei
    battery/grid bewusst optional — siehe Modul-Doku.)"""
    required = {
        dtype: {s.key for s in slots if s.required}
        for dtype, slots in PRESET_SLOT_SPEC.items()
    }
    assert required["solar"] == {"entity_current_power_kw", "entity_energy_total"}
    assert required["grid"] == {"entity_current_power_kw", "entity_energy_total"}
    assert required["battery"] == {
        "entity_current_power_kw",
        "entity_soc_percent",
        "entity_battery_mode",
        "value_battery_mode_active",
        "value_battery_mode_passive",
        "entity_battery_power_setpoint_w",
    }
    assert required["wallbox"] == {
        "entity_current_power_kw",
        "entity_energy_total",
        "entity_charge_mode",
    }


def test_extract_battery_maps_splits_entity_and_value():
    entity_map, value_map = extract_preset_maps(COMPLETE_BATTERY)
    assert entity_map == {
        "entity_current_power_kw": "sensor.anlage_battery_power",
        "entity_soc_percent": "sensor.anlage_battery_soc",
        "entity_battery_mode": "select.anlage_battery_mode",
        "entity_battery_power_setpoint_w": "number.anlage_battery_setpoint",
        "entity_energy_total": "sensor.anlage_battery_discharge_total",
    }
    assert value_map == {
        "value_battery_mode_active": "External",
        "value_battery_mode_passive": "Internal",
        # Flag gesetzt → String "true"; ungesetztes Flag fehlt komplett
        "battery_setpoint_invert_sign": "true",
        "entity_control_hold": "always",
    }
    # Install-Spezifisches (Ort, Kopplung, Name) bleibt draußen
    flat = {**entity_map, **value_map}
    assert "district" not in flat
    assert "shares_hardware_with_device_id" not in flat
    assert "invert_power_sign" not in flat  # False → nicht serialisiert


def test_extract_unknown_type_yields_empty_maps():
    entity_map, value_map = extract_preset_maps(
        {"device_type": "heating", "entity_current_power_kw": "sensor.x"}
    )
    assert entity_map == {} and value_map == {}


def test_missing_required_complete_battery_is_empty():
    assert missing_required_labels(COMPLETE_BATTERY) == []


def test_missing_required_lists_labels_for_incomplete_battery():
    incomplete = dict(COMPLETE_BATTERY)
    incomplete.pop("entity_battery_mode")
    incomplete["value_battery_mode_active"] = ""
    missing = missing_required_labels(incomplete)
    assert "Betriebsmodus (Select)" in missing
    assert "Modus-Wert „aktiv“" in missing
    assert len(missing) == 2


def test_value_slots_cover_future_control_values():
    """value_on/off (+cool) sind heute keinem preset-fähigen Typ
    zugeordnet, bleiben aber in der box_add_device-Allowlist, damit
    ein künftiger heating-/generic-Pfad nicht hier scheitert."""
    assert {"value_on", "value_off", "value_cool_on", "value_cool_off"} <= (
        PRESET_VALUE_SLOTS
    )
