"""Geräteprofil-Picker für ALLE preset-fähigen Typen.

Regression: heating/warmwater/aircon liefen über den KonfigMode-Step
direkt in den Entity-Step — der Profil-Picker wurde nie erreicht, und
der Werte-Step ignorierte die value_map des Profils. Getrieben als Unit
(Flow-Objekt direkt, ohne Flow-Manager) wie in test_contribute_flow.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.theothergas import config_flow
from custom_components.theothergas.config_flow_presets import (
    _preset_config_mode,
    _preset_step_defaults,
)
from custom_components.theothergas.const import (
    CONF_DEVICES,
    CONFIG_MODE_CLIMATE,
    CONFIG_MODE_MANUAL,
    DOMAIN,
)
from custom_components.theothergas.preset_spec import PRESET_CAPABLE_TYPES


def _options_flow(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="user-1", data={CONF_DEVICES: []})
    entry.add_to_hass(hass)
    flow = config_flow.CrowdergyOptionsFlow(entry)
    flow.hass = hass
    flow.flow_id = "test-flow"
    flow.handler = entry.entry_id
    return flow


def _initial_flow(hass: HomeAssistant):
    flow = config_flow.CrowdergyConfigFlow()
    flow.hass = hass
    flow.flow_id = "test-flow"
    flow.handler = DOMAIN
    flow._data = {"api_url": "https://api.example", "access_token": "tok"}
    return flow


def _preset(entity_map: dict[str, str], value_map: dict[str, str] | None = None):
    return {
        "vendor": "Stiebel Eltron",
        "model": "WWK",
        "entity_map": entity_map,
        "value_map": value_map or {},
    }


def _suggested(schema, key: str) -> Any:
    """suggested_value/default eines Felds, auch in Sections."""
    for marker, val in schema.schema.items():
        if getattr(marker, "schema", None) == key:
            desc = getattr(marker, "description", None) or {}
            if "suggested_value" in desc:
                return desc["suggested_value"]
            default = getattr(marker, "default", None)
            return default() if callable(default) else default
        inner = getattr(val, "schema", None)
        if inner is not None and hasattr(inner, "schema"):
            found = _suggested(inner, key)
            if found is not None:
                return found
    return None


@pytest.mark.parametrize("device_type", sorted(PRESET_CAPABLE_TYPES))
async def test_add_device_offers_picker_for_every_capable_type(
    hass: HomeAssistant, device_type: str
):
    flow = _options_flow(hass)
    with patch.object(
        config_flow, "_fetch_vendor_presets",
        AsyncMock(return_value=[_preset({"entity_current_power_kw": "sensor.x"})]),
    ):
        result = await flow.async_step_add_device(
            {"device_type": device_type, "device_name": "Gerät"}
        )
    assert result["step_id"] == "add_vendor_preset_pick"


@pytest.mark.parametrize("device_type", sorted(PRESET_CAPABLE_TYPES))
async def test_initial_flow_offers_picker_for_every_capable_type(
    hass: HomeAssistant, device_type: str
):
    flow = _initial_flow(hass)
    with patch.object(
        config_flow, "_fetch_vendor_presets",
        AsyncMock(return_value=[_preset({"entity_current_power_kw": "sensor.x"})]),
    ):
        result = await flow.async_step_device_type(
            {"device_type": device_type, "device_name": "Gerät"}
        )
    assert result["step_id"] == "vendor_preset_pick"


async def test_no_presets_falls_back_to_config_mode_step(hass: HomeAssistant):
    flow = _options_flow(hass)
    with patch.object(
        config_flow, "_fetch_vendor_presets", AsyncMock(return_value=[]),
    ):
        result = await flow.async_step_add_device(
            {"device_type": "warmwater", "device_name": "WW"}
        )
    assert result["step_id"] == "add_device_config_mode"


async def test_manual_choice_falls_back_to_config_mode_step(hass: HomeAssistant):
    flow = _initial_flow(hass)
    flow._pending_type = "warmwater"
    flow._pending_lookup_cache = [_preset({"entity_control": "water_heater.ww"})]
    result = await flow.async_step_vendor_preset_pick({"preset_choice": "__manual__"})
    assert result["step_id"] == "device_config_mode"


async def test_water_heater_preset_selects_climate_mode_and_prefills(
    hass: HomeAssistant,
):
    hass.states.async_set("water_heater.ww", "eco")
    hass.states.async_set("sensor.ww_power", "0.1")
    flow = _options_flow(hass)
    flow._pending_type = "warmwater"
    flow._pending_name = "WW"
    flow._pending_lookup_cache = [_preset(
        {
            "entity_current_power_kw": "sensor.ww_power",
            "entity_control": "water_heater.ww",
        },
        {"value_on": "65", "value_off": "40", "entity_control_hold": "always"},
    )]
    result = await flow.async_step_add_vendor_preset_pick(
        {"preset_choice": "Stiebel Eltron::WWK"}
    )
    assert result["step_id"] == "add_device_entities"
    assert flow._pending_config_mode == CONFIG_MODE_CLIMATE
    schema = result["data_schema"]
    assert _suggested(schema, "entity_water_heater") == "water_heater.ww"
    assert _suggested(schema, "entity_current_power_kw") == "sensor.ww_power"

    # Submit → Werte-Step mit den Profil-Werten vorbefüllt.
    flow._pending_entity_input = {
        "entity_current_power_kw": "sensor.ww_power",
        "entity_control": "water_heater.ww",
    }
    result = await flow.async_step_add_device_values()
    schema = result["data_schema"]
    assert _suggested(schema, "value_on") == 65.0
    assert _suggested(schema, "value_off") == 40.0
    assert _suggested(schema, "entity_control_hold") == "always"


async def test_select_preset_selects_manual_mode_and_prefills_values(
    hass: HomeAssistant,
):
    hass.states.async_set(
        "select.ww_betrieb", "Aus", {"options": ["Aus", "Boost"]}
    )
    flow = _initial_flow(hass)
    flow._pending_type = "warmwater"
    flow._pending_name = "WW"
    flow._pending_lookup_cache = [_preset(
        {"entity_control": "select.ww_betrieb"},
        {"value_on": "Boost", "value_off": "Aus"},
    )]
    result = await flow.async_step_vendor_preset_pick(
        {"preset_choice": "Stiebel Eltron::WWK"}
    )
    assert result["step_id"] == "device_entities"
    assert flow._pending_config_mode == CONFIG_MODE_MANUAL
    assert _suggested(result["data_schema"], "entity_control") == "select.ww_betrieb"

    result = await flow._dispatch_post_entities(
        {"entity_control": "select.ww_betrieb"}
    )
    assert result["step_id"] == "device_values"
    schema = result["data_schema"]
    assert _suggested(schema, "value_on") == "Boost"
    assert _suggested(schema, "value_off") == "Aus"


async def test_climate_control_on_warmwater_stays_manual(hass: HomeAssistant):
    """Climate-Modus von warmwater nimmt nur water_heater — ein Profil
    mit climate.*-Steuerung bleibt manuell (entity_control nimmt climate),
    statt über die Legacy-Migration im unsichtbaren Feld zu landen."""
    hass.states.async_set("climate.ww", "heat")
    flow = _options_flow(hass)
    flow._pending_type = "warmwater"
    flow._pending_name = "WW"
    flow._pending_lookup_cache = [_preset({"entity_control": "climate.ww"})]
    result = await flow.async_step_add_vendor_preset_pick(
        {"preset_choice": "Stiebel Eltron::WWK"}
    )
    assert flow._pending_config_mode == CONFIG_MODE_MANUAL
    assert _suggested(result["data_schema"], "entity_control") == "climate.ww"


def test_preset_config_mode_per_type():
    assert _preset_config_mode("heating", {"entity_control": "climate.wp"}) == CONFIG_MODE_CLIMATE
    assert _preset_config_mode("aircon", {"entity_control": "climate.ac"}) == CONFIG_MODE_CLIMATE
    assert _preset_config_mode("warmwater", {"entity_control": "water_heater.w"}) == CONFIG_MODE_CLIMATE
    assert _preset_config_mode("heating", {"entity_control": "select.sg"}) == CONFIG_MODE_MANUAL
    assert _preset_config_mode("heating", {}) == CONFIG_MODE_MANUAL


@pytest.mark.parametrize(("raw", "expected"), [("true", True), ("false", False), ("", False)])
async def test_flag_slots_parse_as_bool(hass: HomeAssistant, raw: str, expected: bool):
    flow = _options_flow(hass)
    flow._pending_preset_entity_map = {}
    flow._pending_preset_value_map = {"invert_power_sign": raw}
    assert _preset_step_defaults(flow)["invert_power_sign"] is expected


async def test_invert_flag_reaches_entities_step(hass: HomeAssistant):
    flow = _options_flow(hass)
    flow._pending_type = "solar"
    flow._pending_name = "PV"
    flow._pending_lookup_cache = [_preset(
        {"entity_current_power_kw": "sensor.pv"}, {"invert_power_sign": "true"},
    )]
    result = await flow.async_step_add_vendor_preset_pick(
        {"preset_choice": "Stiebel Eltron::WWK"}
    )
    assert _suggested(result["data_schema"], "invert_power_sign") is True
