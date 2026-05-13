"""The TheOtherGas integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, PLATFORMS
from .coordinator import TheOtherGasCoordinator

_LOGGER = logging.getLogger(__name__)

type TheOtherGasConfigEntry = ConfigEntry


async def async_setup_entry(hass: HomeAssistant, entry: TheOtherGasConfigEntry) -> bool:
    """Set up TheOtherGas from a config entry."""
    coordinator = TheOtherGasCoordinator(hass, entry)

    await coordinator.async_config_entry_first_refresh()
    coordinator.setup_listeners()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: TheOtherGasConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        coordinator: TheOtherGasCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()

    return unload_ok
