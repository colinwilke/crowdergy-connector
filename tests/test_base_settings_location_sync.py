"""Saving the base settings must push the location to existing devices.

The device SPEC sends district/city/region only on create. Devices
created before the location was saved stayed without one in the backend
(and therefore out of the Crowdwerk aggregates) although the options
flow showed it.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.theothergas import config_flow
from custom_components.theothergas.const import (
    CONF_CITY,
    CONF_DEVICES,
    CONF_DISTRICT,
    CONF_REGION,
    DOMAIN,
)

_REQUEST = "custom_components.theothergas.config_flow._authenticated_config_request"

DEVICES = [
    {"device_id": "dev-grid", "device_name": "Netz", "device_type": "grid"},
    {"device_id": "dev-pv", "device_name": "Solar", "device_type": "solar"},
]
LOCATION = {
    CONF_DISTRICT: " Angermund ",
    CONF_CITY: "Düsseldorf",
    CONF_REGION: "Nordrhein-Westfalen",
}


def _make_flow(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="user-1", data={CONF_DEVICES: DEVICES},
    )
    entry.add_to_hass(hass)
    flow = config_flow.CrowdergyOptionsFlow(entry)
    flow.hass = hass
    flow.flow_id = "test-flow"
    flow.handler = entry.entry_id
    return flow, entry


async def test_save_pushes_location_to_every_existing_device(
    hass: HomeAssistant,
):
    flow, entry = _make_flow(hass)
    calls: list[tuple[str, str, dict]] = []

    async def fake_request(hass_, entry_, method, path, **kwargs):
        calls.append((method, path, kwargs["json"]))
        return httpx.Response(
            200, json={}, request=httpx.Request(method, "https://api.example/x"),
        )

    with patch(_REQUEST, new=AsyncMock(side_effect=fake_request)):
        result = await flow.async_step_edit_base_settings(dict(LOCATION))

    assert result["type"] == "menu"
    expected = {
        "district": "Angermund",
        "city": "Düsseldorf",
        "region": "Nordrhein-Westfalen",
    }
    assert calls == [
        ("PUT", "/api/v1/devices/dev-grid", expected),
        ("PUT", "/api/v1/devices/dev-pv", expected),
    ]
    assert entry.data[CONF_DISTRICT] == "Angermund"


async def test_backend_failure_keeps_form_open_with_error(hass: HomeAssistant):
    flow, entry = _make_flow(hass)

    with patch(
        _REQUEST, new=AsyncMock(side_effect=httpx.ConnectError("down")),
    ):
        result = await flow.async_step_edit_base_settings(dict(LOCATION))

    assert result["type"] == "form"
    assert result["step_id"] == "edit_base_settings"
    assert result["errors"] == {"base": "cannot_connect"}
    # Saved locally — a retry re-sends it.
    assert entry.data[CONF_CITY] == "Düsseldorf"
