"""dominant_integration_domain (Box-Mapping-Umbau, 2026-06-10)."""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_registry,
)
from homeassistant.helpers import entity_registry as er

from custom_components.theothergas.entity_mapper import dominant_integration_domain


def _reg_entry(entity_id: str, config_entry_id: str | None) -> er.RegistryEntry:
    return er.RegistryEntry(
        entity_id=entity_id,
        unique_id=entity_id,
        platform="sensor",
        config_entry_id=config_entry_id,
    )


async def test_majority_domain_wins(hass: HomeAssistant):
    kostal = MockConfigEntry(domain="kostal_plenticore")
    kostal.add_to_hass(hass)
    other = MockConfigEntry(domain="template")
    other.add_to_hass(hass)
    mock_registry(
        hass,
        {
            "sensor.a": _reg_entry("sensor.a", kostal.entry_id),
            "sensor.b": _reg_entry("sensor.b", kostal.entry_id),
            "sensor.c": _reg_entry("sensor.c", other.entry_id),
        },
    )

    domain = dominant_integration_domain(
        hass, ["sensor.a", "sensor.b", "sensor.c", "sensor.unknown"]
    )
    assert domain == "kostal_plenticore"


async def test_no_resolvable_entities_returns_none(hass: HomeAssistant):
    mock_registry(hass, {"sensor.helper": _reg_entry("sensor.helper", None)})
    assert dominant_integration_domain(hass, ["sensor.helper", "sensor.x"]) is None
