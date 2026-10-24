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
    CONF_DEVICE_TYPE,
    CONF_ENTITY_CLIMATE,
    CONF_ENTITY_ENERGY_DISCHARGED_TOTAL,
    CONF_ENTITY_ENERGY_TOTAL,
    CONF_ENTITY_POWER,
    CONF_ENTITY_POWER_2,
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


async def test_second_power_and_discharged_counter_are_watched(
    hass: HomeAssistant,
):
    """Both slots are mirrored to the backend — a change must ride the
    event-driven refresh, not wait for the 30-s tick."""
    watched = _mapped_entities(
        hass,
        {
            CONF_DEVICE_ID: "d1",
            CONF_DEVICE_TYPE: "grid",
            CONF_ENTITY_POWER: "sensor.l1",
            CONF_ENTITY_POWER_2: "sensor.l2",
            CONF_ENTITY_ENERGY_TOTAL: "sensor.bezug",
            CONF_ENTITY_ENERGY_DISCHARGED_TOTAL: "sensor.einspeisung",
        },
    )
    assert watched == {
        "sensor.l1", "sensor.l2", "sensor.bezug", "sensor.einspeisung",
    }


async def test_aircon_climate_fallback_is_watched(hass: HomeAssistant):
    """aircon reads its room temperature from the climate entity when no
    dedicated sensor is mapped; other types don't read that slot."""
    aircon = _mapped_entities(
        hass,
        {
            CONF_DEVICE_ID: "d1",
            CONF_DEVICE_TYPE: "aircon",
            CONF_ENTITY_CLIMATE: "climate.ac",
        },
    )
    assert aircon == {"climate.ac"}
    heating = _mapped_entities(
        hass,
        {
            CONF_DEVICE_ID: "d2",
            CONF_DEVICE_TYPE: "heating",
            CONF_ENTITY_CLIMATE: "climate.wp",
        },
    )
    assert heating == set()


async def test_same_entity_in_two_slots_registers_device_once(
    hass: HomeAssistant,
):
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    coord = CrowdergyCoordinator.__new__(CrowdergyCoordinator)
    coord.hass = hass
    coord.entry = entry
    coord.devices = [{
        CONF_DEVICE_ID: "d1",
        CONF_ENTITY_POWER: "sensor.p",
        CONF_ENTITY_POWER_2: "sensor.p",
    }]
    coord._entity_to_devices = {}
    coord._build_entity_map()
    assert coord._entity_to_devices == {"sensor.p": ["d1"]}
