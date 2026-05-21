"""Sensor platform for Crowdergy."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .const import CONF_DEVICE_ID, CONF_DEVICE_NAME, CONF_DEVICE_TYPE, CONF_DEVICES, DOMAIN
from .coordinator import CrowdergyCoordinator
from .device_registry import get_device_info


@dataclass(frozen=True, kw_only=True)
class CrowdergySensorEntityDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], Any]
    available_fn: Callable[[dict[str, Any]], bool] = lambda _: True


SENSOR_DESCRIPTIONS: list[CrowdergySensorEntityDescription] = [
    CrowdergySensorEntityDescription(
        key="current_power_kw",
        translation_key="current_power_kw",
        # "Crowdergy_" prefix on every entity name so HA's UI clearly
        # separates the platform-injected sensors from the user's
        # original integration entities they're mapped to (otherwise
        # both show up as plain "Current Power" and become impossible
        # to tell apart in dashboards / automations).
        name="Crowdergy_Current Power",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda data: data.get("current_power_kw"),
    ),
    CrowdergySensorEntityDescription(
        key="soc_percent",
        translation_key="soc_percent",
        name="Crowdergy_State of Charge",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda data: data.get("soc_percent"),
        available_fn=lambda dev: dev.get(CONF_DEVICE_TYPE) == "battery",
    ),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: CrowdergyCoordinator = hass.data[DOMAIN][entry.entry_id]
    devices = entry.data.get(CONF_DEVICES, [])

    entities: list[CrowdergySensor] = []
    for dev in devices:
        for description in SENSOR_DESCRIPTIONS:
            if description.available_fn(dev):
                entities.append(
                    CrowdergySensor(coordinator, entry, dev, description)
                )

    async_add_entities(entities)


class CrowdergySensor(
    CoordinatorEntity[CrowdergyCoordinator], SensorEntity
):
    entity_description: CrowdergySensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: CrowdergyCoordinator,
        entry: ConfigEntry,
        device: dict[str, Any],
        description: CrowdergySensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._device_id = device[CONF_DEVICE_ID]
        self._attr_unique_id = f"{self._device_id}_{description.key}"
        self._attr_device_info = get_device_info(device)
        # Prefix entity_ids with "crowdergy_" so users can find them by domain.
        # Only honoured on first registration; existing entities keep their ID.
        device_slug = slugify(device.get(CONF_DEVICE_NAME, "device"))
        self._attr_suggested_object_id = f"crowdergy_{device_slug}_{description.key}"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        device_data = self.coordinator.data.get(self._device_id, {})
        return self.entity_description.value_fn(device_data)
