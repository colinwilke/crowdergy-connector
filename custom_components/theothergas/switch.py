"""Switch platform for TheOtherGas."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .const import CONF_DEVICE_ID, CONF_DEVICE_NAME, CONF_DEVICES, DOMAIN
from .coordinator import TheOtherGasCoordinator
from .device_registry import get_device_info

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: TheOtherGasCoordinator = hass.data[DOMAIN][entry.entry_id]
    devices = entry.data.get(CONF_DEVICES, [])

    async_add_entities(
        TheOtherGasActiveSwitch(coordinator, dev) for dev in devices
    )


class TheOtherGasActiveSwitch(
    CoordinatorEntity[TheOtherGasCoordinator], SwitchEntity
):
    _attr_has_entity_name = True
    _attr_name = "Active"
    _attr_icon = "mdi:power"

    def __init__(
        self,
        coordinator: TheOtherGasCoordinator,
        device: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device[CONF_DEVICE_ID]
        self._attr_unique_id = f"{self._device_id}_is_active"
        self._attr_device_info = get_device_info(device)
        device_slug = slugify(device.get(CONF_DEVICE_NAME, "device"))
        self._attr_suggested_object_id = f"crowdergy_{device_slug}_active"

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        device_data = self.coordinator.data.get(self._device_id, {})
        return device_data.get("is_active", False)

    async def async_turn_on(self, **kwargs: Any) -> None:
        success = await self.coordinator.async_send_command(
            self._device_id, "set_active", True
        )
        if success and self.coordinator.data and self._device_id in self.coordinator.data:
            self.coordinator.data[self._device_id]["is_active"] = True
            self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        success = await self.coordinator.async_send_command(
            self._device_id, "set_active", False
        )
        if success and self.coordinator.data and self._device_id in self.coordinator.data:
            self.coordinator.data[self._device_id]["is_active"] = False
            self.async_write_ha_state()
