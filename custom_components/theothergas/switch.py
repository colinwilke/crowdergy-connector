"""Switch platform for Crowdergy."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .const import (
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    CONF_DEVICE_TYPE,
    CONF_DEVICES,
    CONTROLLABLE_TYPES,
    DOMAIN,
)
from .coordinator import CrowdergyCoordinator
from .device_registry import get_hub_device_info

_LOGGER = logging.getLogger(__name__)

# Crowdergize-Switch nur für controllable Typen. CN-13 (2026-06-11):
# SSOT in const.py — die lokale Kopie hier hatte `aircon` nicht,
# Klimaanlagen bekamen deshalb nie einen Crowdergy-AI-Switch.
_CROWDERGIZE_TYPES = CONTROLLABLE_TYPES


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: CrowdergyCoordinator = hass.data[DOMAIN][entry.entry_id]
    devices = entry.data.get(CONF_DEVICES, [])

    hub_device_info = get_hub_device_info(entry)
    async_add_entities(
        CrowdergyActiveSwitch(coordinator, dev, hub_device_info)
        for dev in devices
        if dev.get(CONF_DEVICE_TYPE, "") in _CROWDERGIZE_TYPES
    )


class CrowdergyActiveSwitch(
    CoordinatorEntity[CrowdergyCoordinator], SwitchEntity
):
    """HA-side mirror of the per-device Crowdergize consent flag.

    Toggling this switch in HA POSTs `toggle_active` to the backend (and
    optimistically updates the local state). When the same flag flips
    from the iOS app, the backend emits an SSE telemetry mirror frame —
    the coordinator picks that up and re-renders this entity. The two
    surfaces (HA switch ↔ iOS toggle) are kept in sync via the backend.
    """

    # has_entity_name is False: all switches share the one "Crowdergy" hub
    # device, so the device name can't distinguish them — each switch
    # carries the user's device name explicitly in its own friendly name
    # (e.g. "Crowdergy AI: Wallbox Garage"). User-facing UI uses "Crowdergy
    # AI" as the brand; code/API/DB stay on the internal Crowdergize naming
    # — see project_crowdergy_ai_branding memory.
    _attr_has_entity_name = False
    _attr_icon = "mdi:transmission-tower"

    def __init__(
        self,
        coordinator: CrowdergyCoordinator,
        device: dict[str, Any],
        device_info: DeviceInfo,
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device[CONF_DEVICE_ID]
        self._attr_unique_id = f"{self._device_id}_is_active"
        # All switches attach to the single integration-wide "Crowdergy"
        # hub device — no per-device duplicate cards in HA's device list.
        self._attr_device_info = device_info
        device_name = device.get(CONF_DEVICE_NAME, "device")
        self._attr_name = f"Crowdergy AI: {device_name}"
        device_slug = slugify(device_name)
        # `suggested_object_id` only seeds NEW entities — existing
        # `switch.crowdergy_xxx_crowdergize` IDs stay as they are (HA
        # entity registry keeps them). Only the displayed name flips.
        self._attr_suggested_object_id = f"crowdergy_{device_slug}_ai"

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        device_data = self.coordinator.data.get(self._device_id, {})
        return device_data.get("is_active", False)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set_active(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set_active(False)

    async def _set_active(self, on: bool) -> None:
        success = await self.coordinator.async_post_command(
            self._device_id,
            {"action": "toggle_active", "is_active": on},
        )
        if success:
            # Keep both the cache and the data dict in sync; the SSE
            # telemetry mirror frame will arrive shortly and re-confirm
            # this value but applying optimistically avoids UI lag.
            self.coordinator.state.active_state[self._device_id] = on
            if self.coordinator.data and self._device_id in self.coordinator.data:
                self.coordinator.data[self._device_id]["is_active"] = on
                self.async_write_ha_state()
