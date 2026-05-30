"""Switch platform for Crowdergy."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .const import CONF_DEVICE_ID, CONF_DEVICE_NAME, CONF_DEVICE_TYPE, CONF_DEVICES, DOMAIN
from .coordinator import CrowdergyCoordinator
from .device_registry import get_device_info

_LOGGER = logging.getLogger(__name__)

# Crowdergize-Switch nur für controllable Typen. Mirrors
# `_CONTROLLABLE_TYPES` in config_flow.py.
_CROWDERGIZE_TYPES = {
    "battery", "wallbox", "heating", "warmwater", "generic"
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: CrowdergyCoordinator = hass.data[DOMAIN][entry.entry_id]
    devices = entry.data.get(CONF_DEVICES, [])

    async_add_entities(
        CrowdergyActiveSwitch(coordinator, dev)
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

    _attr_has_entity_name = True
    # User-facing UI uses "Crowdergy AI" as the brand for the per-device
    # consent toggle. Code/API/DB stay on the internal Crowdergize naming
    # — see project_crowdergy_ai_branding memory.
    _attr_name = "Crowdergy AI"
    _attr_icon = "mdi:transmission-tower"

    def __init__(
        self,
        coordinator: CrowdergyCoordinator,
        device: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device[CONF_DEVICE_ID]
        self._attr_unique_id = f"{self._device_id}_is_active"
        self._attr_device_info = get_device_info(device)
        device_slug = slugify(device.get(CONF_DEVICE_NAME, "device"))
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
            self.coordinator._active_state[self._device_id] = on
            if self.coordinator.data and self._device_id in self.coordinator.data:
                self.coordinator.data[self._device_id]["is_active"] = on
                self.async_write_ha_state()
