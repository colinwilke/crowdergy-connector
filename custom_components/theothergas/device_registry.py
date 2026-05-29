"""Device registry helpers for Crowdergy."""
from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo

from .const import CONF_DEVICE_ID, CONF_DEVICE_NAME, CONF_DEVICE_TYPE, DOMAIN, VERSION

DEVICE_TYPE_MODELS = {
    "solar": "Solar Inverter",
    "battery": "Battery Storage",
    "wallbox": "EV Wallbox",
    "grid": "Grid Connection",
    # 2026-05 split heatpump → heating + warmwater; keep the legacy
    # key so old config entries that still carry type='heatpump'
    # still get a sensible model label.
    "heatpump": "Heat Pump",
    "heating": "Heat Pump (Heating)",
    "warmwater": "Heat Pump (DHW)",
    "generic": "Generic Energy Device",
    "haushalt": "Household Consumption",
}


def get_device_info(device: dict[str, Any]) -> DeviceInfo:
    """Build HA DeviceInfo for a Crowdergy device."""
    device_type = device.get(CONF_DEVICE_TYPE, "generic")
    device_name = device.get(CONF_DEVICE_NAME, "Crowdergy Device")
    device_id = device.get(CONF_DEVICE_ID, "unknown")

    return DeviceInfo(
        identifiers={(DOMAIN, device_id)},
        # "Crowdergy_" prefix on the HA device name so the platform-
        # injected device is visually distinct from the user's
        # original integration device (they'd otherwise both appear
        # as e.g. "Wallbox Garage" in HA's device list, with no easy
        # way to tell which one a given automation/entity belongs to).
        name=f"Crowdergy_{device_name}",
        manufacturer="Crowdergy",
        model=DEVICE_TYPE_MODELS.get(device_type, "Generic Energy Device"),
        sw_version=VERSION,
    )
