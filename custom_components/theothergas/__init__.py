"""The Crowdergy integration."""
from __future__ import annotations

import logging

import httpx
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_API_URL,
    CONF_DEVICE_ID,
    CONF_DEVICES,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import CrowdergyCoordinator

_LOGGER = logging.getLogger(__name__)

type CrowdergyConfigEntry = ConfigEntry


async def async_setup_entry(hass: HomeAssistant, entry: CrowdergyConfigEntry) -> bool:
    """Set up Crowdergy from a config entry."""
    coordinator = CrowdergyCoordinator(hass, entry)

    await coordinator.async_config_entry_first_refresh()
    coordinator.setup_listeners()
    coordinator.start_ws_listener()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: CrowdergyConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        coordinator: CrowdergyCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()

    return unload_ok


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    config_entry: CrowdergyConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Allow the user to delete a device card directly in the HA UI.

    Deletes the device on the Crowdergy backend, drops it from the config
    entry's devices list, then lets HA finish removing the registry entry.
    Returning True signals HA to proceed with the removal.
    """
    # Pull the Crowdergy device-id from the (DOMAIN, …) identifier tuple.
    crowdergy_device_id: str | None = None
    for domain, identifier in device_entry.identifiers:
        if domain == DOMAIN:
            crowdergy_device_id = identifier
            break

    if crowdergy_device_id is None:
        # No Crowdergy identifier — nothing to talk to the backend about.
        return True

    api_url = config_entry.data.get(CONF_API_URL)
    token = config_entry.data.get(CONF_ACCESS_TOKEN)
    if api_url and token:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.delete(
                    f"{api_url}/api/v1/devices/{crowdergy_device_id}",
                    headers={"Authorization": f"Bearer {token}"},
                )
                # 404 is fine — already gone backend-side, still safe to drop locally.
                if response.status_code not in (200, 204, 404):
                    response.raise_for_status()
        except (httpx.HTTPStatusError, httpx.RequestError) as err:
            _LOGGER.warning(
                "Backend delete for %s returned %s — removing locally anyway",
                crowdergy_device_id, err,
            )

    # Drop the device from our persisted config-entry list so the
    # coordinator stops polling/pushing for it on the next reload.
    devices = [
        d for d in config_entry.data.get(CONF_DEVICES, [])
        if d.get(CONF_DEVICE_ID) != crowdergy_device_id
    ]
    new_data = {**config_entry.data, CONF_DEVICES: devices}
    hass.config_entries.async_update_entry(config_entry, data=new_data)

    # Prune the coordinator's per-device bookkeeping dicts so a
    # long-lived session doesn't accumulate stale keys after each
    # device deletion. Coordinator stays running; reload would also
    # reset them but HA doesn't force one here.
    coordinator: CrowdergyCoordinator | None = (
        hass.data.get(DOMAIN, {}).get(config_entry.entry_id)
    )
    if coordinator is not None:
        coordinator.forget_device(crowdergy_device_id)

    return True
