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
    CONF_ACCESS_TOKEN,
    CONF_API_URL,
    CONF_DEVICE_ID,
    CONF_DEVICES,
    CONF_ENTITY_CHARGE_MODE,
    CONF_ENTITY_CONTROL,
    CONF_ENTITY_CONTROL_HOLD,
    CONF_ENTITY_POWER,
    CONF_ENTITY_SOC,
    CONF_ENTITY_VEHICLE_STATUS,
    CONF_REFRESH_TOKEN,
    CONF_USER_ID,
    CONF_VALUE_OFF,
    CONF_VALUE_ON,
    DOMAIN,
    ENTITY_CONTROL_HOLD_ALWAYS,
    ENTITY_CONTROL_HOLD_AUTO,
    ENTITY_CONTROL_HOLD_NEVER,
    HOLD_AUTO_STABLE_HITS,
    HOLD_INITIAL_DELAY,
    HOLD_POLL_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

HEARTBEAT_INTERVAL = 30
WS_RECONNECT_INITIAL = 1
WS_RECONNECT_MAX = 60


class CrowdergyCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
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
        # Crowdergize state per device — authoritative source is the backend
        # (`devices.is_active`). We mirror it locally so the HA switch
        # entity can render the latest value without round-tripping each
        # time. Bootstrapped from GET /devices on first refresh, kept fresh
        # by SSE telemetry mirror frames the backend emits after every
        # `toggle_active` (whether the toggle came from iOS or HA).
        self._active_state: dict[str, bool] = {}
        self._active_state_bootstrapped: bool = False
        # Per-device on/off state — what the backend says the device
        # should currently be set to. Updated via SSE telemetry mirror.
        self._on_state: dict[str, bool] = {}
        # Hold-loops: one asyncio.Task per device, keyed by device_id.
        # Started after each `_apply_device_state` if the device's
        # configured hold mode is anything but 'never'. Cancelled on
        # Crowdergize OFF, on shutdown, or when a fresh apply happens
        # (the old loop is replaced).
        self._hold_tasks: dict[str, asyncio.Task] = {}
        self._build_entity_map()

    def _build_entity_map(self) -> None:
        """Map entity_ids to their device_ids for fast lookup on state changes."""
        for dev in self.devices:
            device_id = dev[CONF_DEVICE_ID]
            for key in (
                CONF_ENTITY_POWER,
                CONF_ENTITY_SOC,
                CONF_ENTITY_VEHICLE_STATUS,
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
        for task in list(self._hold_tasks.values()):
            task.cancel()
        self._hold_tasks.clear()
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

    async def _bootstrap_active_state(self) -> None:
        """One-shot GET /devices to seed the Crowdergize + on/off caches.

        Without this the HA switch entity boots showing `False` (the
        coordinator-default) until the user toggles something — and a fresh
        HA restart would silently drop a previously-on state. The backend
        is the source of truth for both flags, so we mirror them once here.
        """
        try:
            response = await self._authenticated_request("GET", "/api/v1/devices")
            response.raise_for_status()
            for d in response.json():
                self._active_state[d["id"]] = bool(d.get("is_active", False))
                self._on_state[d["id"]] = bool(d.get("is_on", False))
            self._active_state_bootstrapped = True
        except (httpx.HTTPStatusError, httpx.RequestError) as err:
            _LOGGER.warning(
                "Bootstrap of device state failed (%s) — will retry next refresh", err,
            )

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        if not self._active_state_bootstrapped:
            await self._bootstrap_active_state()

        result: dict[str, dict[str, Any]] = {}

        for dev in self.devices:
            device_id = dev[CONF_DEVICE_ID]
            entity_power = dev.get(CONF_ENTITY_POWER, "")
            entity_soc = dev.get(CONF_ENTITY_SOC, "")
            entity_vehicle_status = dev.get(CONF_ENTITY_VEHICLE_STATUS, "")

            current_power = self._read_power_kw(entity_power)
            soc_percent = self._read_entity_state(entity_soc)
            vehicle_status = self._read_string(entity_vehicle_status)

            # is_active is the "Crowdergize" consent flag — owned by the
            # backend, NOT derived from any HA entity. We deliberately do
            # not include it in the telemetry payload anymore (the backend
            # would ignore it anyway since 2026-05-16, but keeping it out
            # also keeps the payload honest).
            payload: dict[str, Any] = {
                "power_kw": current_power if current_power is not None else 0.0,
                "is_online": True,
            }
            if soc_percent is not None:
                payload["soc_percent"] = soc_percent
            if vehicle_status is not None:
                payload["vehicle_status"] = vehicle_status

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
                "vehicle_status": vehicle_status,
                "is_active": self._active_state.get(device_id, False),
                "is_on": self._on_state.get(device_id, False),
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

    # ── Inbound SSE: commands pushed from the Crowdergy backend ────────────
    #
    # Replaces the previous WebSocket-based listener on 2026-05-15. The
    # backend now exposes /api/v1/stream as Server-Sent Events: an open
    # HTTP/1.1 GET that yields `data: {…json…}` lines. No ping/pong
    # protocol-level handshake, no aiohttp/Starlette idiosyncrasies — the
    # server also emits a `{"type":"ping"}` every 15 s as application-
    # level heartbeat.

    def _sse_url(self) -> str:
        return f"{self.api_url}/api/v1/stream?token={self._access_token}"

    async def _run_ws_loop(self) -> None:
        """Reconnecting SSE listener for inbound commands from the backend."""
        delay = WS_RECONNECT_INITIAL
        session = aiohttp_client.async_get_clientsession(self.hass)
        while True:
            try:
                async with session.get(
                    self._sse_url(),
                    headers={
                        "Accept": "text/event-stream",
                        "Cache-Control": "no-cache",
                    },
                    timeout=aiohttp.ClientTimeout(total=None, sock_read=None),
                ) as resp:
                    if resp.status == 401 and await self._refresh_access_token():
                        continue
                    if resp.status != 200:
                        _LOGGER.warning(
                            "Crowdergy SSE handshake failed (%s) — retrying in %ss",
                            resp.status, delay,
                        )
                        raise aiohttp.ClientError(f"status {resp.status}")
                    delay = WS_RECONNECT_INITIAL
                    _LOGGER.warning("Crowdergy SSE connected to %s/api/v1/stream", self.api_url)
                    async for raw in resp.content:
                        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                        if not line.startswith("data: "):
                            continue
                        body = line[len("data: "):]
                        try:
                            await self._handle_ws_message(json.loads(body))
                        except Exception as err:  # noqa: BLE001
                            _LOGGER.exception("Failed to handle SSE event: %s", err)
            except asyncio.CancelledError:
                raise
            except aiohttp.ClientError as err:
                _LOGGER.warning("Crowdergy SSE client error: %s — reconnecting in %ss", err, delay)
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("Unexpected SSE error: %s", err)

            await asyncio.sleep(delay)
            delay = min(delay * 2, WS_RECONNECT_MAX)

    async def _handle_ws_message(self, data: dict[str, Any]) -> None:
        # Telemetry mirror frames carry device-level state changes the user
        # may have driven from the iOS app. We watch two flags:
        #   - is_active (Crowdergize consent) — just update the local cache
        #     so the HA switch entity reflects the right value.
        #   - is_on (current on/off state) — drive the actual entity write
        #     using the user-configured entity_control + value_on/value_off.
        if data.get("type") == "telemetry":
            device_id = data.get("device_id")
            payload = data.get("data") or {}
            if not device_id:
                return
            if "is_active" in payload:
                new_value = bool(payload["is_active"])
                if self._active_state.get(device_id) != new_value:
                    self._active_state[device_id] = new_value
                    self._sync_field_into_data(device_id, "is_active", new_value)
                    # Crowdergize off → stop holding the entity_control
                    # value. The device is the user's again.
                    if not new_value:
                        self._cancel_hold(device_id)
            if "is_on" in payload:
                new_on = bool(payload["is_on"])
                if self._on_state.get(device_id) != new_on:
                    self._on_state[device_id] = new_on
                    self._sync_field_into_data(device_id, "is_on", new_on)
                    await self._apply_device_state(device_id, new_on)
            return

        # `command` frames are mostly handled via the telemetry mirror
        # above. The one that still needs explicit handling is
        # `set_charge_mode` (wallbox Lademodus), which writes a
        # dedicated entity_charge_mode select entity outside the
        # generic entity_control mechanism.
        if data.get("type") == "command":
            action = data.get("action")
            device_id = data.get("device_id")
            value = data.get("value")
            _LOGGER.warning(
                "Crowdergy SSE command frame: action=%s device=%s value=%r",
                action, device_id, value,
            )
            if action == "set_charge_mode" and device_id and value is not None:
                await self._apply_charge_mode(device_id, str(value))

    async def _apply_charge_mode(self, device_id: str, mode: str) -> None:
        """Write the wallbox's configured entity_charge_mode select entity."""
        dev = next(
            (d for d in self.devices if d.get(CONF_DEVICE_ID) == device_id),
            None,
        )
        if dev is None:
            _LOGGER.warning(
                "set_charge_mode: no matching device config for %s",
                device_id,
            )
            return
        entity_id = dev.get(CONF_ENTITY_CHARGE_MODE, "") or ""
        if not entity_id:
            _LOGGER.warning(
                "set_charge_mode: device %s has no entity_charge_mode "
                "configured — re-add the device with v1.10.0+",
                device_id,
            )
            return
        domain = entity_id.split(".", 1)[0]
        if domain not in ("select", "input_select"):
            _LOGGER.warning(
                "set_charge_mode: entity_charge_mode for %s is not a select "
                "entity (%s)", device_id, domain,
            )
            return
        _LOGGER.warning(
            "set_charge_mode: %s → %s",
            entity_id, mode,
        )
        try:
            await self.hass.services.async_call(
                domain, "select_option",
                {"entity_id": entity_id, "option": mode},
                blocking=True,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("select.select_option failed: %s", err)

    def _sync_field_into_data(self, device_id: str, field: str, value: Any) -> None:
        """Mutate `self.data[device_id][field]` and notify CoordinatorEntities.

        The HA switch entities read from `coordinator.data` — we copy the
        dict (DataUpdateCoordinator equality-checks references), update
        the field, and push via `async_set_updated_data` to trigger a
        re-render without waiting for the next refresh tick.
        """
        if self.data is None:
            return
        bucket = self.data.get(device_id)
        if bucket is None or bucket.get(field) == value:
            return
        new_data = dict(self.data)
        new_bucket = dict(bucket)
        new_bucket[field] = value
        new_data[device_id] = new_bucket
        self.async_set_updated_data(new_data)

    async def _apply_device_state(self, device_id: str, on: bool) -> None:
        """Write the user-configured entity_control to value_on / value_off.

        Looks up the device's `entity_control` + `value_on` / `value_off`
        from the config entry and dispatches the right HA service based
        on the entity's domain. No-ops cleanly if anything's missing so
        a partial / new-style config can't crash the coordinator.
        """
        dev = next(
            (d for d in self.devices if d.get(CONF_DEVICE_ID) == device_id),
            None,
        )
        if dev is None:
            return
        entity_id = dev.get(CONF_ENTITY_CONTROL, "") or ""
        if not entity_id:
            _LOGGER.debug(
                "Device %s has no entity_control mapped — Crowdergy can't switch it yet",
                device_id,
            )
            return
        domain = entity_id.split(".", 1)[0]
        raw_value = dev.get(CONF_VALUE_ON if on else CONF_VALUE_OFF, "")

        try:
            if domain in ("switch", "input_boolean", "light", "fan"):
                # Bool-style entities: on/off is implicit (turn_on /
                # turn_off services). Honour any configured value_on /
                # value_off string only as an override — empty string is
                # fine and just maps to the natural on/off semantic.
                if raw_value in ("", None):
                    service = "turn_on" if on else "turn_off"
                else:
                    want_on = str(raw_value).lower() in ("on", "true", "1", "yes", "an")
                    service = "turn_on" if want_on else "turn_off"
                await self.hass.services.async_call(
                    domain, service, {"entity_id": entity_id}, blocking=True,
                )
                return
            # All non-binary domains below need an explicit value.
            if raw_value in ("", None):
                _LOGGER.warning(
                    "Device %s has no value_%s configured — skipping HA write",
                    device_id, "on" if on else "off",
                )
                return
            if domain in ("number", "input_number"):
                await self.hass.services.async_call(
                    domain, "set_value",
                    {"entity_id": entity_id, "value": float(raw_value)},
                    blocking=True,
                )
            elif domain in ("select", "input_select"):
                await self.hass.services.async_call(
                    domain, "select_option",
                    {"entity_id": entity_id, "option": str(raw_value)},
                    blocking=True,
                )
            elif domain == "climate":
                await self.hass.services.async_call(
                    domain, "set_hvac_mode",
                    {"entity_id": entity_id, "hvac_mode": str(raw_value)},
                    blocking=True,
                )
            else:
                _LOGGER.warning(
                    "Unsupported entity_control domain %s for device %s",
                    domain, device_id,
                )
        except (ValueError, TypeError) as err:
            _LOGGER.warning(
                "Bad value_%s=%r for %s (%s) — %s",
                "on" if on else "off", raw_value, entity_id, domain, err,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("entity_control write failed: %s", err)

        # After the initial write, kick off (or replace) the hold loop
        # that handles devices reverting their entity_control value
        # back to a default — Kostal-style auto-reset Modbus registers,
        # some OCPP-wallbox transaction timeouts, etc.
        self._start_hold(device_id, entity_id, raw_value, domain, on)

    # ── entity_control hold loop ────────────────────────────────────────

    def _start_hold(
        self,
        device_id: str,
        entity_id: str,
        raw_value: Any,
        domain: str,
        on: bool,
    ) -> None:
        """Start (or replace) a hold-loop for this device.

        Cancels any existing loop for the device first — the latest
        `_apply_device_state` is the source of truth.
        """
        existing = self._hold_tasks.pop(device_id, None)
        if existing is not None and not existing.done():
            existing.cancel()

        dev = next(
            (d for d in self.devices if d.get(CONF_DEVICE_ID) == device_id),
            None,
        )
        if dev is None:
            return
        mode = (
            dev.get(CONF_ENTITY_CONTROL_HOLD, ENTITY_CONTROL_HOLD_AUTO)
            or ENTITY_CONTROL_HOLD_AUTO
        )
        if mode == ENTITY_CONTROL_HOLD_NEVER:
            return

        self._hold_tasks[device_id] = asyncio.create_task(
            self._hold_loop(device_id, entity_id, raw_value, domain, on, mode)
        )

    def _cancel_hold(self, device_id: str) -> None:
        task = self._hold_tasks.pop(device_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def _hold_loop(
        self,
        device_id: str,
        entity_id: str,
        raw_value: Any,
        domain: str,
        on: bool,
        mode: str,
    ) -> None:
        """Keep entity_control sticking to its commanded value.

        - `always`: re-write every HOLD_POLL_INTERVAL seconds.
        - `auto`:   wait HOLD_INITIAL_DELAY, then re-read the entity. If
          it has reverted, write again. After HOLD_AUTO_STABLE_HITS
          consecutive non-reverted checks, stop — the device is
          holding fine on its own.

        Both modes bail out if Crowdergize gets switched off (the
        `_cancel_hold` path covers that) or on coordinator shutdown.
        """
        try:
            # The initial delay matters most in `auto` (gives the
            # device time to apply the first write before we measure).
            # In `always` we wait the same so the first re-write isn't
            # a duplicate of the apply we just did.
            await asyncio.sleep(HOLD_INITIAL_DELAY)
            stable_hits = 0
            while True:
                if not self._active_state.get(device_id, False):
                    return
                expected = self._expected_state_value(raw_value, on, domain)
                actual = self._read_current_state(entity_id)
                reverted = (
                    actual is not None
                    and expected is not None
                    and actual != expected
                )

                if mode == ENTITY_CONTROL_HOLD_ALWAYS or reverted:
                    if reverted:
                        _LOGGER.info(
                            "hold: %s reverted (%r → %r), re-writing",
                            entity_id, expected, actual,
                        )
                    await self._reapply_entity_control(
                        entity_id, domain, raw_value, on
                    )
                    stable_hits = 0
                else:
                    stable_hits += 1
                    if (
                        mode == ENTITY_CONTROL_HOLD_AUTO
                        and stable_hits >= HOLD_AUTO_STABLE_HITS
                    ):
                        _LOGGER.debug(
                            "hold: %s stable for %d checks — releasing",
                            entity_id, stable_hits,
                        )
                        return

                await asyncio.sleep(HOLD_POLL_INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _LOGGER.exception("hold loop for %s crashed", device_id)

    def _expected_state_value(
        self, raw_value: Any, on: bool, domain: str
    ) -> str | None:
        """Normalise the value we'd compare an entity's current state
        against. Returns a string (HA states are strings) or None if
        we can't decide — in that case the loop just re-writes blindly
        in `always` mode and stays passive in `auto`.
        """
        if domain in ("switch", "input_boolean", "light", "fan"):
            return "on" if on else "off"
        if raw_value in ("", None):
            return None
        return str(raw_value)

    def _read_current_state(self, entity_id: str) -> str | None:
        """Read the entity's current state as a string, or None if HA
        has no state or it's unknown / unavailable.
        """
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        if state.state in ("unknown", "unavailable"):
            return None
        return state.state

    async def _reapply_entity_control(
        self, entity_id: str, domain: str, raw_value: Any, on: bool
    ) -> None:
        """Mini-version of `_apply_device_state` for the hold loop — same
        domain dispatch, no logging noise for the success path."""
        try:
            if domain in ("switch", "input_boolean", "light", "fan"):
                if raw_value in ("", None):
                    service = "turn_on" if on else "turn_off"
                else:
                    want_on = str(raw_value).lower() in (
                        "on", "true", "1", "yes", "an",
                    )
                    service = "turn_on" if want_on else "turn_off"
                await self.hass.services.async_call(
                    domain, service, {"entity_id": entity_id}, blocking=True,
                )
                return
            if raw_value in ("", None):
                return
            if domain in ("number", "input_number"):
                await self.hass.services.async_call(
                    domain, "set_value",
                    {"entity_id": entity_id, "value": float(raw_value)},
                    blocking=True,
                )
            elif domain in ("select", "input_select"):
                await self.hass.services.async_call(
                    domain, "select_option",
                    {"entity_id": entity_id, "option": str(raw_value)},
                    blocking=True,
                )
            elif domain == "climate":
                await self.hass.services.async_call(
                    domain, "set_hvac_mode",
                    {"entity_id": entity_id, "hvac_mode": str(raw_value)},
                    blocking=True,
                )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("hold re-apply failed for %s: %s", entity_id, err)
