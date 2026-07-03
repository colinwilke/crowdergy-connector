"""Crowd-Contribute v0.2 (Mapping-Store 2026-06-11): Options-Flow-Steps.

Treibt die Steps als Unit (Flow-Objekt direkt, ohne Flow-Manager) —
geprüft wird die Spec-Anbindung: Kandidaten-Filter über die
preset-fähigen Typen, Vollständigkeits-Gate, entity_map/value_map-
Payload und die Preset-Value-Defaults im Add-Pfad.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.theothergas import config_flow
from custom_components.theothergas.const import (
    CONF_DEVICE_ID,
    CONF_DEVICES,
    DOMAIN,
)

SOLAR_DEV = {
    "device_id": "dev-solar",
    "device_name": "PV Dach",
    "device_type": "solar",
    "entity_current_power_kw": "sensor.pv_sum_power_of_all_pv_dc_inputs",
    "entity_energy_total": "sensor.pv_energy_yield_total",
}

BATTERY_DEV = {
    "device_id": "dev-bat",
    "device_name": "Speicher",
    "device_type": "battery",
    "entity_current_power_kw": "sensor.anlage_battery_power",
    "entity_soc_percent": "sensor.anlage_battery_soc",
    "entity_battery_mode": "select.anlage_battery_mode",
    "value_battery_mode_active": "External",
    "value_battery_mode_passive": "Internal",
    "entity_battery_power_setpoint_w": "number.anlage_battery_setpoint",
    "battery_setpoint_invert_sign": True,
    "entity_control_hold": "always",
}

BATTERY_INCOMPLETE = {
    "device_id": "dev-bat-2",
    "device_name": "Speicher ohne Steuerung",
    "device_type": "battery",
    "entity_current_power_kw": "sensor.zweite_battery_power",
    "entity_soc_percent": "sensor.zweite_battery_soc",
}

HEATING_DEV = {
    "device_id": "dev-heat",
    "device_name": "WP",
    "device_type": "heating",
    "entity_current_power_kw": "sensor.wp_power",
    "entity_control": "climate.wp",
}

# generic ist (wie haushalt) bewusst NICHT preset-fähig — Catch-all ohne
# Slot-Spec. Dient hier als „nicht beitragbarer" Typ.
GENERIC_DEV = {
    "device_id": "dev-generic",
    "device_name": "Sonstiges",
    "device_type": "generic",
    "entity_current_power_kw": "sensor.misc_power",
}


def _make_flow(hass: HomeAssistant, devices: list[dict]):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="user-1",
        data={CONF_DEVICES: devices},
    )
    entry.add_to_hass(hass)
    flow = config_flow.CrowdergyOptionsFlow(entry)
    flow.hass = hass
    # Flow-Manager-Attribute, die async_show_form/async_abort brauchen
    flow.flow_id = "test-flow"
    flow.handler = entry.entry_id
    return flow


def _response(status: int, payload: dict) -> httpx.Response:
    return httpx.Response(
        status,
        json=payload,
        request=httpx.Request("POST", "https://api.example/x"),
    )


async def test_contribute_offers_all_preset_capable_types(hass: HomeAssistant):
    """Solar + Batterie + (seit #68) heating sind preset-fähig und tauchen
    im Picker auf; generic (Catch-all ohne Spec) bleibt draußen."""
    flow = _make_flow(hass, [SOLAR_DEV, BATTERY_DEV, HEATING_DEV, GENERIC_DEV])
    result = await flow.async_step_contribute_preset()
    assert result["type"] == "form"
    schema = result["data_schema"].schema
    selector_cfg = next(iter(schema.values())).config
    values = [o["value"] for o in selector_cfg["options"]]
    assert values == ["dev-solar", "dev-bat", "dev-heat"]


async def test_contribute_without_capable_devices_aborts(hass: HomeAssistant):
    # Nur ein generic-Gerät → kein preset-fähiger Typ → Abbruch.
    flow = _make_flow(hass, [GENERIC_DEV])
    result = await flow.async_step_contribute_preset()
    assert result["type"] == "abort"
    assert result["reason"] == "contribute_no_devices"


async def test_contribute_incomplete_battery_names_missing_slots(
    hass: HomeAssistant,
):
    """Vollständigkeits-Gate: Batterie ohne Dispatch-Steuerung wird vor
    dem Vendor/Model-Formular abgewiesen, mit deutscher Fehlliste."""
    flow = _make_flow(hass, [BATTERY_INCOMPLETE])
    result = await flow.async_step_contribute_preset(
        {CONF_DEVICE_ID: "dev-bat-2"}
    )
    assert result["type"] == "abort"
    assert result["reason"] == "contribute_incomplete"
    missing = result["description_placeholders"]["missing"]
    assert "Betriebsmodus (Select)" in missing
    assert "Leistungs-Sollwert (W, Number)" in missing


async def test_contribute_battery_posts_entity_and_value_map(
    hass: HomeAssistant,
):
    flow = _make_flow(hass, [BATTERY_DEV])
    result = await flow.async_step_contribute_preset(
        {CONF_DEVICE_ID: "dev-bat"}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "contribute_preset_form"

    captured: dict = {}

    async def fake_request(hass_, entry_, method, path, **kwargs):
        assert (method, path) == ("POST", "/api/v1/crowd-presets/contribute")
        captured.update(kwargs["json"])
        return _response(200, {"status": "staged", "contribution_count": 1})

    with patch(
        "custom_components.theothergas.config_flow._authenticated_config_request",
        new=AsyncMock(side_effect=fake_request),
    ):
        result = await flow.async_step_contribute_preset_form(
            {"vendor": "KOSTAL", "model": "Plenticore plus 8.5"}
        )

    assert result["type"] == "abort"
    assert result["reason"] == "contribute_success"
    assert result["description_placeholders"]["status"] == "staged"
    assert captured["device_type"] == "battery"
    assert captured["entity_map"] == {
        "entity_current_power_kw": "sensor.anlage_battery_power",
        "entity_soc_percent": "sensor.anlage_battery_soc",
        "entity_battery_mode": "select.anlage_battery_mode",
        "entity_battery_power_setpoint_w": "number.anlage_battery_setpoint",
    }
    assert captured["value_map"] == {
        "value_battery_mode_active": "External",
        "value_battery_mode_passive": "Internal",
        "battery_setpoint_invert_sign": "true",
        "entity_control_hold": "always",
    }
    # Install-Spezifisches bleibt draußen (Allowlist-Anonymisierung)
    assert "device_name" not in captured["entity_map"]


async def test_contribute_solar_payload_has_no_value_map(hass: HomeAssistant):
    """Solar-Regression: Payload wie v0.1 (entity_map only) — ohne
    gesetzte Werte/Flags wird kein value_map-Feld angehängt."""
    flow = _make_flow(hass, [SOLAR_DEV])
    await flow.async_step_contribute_preset({CONF_DEVICE_ID: "dev-solar"})
    captured: dict = {}

    async def fake_request(hass_, entry_, method, path, **kwargs):
        captured.update(kwargs["json"])
        return _response(200, {"status": "approved", "contribution_count": 4})

    with patch(
        "custom_components.theothergas.config_flow._authenticated_config_request",
        new=AsyncMock(side_effect=fake_request),
    ):
        result = await flow.async_step_contribute_preset_form(
            {"vendor": "KOSTAL", "model": "Plenticore plus 8.5"}
        )

    assert result["reason"] == "contribute_success"
    assert "value_map" not in captured
    assert captured["entity_map"] == {
        "entity_current_power_kw": "sensor.pv_sum_power_of_all_pv_dc_inputs",
        "entity_energy_total": "sensor.pv_energy_yield_total",
    }


async def test_add_preset_pick_stashes_value_map_and_opens_battery_step(
    hass: HomeAssistant,
):
    """Options-Add: Preset-Wahl übernimmt value_map als Step-Defaults,
    und ein Battery-Preset mit Dispatch-Slots öffnet den Battery-
    Werte-Step auch ohne gesetztes Lademodus-Select."""
    flow = _make_flow(hass, [])
    flow._pending_type = "battery"
    flow._pending_name = "Speicher"
    flow._pending_lookup_cache = [
        {
            "vendor": "KOSTAL",
            "model": "Plenticore plus 8.5",
            "entity_map": {
                "entity_current_power_kw": "sensor.x_battery_power",
                "entity_battery_mode": "select.x_battery_mode",
            },
            "value_map": {
                "value_battery_mode_active": "External",
                "value_battery_mode_passive": "Internal",
            },
        }
    ]

    result = await flow.async_step_add_vendor_preset_pick(
        {"preset_choice": "KOSTAL::Plenticore plus 8.5"}
    )
    assert result["step_id"] == "add_device_entities"
    assert flow._pending_preset_value_map == {
        "value_battery_mode_active": "External",
        "value_battery_mode_passive": "Internal",
    }

    # Entities-Step ohne Lademodus-Select submitted → Battery-Werte-Step
    # öffnet trotzdem (Preset schlägt die Dispatch-Steuerung vor).
    result = await flow._dispatch_add_post_entities(
        {"entity_current_power_kw": "sensor.eigene_battery_power"}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "add_device_battery_values"


async def test_pick_schema_marks_staged_presets(hass: HomeAssistant):
    schema = config_flow._vendor_preset_pick_schema(
        [
            {"vendor": "A", "model": "M1", "status": "approved",
             "contribution_count": 5},
            {"vendor": "B", "model": "M2", "status": "staged",
             "contribution_count": 1},
            {"vendor": "C", "model": "M3", "contribution_count": 2},
        ]
    )
    selector_cfg = next(iter(schema.schema.values())).config
    labels = {o["value"]: o["label"] for o in selector_cfg["options"]}
    assert "unbestätigt" not in labels["A::M1"]
    assert "unbestätigt" in labels["B::M2"]
    # fehlender status (Alt-Backend) = approved-Verhalten
    assert "unbestätigt" not in labels["C::M3"]


async def test_contribute_payload_carries_required_integrations(
    hass: HomeAssistant,
):
    """Der Contribute-Payload trägt die distinkten Integrationen des
    Mappings, damit das Backend sie speichert + neuen Usern anzeigt."""
    flow = _make_flow(hass, [BATTERY_DEV])
    await flow.async_step_contribute_preset({CONF_DEVICE_ID: "dev-bat"})
    captured: dict = {}

    async def fake_request(hass_, entry_, method, path, **kwargs):
        captured.update(kwargs["json"])
        return _response(200, {"status": "staged", "contribution_count": 1})

    with patch(
        "custom_components.theothergas.entity_mapper.required_integration_domains",
        return_value=["kostal_plenticore"],
    ), patch(
        "custom_components.theothergas.config_flow._authenticated_config_request",
        new=AsyncMock(side_effect=fake_request),
    ):
        result = await flow.async_step_contribute_preset_form(
            {"vendor": "KOSTAL", "model": "Plenticore plus 8.5"}
        )

    assert result["reason"] == "contribute_success"
    assert captured["required_integrations"] == ["kostal_plenticore"]


def test_pick_schema_labels_required_integrations(hass: HomeAssistant):
    """Der Profil-Picker zeigt pro Preset die benötigte(n) Integration(en)
    als Klarname (Slug-Fallback)."""
    schema = config_flow._vendor_preset_pick_schema(
        [
            # required_integrations (neu) → Klarnamen, dedupliziert
            {"vendor": "A", "model": "M1", "status": "approved",
             "contribution_count": 5,
             "required_integrations": ["kostal_plenticore", "go_e"]},
            # nur integration_domain (Alt-Backend) → Fallback auf Einzelwert
            {"vendor": "B", "model": "M2", "status": "approved",
             "contribution_count": 2, "integration_domain": "tibber"},
            # unbekannte Domain → aufgehübschter Slug
            {"vendor": "C", "model": "M3", "status": "approved",
             "contribution_count": 1,
             "required_integrations": ["my_inverter"]},
            # keine Integration → kein „benötigt"-Suffix
            {"vendor": "D", "model": "M4", "status": "approved",
             "contribution_count": 1},
        ]
    )
    selector_cfg = next(iter(schema.schema.values())).config
    labels = {o["value"]: o["label"] for o in selector_cfg["options"]}
    assert "benötigt: Kostal Plenticore, go-e" in labels["A::M1"]
    assert "benötigt: Tibber" in labels["B::M2"]
    assert "benötigt: My Inverter" in labels["C::M3"]
    assert "benötigt" not in labels["D::M4"]


def test_integration_display_name_known_and_fallback():
    from custom_components.theothergas.entity_mapper import (
        integration_display_name,
    )

    assert integration_display_name("kostal_plenticore") == "Kostal Plenticore"
    assert integration_display_name("go_e") == "go-e"
    # unbekannte Domain → Slug aufgehübscht
    assert integration_display_name("acme_meter_x") == "Acme Meter X"
    assert integration_display_name("") == ""
