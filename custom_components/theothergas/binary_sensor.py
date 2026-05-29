"""Binary-sensor platform for Crowdergy — exposes SSE liveness.

Single integration-wide entity: `binary_sensor.crowdergy_connected`. ON
while a Crowdergy SSE message (ping or data) has been received within
the last `SSE_STALE_THRESHOLD_S` seconds; OFF otherwise.

Lets users write HA automations that take over when Crowdergy goes
offline — e.g. flip an input_select back to a safe default so the
inverter's native logic resumes rather than holding the last
Crowdergy-commanded mode forever.
"""
from __future__ import annotations

import time

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, SSE_STALE_THRESHOLD_S
from .coordinator import CrowdergyCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: CrowdergyCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([CrowdergyConnectedBinarySensor(coordinator, entry)])


class CrowdergyConnectedBinarySensor(
    CoordinatorEntity[CrowdergyCoordinator], BinarySensorEntity
):
    """ON while Crowdergy SSE has been alive recently, OFF otherwise.

    State is recomputed every time HA polls the entity (immediately on
    coordinator refresh, every ~30 s via the coordinator's heartbeat
    cadence). Detection latency is roughly
    `SSE_STALE_THRESHOLD_S + HEARTBEAT_INTERVAL` end-to-end (≈ 90 s
    with current constants), which is fast enough for the
    "take-over-on-Crowdergy-outage" automation pattern.
    """

    _attr_has_entity_name = True
    _attr_name = "Crowdergy Connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: CrowdergyCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        # One per config entry; the entry_id keeps the unique_id stable
        # across HA restarts even if the user re-creates the entry.
        self._attr_unique_id = f"{entry.entry_id}_crowdergy_connected"

    @property
    def is_on(self) -> bool:
        last = self.coordinator.last_sse_event_at
        if last == 0.0:
            # Never seen an SSE event — boot state. Report OFF so
            # take-over automations don't sit in limbo waiting for a
            # transition that never comes when the backend's
            # unreachable from boot.
            return False
        return (time.time() - last) <= SSE_STALE_THRESHOLD_S
