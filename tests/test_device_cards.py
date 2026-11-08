"""Hub-device model: the live hub device must not be deletable via a
card-delete, and deleting any other card never touches the backend.
"""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.theothergas import async_remove_config_entry_device
from custom_components.theothergas.const import DOMAIN


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    return entry


async def test_hub_device_deletion_is_refused(hass: HomeAssistant):
    entry = _entry(hass)
    dev_reg = dr.async_get(hass)
    hub = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name="Crowdergy",
    )
    assert await async_remove_config_entry_device(hass, entry, hub) is False


async def test_other_device_card_deletion_is_allowed_without_backend(
    hass: HomeAssistant,
):
    entry = _entry(hass)
    dev_reg = dr.async_get(hass)
    legacy = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "dev-123")},
        name="Crowdergy_Wallbox",
    )
    # No coordinator registered -> if the hook still tried the backend
    # path it would need one; returning True proves it doesn't.
    assert await async_remove_config_entry_device(hass, entry, legacy) is True
