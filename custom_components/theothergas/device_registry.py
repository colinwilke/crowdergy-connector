"""Device registry helpers for TheOtherGas."""
from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo

from .const import CONF_DEVICE_ID, CONF_DEVICE_NAME, CONF_DEVICE_TYPE, DOMAIN

DEVICE_TYPE_MODELS = {
    "solar": "Solar Inverter",
    "battery": "Battery Storage",
    "wallbox": "EV Wallbox",
    "grid": "Grid Connection",
    "heatpump": "Heat Pump",
    "generic": "Generic Energy Device",
}


def get_device_info(device: dict[str, Any]) -> DeviceInfo:
    """Build HA DeviceInfo for a TheOtherGas device."""
    device_type = device.get(CONF_DEVICE_TYPE, "generic")
    device_name = device.get(CONF_DEVICE_NAME, "Crowdergy Device")
    device_id = device.get(CONF_DEVICE_ID, "unknown")

    return DeviceInfo(
        identifiers={(DOMAIN, device_id)},
        name=device_name,
        manufacturer="Crowdergy",
        model=DEVICE_TYPE_MODELS.get(device_type, "Generic Energy Device"),
        sw_version="1.0.0",
    )
