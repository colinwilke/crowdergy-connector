"""Box-Services (Phase 3): box_discover_devices + box_add_device."""
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
    SERVICE_BOX_DISCOVER,
)
from custom_components.theothergas.const import (
    CONF_ACCESS_TOKEN,
    CONF_API_URL,
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    CONF_DEVICE_TYPE,
    CONF_DEVICES,
    CONF_EMAIL,
    CONF_ENTITY_ENERGY_TOTAL,
    CONF_ENTITY_POWER,
    CONF_REFRESH_TOKEN,
    CONF_USER_ID,
    DOMAIN,
)
from custom_components.theothergas.entity_mapper import DeviceGroup, MappingCandidate

ENTRY_DATA = {
    CONF_API_URL: "https://api.example",
    CONF_EMAIL: "",
    CONF_ACCESS_TOKEN: "access-jwt",
    CONF_REFRESH_TOKEN: "refresh-jwt",
    CONF_USER_ID: "user-1",
    CONF_DEVICES: [],
}


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: {}})
    await hass.async_block_till_done()
    entry = MockConfigEntry(domain=DOMAIN, unique_id="user-1", data=dict(ENTRY_DATA))
    entry.add_to_hass(hass)
    return entry


def _solar_group() -> DeviceGroup:
    return DeviceGroup(
        suggested_type="solar",
        suggested_name="Plenticore Solar",
        ha_device_id="dev-1",
        candidates=[
            MappingCandidate(
                entity_id="sensor.plenticore_total_dc_power",
                device_type="solar",
                slot=CONF_ENTITY_POWER,
                confidence=0.95,
            ),
            MappingCandidate(
                entity_id="sensor.plenticore_total_yield",
                device_type="solar",
                slot=CONF_ENTITY_ENERGY_TOTAL,
                confidence=0.9,
            ),
        ],
    )


async def test_discover_returns_serialized_groups(hass: HomeAssistant):
    await _setup(hass)
    with patch(
        "custom_components.theothergas.box_services._discover",
        new=AsyncMock(return_value=[_solar_group()]),
    ):
        response = await hass.services.async_call(
            DOMAIN, SERVICE_BOX_DISCOVER, {}, blocking=True, return_response=True
        )

    devices = response["devices"]
    assert len(devices) == 1
    assert devices[0][CONF_DEVICE_TYPE] == "solar"
    assert devices[0]["confidence"] == pytest.approx(0.925)
    assert devices[0]["entities"] == {
        CONF_ENTITY_POWER: "sensor.plenticore_total_dc_power",
        CONF_ENTITY_ENERGY_TOTAL: "sensor.plenticore_total_yield",
    }
    assert devices[0]["already_added"] is False


async def test_discover_without_entry_raises(hass: HomeAssistant):
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: {}})
    await hass.async_block_till_done()
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN, SERVICE_BOX_DISCOVER, {}, blocking=True, return_response=True
        )


async def test_add_device_registers_and_persists(hass: HomeAssistant):
    entry = await _setup(hass)

    async def fake_request(hass_, entry_, method, path, **kwargs):
        assert (method, path) == ("POST", "/api/v1/devices")
        assert kwargs["json"]["name"] == "Plenticore Solar"
        assert kwargs["json"]["type"] == "solar"
        return httpx.Response(
            200,
            json={"id": "backend-dev-1"},
            request=httpx.Request("POST", "https://api.example/api/v1/devices"),
        )

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

    async def failing_request(hass_, entry_, method, path, **kwargs):
        return httpx.Response(
            500,
            json={},
            request=httpx.Request("POST", "https://api.example/api/v1/devices"),
        )

    with patch(
        "custom_components.theothergas.config_flow._authenticated_config_request",
        new=AsyncMock(side_effect=failing_request),
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
