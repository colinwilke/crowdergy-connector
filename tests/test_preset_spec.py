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
    # #68 (2026-06-18): die steuerbaren thermischen Lasten kamen dazu, damit
    # Presets für sie beigetragen werden können (Box-Wizard-Phase-5).
    # haushalt (Residual) + generic (Catch-all) bleiben ohne Spec.
    assert PRESET_CAPABLE_TYPES == {
        "solar", "grid", "battery", "wallbox",
        "heating", "warmwater", "aircon",
    }


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
    # #68: steuerbare thermische Lasten — Pflicht = Leistung + Steuer-Entity
    # (analog wallbox). value_on/off bleiben optional: binäre Steuer-
    # Entities (switch/…) schalten implizit, leeres value_on/off ist gültig.
    for dtype in ("heating", "warmwater", "aircon"):
        assert required[dtype] == {
            "entity_current_power_kw",
            "entity_control",
        }, dtype


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
    # generic hat (bewusst) keine Spec → leere Maps, egal welche Keys da sind.
    entity_map, value_map = extract_preset_maps(
        {"device_type": "generic", "entity_current_power_kw": "sensor.x"}
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


def test_value_slots_cover_control_values():
    """value_on/off (+cool) sind seit #68 heating/aircon zugeordnet und
    bleiben in der box_add_device-Allowlist (auch als generic-Backstop)."""
    assert {"value_on", "value_off", "value_cool_on", "value_cool_off"} <= (
        PRESET_VALUE_SLOTS
    )


# ── #68: steuerbare thermische Lasten (heating/warmwater/aircon) ─────────────

COMPLETE_HEATING_CLIMATE = {
    "device_type": "heating",
    "device_name": "Wärmepumpe",
    "entity_current_power_kw": "sensor.wp_power_consumption",
    # climate-first: Steuer-Entity ist die climate.* Entity, value_on/off
    # tragen die hvac-Modi.
    "entity_control": "climate.wp_heizkreis",
    "value_on": "heat",
    "value_off": "off",
    "entity_current_temp_c": "sensor.wp_ruecklauf_temp",
    "entity_vorlauf_setpoint_c": "number.wp_vorlauf_soll",
    # install-spezifisch / nicht-spec → darf NIE in die Maps:
    "shares_hardware_with_device_id": "dev-9",
    "district": "Altona",
}

COMPLETE_WARMWATER_SWITCH = {
    "device_type": "warmwater",
    "device_name": "Brauchwasser",
    "entity_current_power_kw": "sensor.ww_power",
    # binäre Steuer-Entity → value_on/off bewusst leer (turn_on/turn_off).
    "entity_control": "switch.ww_boost",
}


def test_extract_heating_splits_entity_and_value():
    entity_map, value_map = extract_preset_maps(COMPLETE_HEATING_CLIMATE)
    assert entity_map == {
        "entity_current_power_kw": "sensor.wp_power_consumption",
        "entity_control": "climate.wp_heizkreis",
        "entity_current_temp_c": "sensor.wp_ruecklauf_temp",
        "entity_vorlauf_setpoint_c": "number.wp_vorlauf_soll",
    }
    assert value_map == {"value_on": "heat", "value_off": "off"}
    flat = {**entity_map, **value_map}
    assert "shares_hardware_with_device_id" not in flat
    assert "district" not in flat


def test_missing_required_heating_climate_is_complete():
    assert missing_required_labels(COMPLETE_HEATING_CLIMATE) == []


def test_missing_required_warmwater_switch_is_complete():
    # Binäre Steuer-Entity ohne value_on/off ist vollständig (implizit
    # turn_on/turn_off) — der Contribute-Flow darf das nicht ablehnen.
    assert missing_required_labels(COMPLETE_WARMWATER_SWITCH) == []


def test_missing_required_heating_without_control_lists_it():
    incomplete = dict(COMPLETE_HEATING_CLIMATE)
    incomplete.pop("entity_control")
    missing = missing_required_labels(incomplete)
    assert missing == ["Steuerung (An/Aus)"]


def test_aircon_extract_includes_cool_slots_when_present():
    entity_map, value_map = extract_preset_maps(
        {
            "device_type": "aircon",
            "entity_current_power_kw": "sensor.klima_power",
            "entity_control": "climate.klima",
            "value_on": "cool",
            "value_off": "off",
            "value_cool_on": "cool",
            "value_cool_off": "off",
        }
    )
    assert entity_map == {
        "entity_current_power_kw": "sensor.klima_power",
        "entity_control": "climate.klima",
    }
    assert value_map == {
        "value_on": "cool",
        "value_off": "off",
        "value_cool_on": "cool",
        "value_cool_off": "off",
    }
