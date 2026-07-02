"""#98: entity_charge_mode / entity_cool_control are state-watched.

The per-tick read already forwarded both slots, but the state-change
listener map didn't register them — a manual flip at the HA select
waited up to HEARTBEAT_INTERVAL (30 s) before propagating to the
backend/iOS instead of riding the event-driven refresh.
"""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.theothergas.const import (
    CONF_DEVICE_ID,
    CONF_ENTITY_CHARGE_MODE,
    CONF_ENTITY_CONTROL,
    CONF_ENTITY_COOL_CONTROL,
    CONF_ENTITY_POWER,
    DOMAIN,
)
from custom_components.theothergas.coordinator import CrowdergyCoordinator


def _mapped_entities(hass: HomeAssistant, device: dict) -> set[str]:
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    coord = CrowdergyCoordinator.__new__(CrowdergyCoordinator)
    coord.hass = hass
    coord.entry = entry
    coord.devices = [device]
    coord._entity_to_devices = {}
    coord._build_entity_map()
    return set(coord._entity_to_devices.keys())


async def test_charge_mode_and_cool_control_are_watched(hass: HomeAssistant):
    watched = _mapped_entities(
        hass,
        {
            CONF_DEVICE_ID: "d1",
            CONF_ENTITY_POWER: "sensor.wb_power",
            CONF_ENTITY_CONTROL: "switch.wb",
            CONF_ENTITY_CHARGE_MODE: "select.wb_mode",
            CONF_ENTITY_COOL_CONTROL: "climate.ac_cool",
        },
    )
    assert "select.wb_mode" in watched
    assert "climate.ac_cool" in watched
    # pre-#98 slots keep being watched
    assert {"sensor.wb_power", "switch.wb"} <= watched


async def test_unmapped_slots_are_not_registered(hass: HomeAssistant):
    watched = _mapped_entities(
        hass,
        {CONF_DEVICE_ID: "d1", CONF_ENTITY_POWER: "sensor.p"},
    )
    assert watched == {"sensor.p"}
