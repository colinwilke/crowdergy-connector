"""Sensor platform for TheOtherGas."""
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

from .const import CONF_DEVICE_ID, CONF_DEVICE_TYPE, CONF_DEVICES, DOMAIN
from .coordinator import TheOtherGasCoordinator
from .device_registry import get_device_info


@dataclass(frozen=True, kw_only=True)
class TheOtherGasSensorEntityDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], Any]
    available_fn: Callable[[dict[str, Any]], bool] = lambda _: True


SENSOR_DESCRIPTIONS: list[TheOtherGasSensorEntityDescription] = [
    TheOtherGasSensorEntityDescription(
        key="current_power_kw",
        translation_key="current_power_kw",
        name="Current Power",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda data: data.get("current_power_kw"),
    ),
    TheOtherGasSensorEntityDescription(
        key="soc_percent",
        translation_key="soc_percent",
        name="State of Charge",
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
    coordinator: TheOtherGasCoordinator = hass.data[DOMAIN][entry.entry_id]
    devices = entry.data.get(CONF_DEVICES, [])

    entities: list[TheOtherGasSensor] = []
    for dev in devices:
        for description in SENSOR_DESCRIPTIONS:
            if description.available_fn(dev):
                entities.append(
                    TheOtherGasSensor(coordinator, entry, dev, description)
                )

    async_add_entities(entities)


class TheOtherGasSensor(
    CoordinatorEntity[TheOtherGasCoordinator], SensorEntity
):
    entity_description: TheOtherGasSensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: TheOtherGasCoordinator,
        entry: ConfigEntry,
        device: dict[str, Any],
        description: TheOtherGasSensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._device_id = device[CONF_DEVICE_ID]
        self._attr_unique_id = f"{self._device_id}_{description.key}"
        self._attr_device_info = get_device_info(device)

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        device_data = self.coordinator.data.get(self._device_id, {})
        return self.entity_description.value_fn(device_data)
