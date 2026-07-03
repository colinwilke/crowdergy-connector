"""Hub-device model (v3.38.0): stale per-device cards must be cleared
safely, and the live hub device must not be deletable via a card-delete.

Regression: pre-v3.38.0 each mapped device had its own `Crowdergy_<Name>`
card, and deleting a card ran a real backend device delete. After the hub
redesign those cards are entity-less leftovers — deleting one silently
deleted the live device it used to represent. Now the card-delete hook is
non-destructive and setup prunes the leftovers automatically.
"""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.theothergas import (
    _prune_legacy_device_cards,
    async_remove_config_entry_device,
)
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


async def test_legacy_device_card_deletion_is_allowed_without_backend(
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


async def test_prune_removes_legacy_cards_keeps_hub(hass: HomeAssistant):
    entry = _entry(hass)
    dev_reg = dr.async_get(hass)
    dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name="Crowdergy",
    )
    for dev_id in ("dev-a", "dev-b"):
        dev_reg.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, dev_id)},
            name=f"Crowdergy_{dev_id}",
        )

    _prune_legacy_device_cards(hass, entry)

    idents = {
        ident
        for device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id)
        for ident in device.identifiers
    }
    assert (DOMAIN, entry.entry_id) in idents  # hub kept
    assert (DOMAIN, "dev-a") not in idents  # leftovers pruned
    assert (DOMAIN, "dev-b") not in idents
