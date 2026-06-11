"""Box-Services (Phase 3 / Mapping-Umbau): box_list_presets + box_add_device."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.theothergas.box_services import (
    SERVICE_BOX_ADD_DEVICE,
    SERVICE_BOX_LIST_PRESETS,
)
from custom_components.theothergas.const import (
    CONF_ACCESS_TOKEN,
    CONF_API_URL,
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    CONF_DEVICE_TYPE,
    CONF_DEVICES,
    CONF_EMAIL,
    CONF_ENTITY_POWER,
    CONF_REFRESH_TOKEN,
    CONF_USER_ID,
    DOMAIN,
)

ENTRY_DATA = {
    CONF_API_URL: "https://api.example",
    CONF_EMAIL: "",
    CONF_ACCESS_TOKEN: "access-jwt",
    CONF_REFRESH_TOKEN: "refresh-jwt",
    CONF_USER_ID: "user-1",
    CONF_DEVICES: [],
}

PRESET = {
    "device_type": "solar",
    "vendor": "KOSTAL",
    "model": "Plenticore plus 8.5",
    "entity_map": {CONF_ENTITY_POWER: "sensor.plenticore_total_dc_power"},
    "helper_yaml": None,
    "contribution_count": 3,
    "integration_domain": "kostal_plenticore",
}


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: {}})
    await hass.async_block_till_done()
    entry = MockConfigEntry(domain=DOMAIN, unique_id="user-1", data=dict(ENTRY_DATA))
    entry.add_to_hass(hass)
    return entry


def _response(status: int, payload: dict) -> httpx.Response:
    return httpx.Response(
        status,
        json=payload,
        request=httpx.Request("GET", "https://api.example/x"),
    )


async def test_list_presets_proxies_lookup(hass: HomeAssistant):
    await _setup(hass)

    async def fake_request(hass_, entry_, method, path, **kwargs):
        assert (method, path) == ("GET", "/api/v1/crowd-presets/lookup")
        assert kwargs["params"] == {CONF_DEVICE_TYPE: "solar", "vendor": "KOSTAL"}
        return _response(200, {"presets": [PRESET]})

    with patch(
        "custom_components.theothergas.config_flow._authenticated_config_request",
        new=AsyncMock(side_effect=fake_request),
    ):
        response = await hass.services.async_call(
            DOMAIN,
            SERVICE_BOX_LIST_PRESETS,
            {CONF_DEVICE_TYPE: "solar", "vendor": "KOSTAL"},
            blocking=True,
            return_response=True,
        )

    assert response["presets"][0]["integration_domain"] == "kostal_plenticore"
    assert response["presets"][0]["entity_map"][CONF_ENTITY_POWER]


async def test_list_presets_backend_error(hass: HomeAssistant):
    await _setup(hass)
    with patch(
        "custom_components.theothergas.config_flow._authenticated_config_request",
        new=AsyncMock(return_value=_response(500, {})),
    ):
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                DOMAIN,
                SERVICE_BOX_LIST_PRESETS,
                {CONF_DEVICE_TYPE: "solar"},
                blocking=True,
                return_response=True,
            )


async def test_list_presets_without_entry_raises(hass: HomeAssistant):
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: {}})
    await hass.async_block_till_done()
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_BOX_LIST_PRESETS,
            {CONF_DEVICE_TYPE: "solar"},
            blocking=True,
            return_response=True,
        )


async def test_add_device_registers_and_persists(hass: HomeAssistant):
    entry = await _setup(hass)

    async def fake_request(hass_, entry_, method, path, **kwargs):
        assert (method, path) == ("POST", "/api/v1/devices")
        assert kwargs["json"]["name"] == "Plenticore Solar"
        assert kwargs["json"]["type"] == "solar"
        return _response(200, {"id": "backend-dev-1"})

    with patch(
        "custom_components.theothergas.config_flow._authenticated_config_request",
        new=AsyncMock(side_effect=fake_request),
    ):
        response = await hass.services.async_call(
            DOMAIN,
            SERVICE_BOX_ADD_DEVICE,
            {
                CONF_DEVICE_TYPE: "solar",
                CONF_DEVICE_NAME: "Plenticore Solar",
                "entities": {CONF_ENTITY_POWER: "sensor.plenticore_total_dc_power"},
            },
            blocking=True,
            return_response=True,
        )

    assert response[CONF_DEVICE_ID] == "backend-dev-1"
    devices = entry.data[CONF_DEVICES]
    assert len(devices) == 1
    assert devices[0][CONF_DEVICE_ID] == "backend-dev-1"
    assert devices[0][CONF_ENTITY_POWER] == "sensor.plenticore_total_dc_power"


async def test_add_device_backend_error_keeps_entry_unchanged(hass: HomeAssistant):
    entry = await _setup(hass)
    with patch(
        "custom_components.theothergas.config_flow._authenticated_config_request",
        new=AsyncMock(return_value=_response(500, {})),
    ):
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                DOMAIN,
                SERVICE_BOX_ADD_DEVICE,
                {
                    CONF_DEVICE_TYPE: "solar",
                    CONF_DEVICE_NAME: "X",
                    "entities": {},
                },
                blocking=True,
                return_response=True,
            )
    assert entry.data[CONF_DEVICES] == []


async def test_add_device_rejects_unknown_type(hass: HomeAssistant):
    await _setup(hass)
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_BOX_ADD_DEVICE,
            {CONF_DEVICE_TYPE: "toaster", CONF_DEVICE_NAME: "X", "entities": {}},
            blocking=True,
        )


# ── Phase 4: Consent + Forbidden Domains ─────────────────────────────────────


async def test_add_device_rejects_forbidden_domain(hass: HomeAssistant):
    entry = await _setup(hass)
    with pytest.raises(HomeAssistantError) as excinfo:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_BOX_ADD_DEVICE,
            {
                CONF_DEVICE_TYPE: "generic",
                CONF_DEVICE_NAME: "Spion",
                "entities": {"entity_control": "camera.wohnzimmer"},
            },
            blocking=True,
            return_response=True,
        )
    assert "forbidden" in str(excinfo.value)
    assert entry.data[CONF_DEVICES] == []


async def test_set_consent_writes_entry_options(hass: HomeAssistant):
    from custom_components.theothergas.box_services import SERVICE_BOX_SET_CONSENT
    from custom_components.theothergas.const import (
        OPT_CONSENT_REMOTE_CONTROL,
        OPT_CONSENT_TELEMETRY,
    )

    entry = await _setup(hass)
    await hass.services.async_call(
        DOMAIN,
        SERVICE_BOX_SET_CONSENT,
        {"telemetry": False, "remote_control": True},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert entry.options[OPT_CONSENT_TELEMETRY] is False
    assert entry.options[OPT_CONSENT_REMOTE_CONTROL] is True
