"""#40 — Connector wendet den vollen value_map on `device_update` an.

Zwei Ebenen:
* `_merge_preset_value_map` (pure): Default-DENY-Merge der value-Slots in
  einen Device-Record.
* `_apply_preset_from_backend` (Coordinator-Handler, HA-Harness): auf
  `device_update` das Gerät + Preset ziehen, mergen, Entry updaten +
  entkoppelten Reload planen.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.theothergas.const import (
    CONF_DEVICE_ID,
    CONF_DEVICE_TYPE,
    CONF_DEVICES,
    DOMAIN,
)
from custom_components.theothergas.coordinator import CrowdergyCoordinator
from custom_components.theothergas.command_dispatcher import _merge_preset_value_map


# ── Pure: _merge_preset_value_map ────────────────────────────────────────────


def test_merge_applies_only_preset_value_slots_verbatim():
    dev = {
        CONF_DEVICE_ID: "d1",
        CONF_DEVICE_TYPE: "battery",
        "value_battery_mode_active": "OLD",
        "entity_battery_mode": "select.bat_mode",
    }
    value_map = {
        "value_battery_mode_active": "External",   # value-Slot → übernehmen
        "value_battery_mode_passive": "Internal",  # value-Slot → übernehmen
        "entity_current_power_kw": "sensor.x",      # KEIN value-Slot → ignorieren
    }
    merged = _merge_preset_value_map(dev, value_map)
    assert merged is not dev
    assert merged["value_battery_mode_active"] == "External"
    assert merged["value_battery_mode_passive"] == "Internal"
    # Entity-Slot bleibt unberührt (Connector-Owned, Suffix-Match separat).
    assert merged["entity_battery_mode"] == "select.bat_mode"
    assert "entity_current_power_kw" not in merged


def test_merge_noop_returns_same_object():
    dev = {CONF_DEVICE_ID: "d1", "value_battery_mode_active": "External"}
    # gleicher Wert → keine Änderung → selbes Objekt (Caller skippt Reload)
    assert _merge_preset_value_map(dev, {"value_battery_mode_active": "External"}) is dev
    assert _merge_preset_value_map(dev, None) is dev
    assert _merge_preset_value_map(dev, {}) is dev


# ── Handler: _apply_preset_from_backend (HA-Harness) ─────────────────────────


def _coordinator(hass, entry, devices):
    coord = CrowdergyCoordinator.__new__(CrowdergyCoordinator)
    coord.hass = hass
    coord.entry = entry
    coord.devices = devices
    return coord


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


@pytest.mark.asyncio
async def test_apply_preset_from_backend_merges_and_reloads(hass):
    dev = {
        CONF_DEVICE_ID: "d1",
        CONF_DEVICE_TYPE: "battery",
        "value_battery_mode_active": "OLD",
    }
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_DEVICES: [dev]})
    entry.add_to_hass(hass)
    coord = _coordinator(hass, entry, [dev])

    async def fake_request(method, path, **kwargs):
        if path == "/api/v1/devices/d1":
            return _Resp(200, {"vendor": "KOSTAL", "model": "Plenticore", "type": "battery"})
        if path == "/api/v1/crowd-presets/lookup":
            assert kwargs["params"] == {"device_type": "battery", "vendor": "KOSTAL"}
            return _Resp(200, {"presets": [
                {"model": "AndereModell", "value_map": {"value_battery_mode_active": "NOPE"}},
                {"model": "Plenticore", "value_map": {"value_battery_mode_active": "External"}},
            ]})
        return _Resp(404, {})

    coord._authenticated_request = fake_request
    with patch.object(hass.config_entries, "async_reload", new=AsyncMock()) as reload_mock:
        await coord._apply_preset_from_backend("d1")
        await hass.async_block_till_done()

    merged = entry.data[CONF_DEVICES][0]
    assert merged["value_battery_mode_active"] == "External"  # richtiges Modell gematcht
    reload_mock.assert_awaited_once_with(entry.entry_id)


@pytest.mark.asyncio
async def test_apply_preset_no_vendor_is_noop(hass):
    dev = {CONF_DEVICE_ID: "d1", CONF_DEVICE_TYPE: "battery"}
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_DEVICES: [dev]})
    entry.add_to_hass(hass)
    coord = _coordinator(hass, entry, [dev])

    async def fake_request(method, path, **kwargs):
        # Gerät hat kein Profil gewählt → vendor/model NULL.
        return _Resp(200, {"vendor": None, "model": None, "type": "battery"})

    coord._authenticated_request = fake_request
    with patch.object(hass.config_entries, "async_reload", new=AsyncMock()) as reload_mock:
        await coord._apply_preset_from_backend("d1")
        await hass.async_block_till_done()
    reload_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_preset_unknown_device_is_noop(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_DEVICES: []})
    entry.add_to_hass(hass)
    coord = _coordinator(hass, entry, [])
    # _authenticated_request darf gar nicht erst aufgerufen werden.
    coord._authenticated_request = AsyncMock(side_effect=AssertionError("kein Call erwartet"))
    await coord._apply_preset_from_backend("ghost")  # darf nicht werfen
