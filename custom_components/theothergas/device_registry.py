"""Device registry helpers for Crowdergy."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN, VERSION


def get_hub_device_info(entry: ConfigEntry) -> DeviceInfo:
    """Single integration-wide "Crowdergy" hub device.

    All per-device "Crowdergy AI" switches and the connectivity
    binary_sensor attach to this one device. Previously the connector
    created a `Crowdergy_<Name>` device per mapped device, which sat as a
    duplicate card next to the user's real integration device (and, since
    the sensor mirror was removed, left empty cards for read-only types
    like solar/grid). Grouping everything under one hub keeps the HA
    device list clean while preserving the per-device switch entities.
    """
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="Crowdergy",
        manufacturer="Crowdergy",
        model="Energy Manager",
        sw_version=VERSION,
    )
