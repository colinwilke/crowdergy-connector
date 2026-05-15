"""DataUpdateCoordinator for Crowdergy Connector."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta
from typing import Any

import aiohttp
import httpx
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CHARGE_MODE_OPTIONS,
    CONF_ACCESS_TOKEN,
    CONF_API_URL,
    CONF_DEVICE_ID,
    CONF_DEVICES,
    CONF_ENTITY_ACTIVE,
    CONF_ENTITY_CHARGE_MODE,
    CONF_ENTITY_POWER,
    CONF_ENTITY_SOC,
    CONF_ENTITY_SOC_MAX,
    CONF_ENTITY_SOC_MIN,
    CONF_ENTITY_VEHICLE_STATUS,
    CONF_REFRESH_TOKEN,
    CONF_USER_ID,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

HEARTBEAT_INTERVAL = 30
WS_RECONNECT_INITIAL = 1
WS_RECONNECT_MAX = 60


class TheOtherGasCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Pushes telemetry on entity state changes + periodic heartbeat,
    and listens on a WS channel for commands from the Crowdergy app."""

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
        self._user_id: str = entry.data.get(CONF_USER_ID, "")
        self.devices: list[dict[str, Any]] = entry.data.get(CONF_DEVICES, [])
        self._client = httpx.AsyncClient(base_url=self.api_url, timeout=15.0)
        self._unsub_listeners: list[Any] = []
        self._entity_to_devices: dict[str, list[str]] = {}
        self._ws_task: asyncio.Task | None = None
        self._build_entity_map()

    def _build_entity_map(self) -> None:
        """Map entity_ids to their device_ids for fast lookup on state changes."""
        for dev in self.devices:
            device_id = dev[CONF_DEVICE_ID]
            for key in (
                CONF_ENTITY_POWER,
                CONF_ENTITY_SOC,
                CONF_ENTITY_ACTIVE,
                CONF_ENTITY_SOC_MIN,
                CONF_ENTITY_SOC_MAX,
                CONF_ENTITY_VEHICLE_STATUS,
                CONF_ENTITY_CHARGE_MODE,
            ):
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
            # async_refresh bypasses DataUpdateCoordinator's built-in
            # debouncer so a user-driven HA change propagates immediately
            # (sub-second) to the backend / iOS, instead of waiting up to
            # the next 30 s heartbeat.
            self.hass.async_create_task(self.async_refresh())

        self._unsub_listeners.append(
            async_track_state_change_event(self.hass, entity_ids, _on_state_change)
        )

    def start_ws_listener(self) -> None:
        """Open a WS connection to the backend and react to inbound command frames."""
        if not self._user_id:
            _LOGGER.warning("No user_id stored — skipping WS listener setup")
            return
        if self._ws_task and not self._ws_task.done():
            return
        self._ws_task = self.hass.async_create_background_task(
            self._run_ws_loop(),
            name=f"{DOMAIN}_ws_listener",
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
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
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

    def _read_number(self, entity_id: str) -> float | None:
        """Read a number-entity state as float, ignoring unknown/unavailable."""
        value = self._read_entity_state(entity_id)
        return value if isinstance(value, (int, float)) else None

    def _read_string(self, entity_id: str) -> str | None:
        """Read an entity state as a plain string (incl. friendly_name fallback)."""
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        # Prefer the friendly representation if HA exposes one (sensor entities
        # often carry a `friendly_value` or use the raw `state`).
        text = str(state.state)
        return text if text else None

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}

        for dev in self.devices:
            device_id = dev[CONF_DEVICE_ID]
            entity_power = dev.get(CONF_ENTITY_POWER, "")
            entity_soc = dev.get(CONF_ENTITY_SOC, "")
            entity_active = dev.get(CONF_ENTITY_ACTIVE, "")
            entity_soc_min = dev.get(CONF_ENTITY_SOC_MIN, "")
            entity_soc_max = dev.get(CONF_ENTITY_SOC_MAX, "")
            entity_vehicle_status = dev.get(CONF_ENTITY_VEHICLE_STATUS, "")
            entity_charge_mode = dev.get(CONF_ENTITY_CHARGE_MODE, "")

            current_power = self._read_power_kw(entity_power)
            soc_percent = self._read_entity_state(entity_soc)
            is_active_raw = self._read_entity_state(entity_active)
            soc_min = self._read_number(entity_soc_min)
            soc_max = self._read_number(entity_soc_max)
            vehicle_status = self._read_string(entity_vehicle_status)
            charge_mode = self._read_string(entity_charge_mode)

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
            if soc_min is not None:
                payload["soc_min_percent"] = soc_min
            if soc_max is not None:
                payload["soc_max_percent"] = soc_max
            if vehicle_status is not None:
                payload["vehicle_status"] = vehicle_status
            if charge_mode is not None:
                payload["charge_mode"] = charge_mode

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
                "soc_min_percent": soc_min,
                "soc_max_percent": soc_max,
                "vehicle_status": vehicle_status,
                "charge_mode": charge_mode,
                "is_active": is_active,
                "is_online": True,
            }

        return result

    async def async_post_command(
        self, device_id: str, payload: dict[str, Any]
    ) -> bool:
        """POST a command body to the backend in the schema it expects.

        Payload must include `action` plus the action-specific fields the
        backend's DeviceCommand schema demands, e.g.:
          {"action": "toggle_active",  "is_active": True}
          {"action": "set_soc_min",    "soc_min_percent": 25.0}
        """
        try:
            response = await self._authenticated_request(
                "POST",
                f"/api/v1/devices/{device_id}/commands",
                json=payload,
            )
            response.raise_for_status()
            return True
        except (httpx.HTTPStatusError, httpx.RequestError) as err:
            _LOGGER.error("Command failed: %s", err)
            return False

    # ── Inbound WS: commands pushed from the Crowdergy backend ────────────

    def _ws_url(self) -> str:
        base = self.api_url
        if base.startswith("https://"):
            base = "wss://" + base[len("https://"):]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://"):]
        return f"{base}/api/v1/ws/{self._user_id}"

    async def _run_ws_loop(self) -> None:
        """Reconnecting WS listener for inbound commands from the backend."""
        delay = WS_RECONNECT_INITIAL
        session = aiohttp_client.async_get_clientsession(self.hass)
        while True:
            try:
                async with session.ws_connect(
                    self._ws_url(),
                    headers=self._auth_headers(),
                    heartbeat=30,
                ) as ws:
                    delay = WS_RECONNECT_INITIAL
                    _LOGGER.info("Crowdergy WS connected")
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                await self._handle_ws_message(json.loads(msg.data))
                            except Exception as err:  # noqa: BLE001
                                _LOGGER.exception("Failed to handle WS message: %s", err)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
            except asyncio.CancelledError:
                raise
            except aiohttp.WSServerHandshakeError as err:
                # 401 = token expired/invalid → try refresh, then reconnect
                if err.status == 401 and await self._refresh_access_token():
                    continue
                _LOGGER.warning("WS handshake failed (%s), backing off %ss", err.status, delay)
            except aiohttp.ClientError as err:
                _LOGGER.warning("WS client error: %s — reconnecting in %ss", err, delay)
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("Unexpected WS error: %s", err)

            await asyncio.sleep(delay)
            delay = min(delay * 2, WS_RECONNECT_MAX)

    async def _handle_ws_message(self, data: dict[str, Any]) -> None:
        if data.get("type") != "command":
            return
        action = data.get("action")
        device_id = data.get("device_id")
        value = data.get("value")
        if not action or not device_id:
            return

        dev = next(
            (d for d in self.devices if d.get(CONF_DEVICE_ID) == device_id),
            None,
        )
        if dev is None:
            _LOGGER.debug("Ignoring command for unknown device %s", device_id)
            return

        if action == "set_soc_min":
            await self._set_number_entity(dev.get(CONF_ENTITY_SOC_MIN, ""), value)
        elif action == "set_soc_max":
            await self._set_number_entity(dev.get(CONF_ENTITY_SOC_MAX, ""), value)
        elif action == "set_charge_mode":
            await self._set_charge_mode_entity(dev.get(CONF_ENTITY_CHARGE_MODE, ""), value)
        elif action == "toggle_active":
            await self._set_active_entity(dev.get(CONF_ENTITY_ACTIVE, ""), bool(value))
        else:
            _LOGGER.debug("Ignoring unsupported inbound action: %s", action)

    async def _set_charge_mode_entity(self, entity_id: str, value: Any) -> None:
        if not entity_id:
            _LOGGER.debug("No select entity configured for charge mode command")
            return
        option = str(value)
        if option not in CHARGE_MODE_OPTIONS:
            _LOGGER.warning(
                "Refusing to apply unknown charge mode %r (allowed: %s)",
                option,
                CHARGE_MODE_OPTIONS,
            )
            return
        await self.hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": entity_id, "option": option},
            blocking=True,
        )

    async def _set_number_entity(self, entity_id: str, value: Any) -> None:
        if not entity_id:
            _LOGGER.debug("No number entity configured for SoC command")
            return
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            _LOGGER.warning("Invalid SoC value %r for %s", value, entity_id)
            return
        await self.hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": entity_id, "value": numeric},
            blocking=True,
        )

    async def _set_active_entity(self, entity_id: str, on: bool) -> None:
        if not entity_id:
            return
        domain = entity_id.split(".", 1)[0]
        # Only switch-like domains support turn_on/turn_off.
        if domain not in ("switch", "input_boolean", "light", "fan"):
            _LOGGER.debug("Cannot toggle entity %s — unsupported domain", entity_id)
            return
        service = "turn_on" if on else "turn_off"
        await self.hass.services.async_call(
            domain,
            service,
            {"entity_id": entity_id},
            blocking=True,
        )
