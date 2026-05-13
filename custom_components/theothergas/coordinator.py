"""DataUpdateCoordinator for Crowdergy Connector."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import httpx
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_API_URL,
    CONF_DEVICE_ID,
    CONF_DEVICES,
    CONF_ENTITY_ACTIVE,
    CONF_ENTITY_POWER,
    CONF_ENTITY_SOC,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

HEARTBEAT_INTERVAL = 60


class TheOtherGasCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Pushes telemetry on entity state changes + periodic heartbeat."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=HEARTBEAT_INTERVAL),
        )
        self.entry = entry
        self.api_url: str = entry.data[CONF_API_URL]
        self._access_token: str = entry.data[CONF_ACCESS_TOKEN]
        self._refresh_token: str = entry.data[CONF_REFRESH_TOKEN]
        self.devices: list[dict[str, Any]] = entry.data.get(CONF_DEVICES, [])
        self._client = httpx.AsyncClient(base_url=self.api_url, timeout=15.0)
        self._unsub_listeners: list[Any] = []
        self._entity_to_devices: dict[str, list[str]] = {}
        self._build_entity_map()

    def _build_entity_map(self) -> None:
        """Map entity_ids to their device_ids for fast lookup on state changes."""
        for dev in self.devices:
            device_id = dev[CONF_DEVICE_ID]
            for key in (CONF_ENTITY_POWER, CONF_ENTITY_SOC, CONF_ENTITY_ACTIVE):
                entity_id = dev.get(key, "")
                if entity_id:
                    self._entity_to_devices.setdefault(entity_id, []).append(device_id)

    def setup_listeners(self) -> None:
        """Subscribe to state changes of all tracked entities."""
        entity_ids = list(self._entity_to_devices.keys())
        if not entity_ids:
            return

        @callback
        def _on_state_change(event: Event) -> None:
            self.hass.async_create_task(self.async_request_refresh())

        self._unsub_listeners.append(
            async_track_state_change_event(self.hass, entity_ids, _on_state_change)
        )

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token}"}

    async def _refresh_access_token(self) -> bool:
        try:
            response = await self._client.post(
                "/api/v1/auth/refresh",
                json={"refresh_token": self._refresh_token},
            )
            if response.status_code == 200:
                tokens = response.json()
                self._access_token = tokens["access_token"]
                self._refresh_token = tokens["refresh_token"]
                new_data = {**self.entry.data}
                new_data[CONF_ACCESS_TOKEN] = self._access_token
                new_data[CONF_REFRESH_TOKEN] = self._refresh_token
                self.hass.config_entries.async_update_entry(self.entry, data=new_data)
                return True
            _LOGGER.warning("Token refresh returned %s", response.status_code)
        except httpx.RequestError as err:
            _LOGGER.error("Token refresh failed: %s", err)
        return False

    async def _authenticated_request(
        self, method: str, path: str, **kwargs: Any
    ) -> httpx.Response:
        response = await self._client.request(
            method, path, headers=self._auth_headers(), **kwargs
        )
        if response.status_code == 401:
            if await self._refresh_access_token():
                response = await self._client.request(
                    method, path, headers=self._auth_headers(), **kwargs
                )
        return response

    async def async_shutdown(self) -> None:
        for unsub in self._unsub_listeners:
            unsub()
        self._unsub_listeners.clear()
        await self._client.aclose()

    def _read_entity_state(self, entity_id: str) -> Any:
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return state.state

    def _read_power_kw(self, entity_id: str) -> float | None:
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        try:
            value = float(state.state)
        except (ValueError, TypeError):
            return None
        unit = state.attributes.get("unit_of_measurement", "").lower()
        if unit == "w":
            return value / 1000.0
        return value

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}

        for dev in self.devices:
            device_id = dev[CONF_DEVICE_ID]
            entity_power = dev.get(CONF_ENTITY_POWER, "")
            entity_soc = dev.get(CONF_ENTITY_SOC, "")
            entity_active = dev.get(CONF_ENTITY_ACTIVE, "")

            current_power = self._read_power_kw(entity_power)
            soc_percent = self._read_entity_state(entity_soc)
            is_active_raw = self._read_entity_state(entity_active)

            if isinstance(is_active_raw, (int, float)):
                is_active = bool(is_active_raw)
            elif isinstance(is_active_raw, str):
                is_active = is_active_raw.lower() in ("on", "true", "1")
            else:
                is_active = True

            payload: dict[str, Any] = {
                "power_kw": current_power if current_power is not None else 0.0,
                "is_online": True,
                "is_active": is_active,
            }
            if soc_percent is not None:
                payload["soc_percent"] = soc_percent

            if device_id:
                try:
                    response = await self._authenticated_request(
                        "PATCH",
                        f"/api/v1/devices/{device_id}/telemetry",
                        json=payload,
                    )
                    response.raise_for_status()
                except httpx.HTTPStatusError as err:
                    _LOGGER.error(
                        "Backend returned %s for device %s: %s",
                        err.response.status_code,
                        device_id,
                        err.response.text,
                    )
                except httpx.RequestError as err:
                    _LOGGER.error("Cannot reach backend for device %s: %s", device_id, err)

            result[device_id] = {
                "current_power_kw": payload["power_kw"],
                "soc_percent": payload.get("soc_percent"),
                "is_active": is_active,
                "is_online": True,
            }

        return result

    async def async_send_command(
        self, device_id: str, command: str, value: Any
    ) -> bool:
        try:
            response = await self._authenticated_request(
                "POST",
                f"/api/v1/devices/{device_id}/commands",
                json={"action": command, "value": value},
            )
            response.raise_for_status()
            return True
        except (httpx.HTTPStatusError, httpx.RequestError) as err:
            _LOGGER.error("Command failed: %s", err)
            return False
