"""DataUpdateCoordinator for Crowdergy Connector."""
from __future__ import annotations

import asyncio
import json
import logging
import time
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
    CONF_ENTITY_CURRENT_TEMP,
    CONF_ENTITY_TARGET_TEMP,
    CONF_ENTITY_ENERGY_TOTAL,
    CONF_ENTITY_ENERGY_DISCHARGED_TOTAL,
    CONF_ENTITY_OUTDOOR_TEMP,
    CONF_REFRESH_TOKEN,
    CONF_USER_ID,
    CONF_VALUE_OFF,
    CONF_VALUE_ON,
    CONF_VEHICLE_STATUS_VALUE_ERROR,
    CONF_VEHICLE_STATUS_VALUE_PLUGGED,
    CONF_VEHICLE_STATUS_VALUE_UNPLUGGED,
    DOMAIN,
    ENTITY_CONTROL_HOLD_ALWAYS,
    ENTITY_CONTROL_HOLD_NEVER,
    HOLD_INITIAL_DELAY,
    HOLD_POLL_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

HEARTBEAT_INTERVAL = 30
"""Coordinator's regular scheduled tick — every 30 s the coordinator
recomputes state for all devices regardless of HA events. Combined
with event-driven refreshes (state-change listener) and the per-
device send threshold below, this gives an upper bound on staleness
without flooding the backend with rows."""

EVENT_REFRESH_MIN_INTERVAL = 5.0
"""Throttle for event-driven `async_refresh` calls — if an HA state
change fires within EVENT_REFRESH_MIN_INTERVAL seconds of the
previous one, skip it. The scheduled 30 s heartbeat will catch
anything missed. Prevents storms when a power sensor updates every
sub-second."""

PER_DEVICE_HEARTBEAT_INTERVAL = 30.0
"""Even when nothing crossed a value threshold, send at least one
PATCH per device every 30 s — matches the coordinator's scheduled
tick so EVERY tick a quiet device sends one row. Why so frequent:
iOS marks "connecting" after 35 s and "offline" after 70 s of
global silence. A 60 s heartbeat looked fine on average but flickered
to offline when devices synced into the same tick window (no PATCH
between them for > 70 s). 30 s leaves no room for that race.
Quiet devices therefore cost 1 row / 30 s = 2880 rows/device/day,
still ≥ 10× reduction vs the pre-v1.18 event-storm."""

# Per-field "changed enough to be worth a row" thresholds. When NO
# field crosses these AND the per-device heartbeat hasn't expired,
# the entire PATCH is skipped. Categorical fields (vehicle_status,
# charge_mode, is_on) trigger on ANY change.
SEND_THRESHOLDS: dict[str, float] = {
    "power_kw": 0.05,         # 50 W
    "soc_percent": 1.0,       # 1 percentage point
    "current_temp_c": 0.3,    # 0.3 °C
    "target_temp_c": 0.3,
}

WS_RECONNECT_INITIAL = 1
WS_RECONNECT_MAX = 60


def _load_manifest_version() -> str:
    """Read the integration's version from manifest.json. Used to
    populate the X-Crowdergy-Connector-Version header so the backend
    (and iOS in turn) can see which connector is in play. Falls back
    to '0.0.0' if the file is missing/garbled — the worst case is the
    iOS banner never lights up, which is harmless."""
    try:
        import os
        path = os.path.join(os.path.dirname(__file__), "manifest.json")
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f).get("version", "0.0.0")
    except Exception:  # noqa: BLE001
        return "0.0.0"


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
        # Wallbox charge_mode snapshot per device — captures whatever
        # the user had set on entity_charge_mode BEFORE Crowdergize
        # was switched ON, so we can restore it on OFF. In-memory only;
        # an HA-restart mid-session loses the snapshot (V1 tradeoff —
        # rare case, the worst outcome is the entity stays at the
        # MPC-override value after Crowdergize OFF, easy to fix
        # manually). Keyed by device_id.
        self._pre_crowdergize_charge_mode: dict[str, str] = {}
        # Read once at coordinator init — never changes during a HA
        # session (a manifest bump means HACS reloads the integration).
        self._connector_version: str = _load_manifest_version()
        # Last SENT (not just last read) lifetime-kWh per device.
        # Used to compute Δ-since-last-PATCH on the next send. We
        # track "last sent" rather than "last read" so the per-tick
        # threshold-skip doesn't drop kWh — if we skip 3 ticks in a
        # row because power didn't move enough, the eventual PATCH
        # still carries the accumulated kWh since the last actual
        # send. Starts empty after a coordinator restart so the very
        # first sample doesn't emit a phantom delta against zero.
        self._prev_energy_kwh: dict[str, float] = {}
        # Battery-only twin of `_prev_energy_kwh` for the discharge
        # counter (CONF_ENTITY_ENERGY_DISCHARGED_TOTAL). Same reset
        # / Δ rules apply.
        self._prev_energy_kwh_discharged: dict[str, float] = {}
        # Per-device send bookkeeping driving SEND_THRESHOLDS — the
        # most-recent payload we actually pushed to the backend, plus
        # a wall-clock timestamp of that push. `_should_send()` uses
        # both to decide whether the current tick's payload differs
        # enough to be worth a row.
        self._last_sent_payload: dict[str, dict[str, Any]] = {}
        self._last_send_at: dict[str, float] = {}
        # Throttle bookkeeping for the event-driven `async_refresh`
        # path. The scheduled 30 s tick is unaffected.
        self._last_event_refresh_at: float = 0.0
        self._build_entity_map()

    def _build_entity_map(self) -> None:
        """Map entity_ids to their device_ids for fast lookup on state changes.

        We include entity_control so that user-driven HA-side toggles
        (e.g. someone flips the coffee-machine switch in HA) trigger
        an immediate refresh and propagate `is_on` to the backend.
        Without this, the HA → app direction was silent.
        """
        for dev in self.devices:
            device_id = dev[CONF_DEVICE_ID]
            for key in (
                CONF_ENTITY_POWER,
                CONF_ENTITY_SOC,
                CONF_ENTITY_VEHICLE_STATUS,
                CONF_ENTITY_CURRENT_TEMP,
                CONF_ENTITY_TARGET_TEMP,
                CONF_ENTITY_ENERGY_TOTAL,
                CONF_ENTITY_CONTROL,
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
            # Event-driven refresh with a 5 s min-interval throttle.
            # Fast-changing sensors (power can fire sub-second) would
            # otherwise trigger a refresh per event and storm the
            # backend with rows. The scheduled 30 s heartbeat picks up
            # anything missed; threshold check inside the PATCH loop
            # ensures unchanged-enough payloads are skipped regardless.
            now = self.hass.loop.time()
            if now - self._last_event_refresh_at < EVENT_REFRESH_MIN_INTERVAL:
                return
            self._last_event_refresh_at = now
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
        return {
            "Authorization": f"Bearer {self._access_token}",
            # Lets the backend stamp users.connector_version so the iOS
            # app can compare against min_connector_version and surface
            # an "Update verfügbar" banner. Read from manifest.json at
            # config-flow time and threaded through `entry.data`.
            "X-Crowdergy-Connector-Version": self._connector_version,
        }

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

    def _should_send(self, device_id: str, payload: dict[str, Any]) -> bool:
        """Decide whether the just-computed payload differs enough
        from the last sent one to be worth a new telemetry row.

        Returns True if any of:
          * No previous payload exists yet for this device (first send).
          * `PER_DEVICE_HEARTBEAT_INTERVAL` has elapsed since the last
            send (keeps the backend's freshness signal alive even when
            nothing's changing).
          * A numeric field crossed its SEND_THRESHOLDS magnitude.
          * A categorical field (vehicle_status / charge_mode / is_on)
            differs at all from the last sent value.
          * `energy_kwh_delta` carries a positive value (any energy
            since last send is worth recording).
        """
        prev = self._last_sent_payload.get(device_id)
        if prev is None:
            return True
        last = self._last_send_at.get(device_id, 0.0)
        if time.time() - last >= PER_DEVICE_HEARTBEAT_INTERVAL:
            return True
        # Any non-zero energy Δ (signed for storage devices, positive
        # otherwise) is reason enough to land a row — every kWh
        # matters for the chart totals.
        if abs(payload.get("energy_kwh_delta") or 0.0) > 0:
            return True
        for key, threshold in SEND_THRESHOLDS.items():
            cur, old = payload.get(key), prev.get(key)
            if cur is None and old is None:
                continue
            if cur is None or old is None:
                return True   # presence flipped
            if abs(cur - old) >= threshold:
                return True
        for key in ("vehicle_status", "charge_mode", "is_on"):
            if payload.get(key) != prev.get(key):
                return True
        return False

    def _read_energy_kwh(self, entity_id: str) -> float | None:
        """Read a `total_increasing` HA energy sensor as kWh.

        Most integrations report in kWh directly, but a few (Shelly
        EM in default mode, some Modbus bridges) expose the lifetime
        counter in Wh — the raw value would be 1000× too high and
        the iOS-side display would scream "MWh consumed today" on a
        sub-1-kWh tick. Read `unit_of_measurement` from the state's
        attributes and normalise.
        """
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        try:
            value = float(state.state)
        except (ValueError, TypeError):
            return None
        unit = (state.attributes.get("unit_of_measurement") or "").strip().lower()
        if unit in ("wh", "w·h", "watt-hours", "watthours"):
            return value / 1000.0
        if unit in ("mwh", "megawatt-hours"):
            return value * 1000.0
        # Default assume kWh — matches HA's recommended state_class
        # for energy sensors and the user-confirmed setup here.
        return value

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

    def _normalised_vehicle_status(
        self, dev: dict[str, Any], raw: str | None
    ) -> str | None:
        """Translate a wallbox's vehicle-status sensor reading into one
        of the normalised values the backend / iOS expects:
        'plugged' / 'unplugged' / 'error'. Returns the original string
        when no mapping is configured (pre-v2.0 config entries) so the
        iOS app still has something to display while the user re-runs
        the config flow.
        """
        if raw is None:
            return None
        plugged = dev.get(CONF_VEHICLE_STATUS_VALUE_PLUGGED, "")
        unplugged = dev.get(CONF_VEHICLE_STATUS_VALUE_UNPLUGGED, "")
        error = dev.get(CONF_VEHICLE_STATUS_VALUE_ERROR, "")
        # If neither plugged nor unplugged is mapped the user hasn't
        # configured the ternary yet — pass through raw.
        if not plugged and not unplugged:
            return raw
        if plugged and raw == plugged:
            return "plugged"
        if unplugged and raw == unplugged:
            return "unplugged"
        if error and raw == error:
            return "error"
        # The car reported a state the user didn't include in their
        # mapping. Surface it as 'error' so the iOS UI doesn't silently
        # treat it as plugged / unplugged.
        return "error"

    def _read_is_on_state(self, dev: dict[str, Any]) -> bool | None:
        """Translate the device's entity_control current state into a
        Boolean `is_on`. Returns None when we can't decide cleanly so the
        backend keeps its existing value rather than guessing.

        - switch / input_boolean / light / fan: HA's native "on" / "off".
        - number / select / climate: compare against value_on / value_off.
          Equal to value_on → True, equal to value_off → False, anything
          else (a user setting a different value manually) → None.
        """
        entity_id = dev.get(CONF_ENTITY_CONTROL, "") or ""
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None

        domain = entity_id.split(".", 1)[0]
        raw_state = str(state.state)

        if domain in ("switch", "input_boolean", "light", "fan"):
            if raw_state.lower() == "on":
                return True
            if raw_state.lower() == "off":
                return False
            return None

        value_on = dev.get(CONF_VALUE_ON, "")
        value_off = dev.get(CONF_VALUE_OFF, "")

        def _matches(target: Any) -> bool:
            if target in ("", None):
                return False
            if domain in ("number", "input_number"):
                try:
                    return float(raw_state) == float(target)
                except (TypeError, ValueError):
                    return False
            return raw_state == str(target)

        if _matches(value_on):
            return True
        if _matches(value_off):
            return False
        return None

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

    async def _push_outdoor_temp(self) -> None:
        """Read the optional integration-wide outdoor-temp entity and
        POST it to /users/me/outdoor. Silently skipped when the user
        didn't configure one — the backend then falls back to its own
        Open-Meteo poll for this user.
        """
        entity_id = self.entry.data.get(CONF_ENTITY_OUTDOOR_TEMP, "")
        if not entity_id:
            return
        temp = self._read_entity_state(entity_id)
        if temp is None:
            return
        try:
            response = await self._authenticated_request(
                "POST",
                "/api/v1/users/me/outdoor",
                json={"outdoor_temp_c": temp},
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as err:
            _LOGGER.warning(
                "Outdoor-temp push rejected (%s): %s",
                err.response.status_code,
                err.response.text,
            )
        except httpx.RequestError as err:
            _LOGGER.warning("Outdoor-temp push failed: %s", err)

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        if not self._active_state_bootstrapped:
            await self._bootstrap_active_state()

        # Integration-wide push: if the user wired an outdoor-temp
        # sensor at setup, send its current reading to the backend
        # once per tick. The backend keeps it on the user row and
        # iOS reads it for the PowerView header.
        await self._push_outdoor_temp()

        result: dict[str, dict[str, Any]] = {}

        for dev in self.devices:
            device_id = dev[CONF_DEVICE_ID]
            entity_power = dev.get(CONF_ENTITY_POWER, "")
            entity_soc = dev.get(CONF_ENTITY_SOC, "")
            entity_vehicle_status = dev.get(CONF_ENTITY_VEHICLE_STATUS, "")
            entity_charge_mode = dev.get(CONF_ENTITY_CHARGE_MODE, "")
            entity_current_temp = dev.get(CONF_ENTITY_CURRENT_TEMP, "")
            entity_target_temp = dev.get(CONF_ENTITY_TARGET_TEMP, "")
            entity_energy_total = dev.get(CONF_ENTITY_ENERGY_TOTAL, "")
            entity_energy_discharged_total = dev.get(
                CONF_ENTITY_ENERGY_DISCHARGED_TOTAL, ""
            )

            current_power = self._read_power_kw(entity_power)
            soc_percent = self._read_entity_state(entity_soc)
            # Vehicle-status: v2.0 normalises the raw HA state to one
            # of 'plugged' / 'unplugged' / 'error' using the per-device
            # mapping the user configured in the wallbox flow. If no
            # mapping is set yet (pre-v2.0 config entry), we forward
            # the raw string so the iOS app can still display *some*
            # status while the user re-runs the flow.
            vehicle_status = self._normalised_vehicle_status(
                dev, self._read_string(entity_vehicle_status)
            )
            # Read charge_mode back from HA so an external change (user
            # flipping the wallbox select in HA directly, or the
            # device's own logic) propagates up to iOS. Was previously
            # write-only via the set_charge_mode command, which left
            # iOS showing a stale value whenever the wallbox or HA
            # changed it on its own.
            charge_mode = self._read_string(entity_charge_mode)
            current_temp_c = self._read_entity_state(entity_current_temp)
            target_temp_c = self._read_entity_state(entity_target_temp)
            # Lifetime cumulative energy in kWh (unit-normalised from
            # the HA `unit_of_measurement` attribute). We still send
            # the raw cumulative for debugging, but the iOS chart
            # reads from the Δ-per-tick computed below.
            energy_kwh_total = self._read_energy_kwh(entity_energy_total)
            # Δ since last actually-SENT tick (not last read). Skipped
            # ticks (no field crossed its threshold) accumulate into
            # the next send so kWh is never lost. None for the first
            # read after a coordinator restart, or for backward jumps
            # (sensor reset / replacement) so the backend never lands
            # a negative contribution. `_prev_energy_kwh` is updated
            # ONLY after a successful PATCH below.
            # Per-tick `energy_kwh_delta`. Sign convention matches the
            # underlying power_kw convention for the device type:
            #   * heatpump / wallbox / generic / haushalt / solar
            #     (one entity mapped) → POSITIVE consumption Δ,
            #     unchanged from pre-v1.24.
            #   * battery / grid (two entities mapped, v1.24+)
            #     → signed net `delivered − consumed`. Positive
            #     when the device delivered net energy back to the
            #     home (battery discharge, grid import). Matches the
            #     existing battery/grid power_kw sign convention.
            #
            # `entity_energy_total` is the "consumed by device"
            # counter (battery: charged; grid: imported); the
            # optional `entity_energy_discharged_total` is the
            # "delivered by device" counter (battery: discharged;
            # grid: exported; later V2G wallbox: V2G-out). The
            # backend stores whatever signed value we emit here.
            energy_kwh_total_out = self._read_energy_kwh(
                entity_energy_discharged_total
            )
            in_delta: float | None = None
            out_delta: float | None = None
            if energy_kwh_total is not None:
                prev_in = self._prev_energy_kwh.get(device_id)
                if prev_in is not None:
                    raw = energy_kwh_total - prev_in
                    in_delta = raw if raw > 0 else 0.0
            if energy_kwh_total_out is not None:
                prev_out = self._prev_energy_kwh_discharged.get(device_id)
                if prev_out is not None:
                    raw = energy_kwh_total_out - prev_out
                    out_delta = raw if raw > 0 else 0.0
            energy_kwh_delta: float | None = None
            if out_delta is not None:
                # Two-entity storage device → signed net.
                energy_kwh_delta = out_delta - (in_delta or 0.0)
            elif in_delta is not None:
                # Single-entity consumption (or production) device →
                # positive Δ as before.
                energy_kwh_delta = in_delta
            # Derive is_on from the live HA state of entity_control so a
            # user-driven HA-side toggle propagates up to the backend
            # (and from there to iOS via SSE). Returns None when we
            # can't decide (no mapping, unknown state, ambiguous values);
            # the backend then leaves device.is_on untouched.
            is_on = self._read_is_on_state(dev)

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
            if charge_mode is not None:
                payload["charge_mode"] = charge_mode
            if current_temp_c is not None:
                payload["current_temp_c"] = current_temp_c
            if target_temp_c is not None:
                payload["target_temp_c"] = target_temp_c
            if energy_kwh_total is not None:
                payload["energy_kwh_total"] = energy_kwh_total
            if energy_kwh_delta is not None:
                payload["energy_kwh_delta"] = energy_kwh_delta
            if is_on is not None:
                payload["is_on"] = is_on

            if device_id and self._should_send(device_id, payload):
                try:
                    response = await self._authenticated_request(
                        "PATCH",
                        f"/api/v1/devices/{device_id}/telemetry",
                        json=payload,
                    )
                    response.raise_for_status()
                    # Bookkeeping only on successful send so the next
                    # tick's threshold check + kWh-Δ both reflect the
                    # state the backend actually has. If the PATCH
                    # raised, we'll retry on the next tick with a
                    # threshold computed against the previous good
                    # send, not against this (lost) attempt.
                    self._last_sent_payload[device_id] = payload
                    self._last_send_at[device_id] = time.time()
                    if energy_kwh_total is not None:
                        self._prev_energy_kwh[device_id] = energy_kwh_total
                    if energy_kwh_total_out is not None:
                        self._prev_energy_kwh_discharged[device_id] = (
                            energy_kwh_total_out
                        )
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

        # Hold-loop self-heal. `_apply_device_state` only fires on
        # is_on *transitions* coming through the SSE WS — but the MPC
        # tick re-decides the same state every 5 minutes, and HA
        # restarts wipe live hold tasks. Without this guard, a device
        # that should be ON loses its periodic re-write the moment a
        # transition is missed (warmwasser case 2026-05-22: hysteresis
        # 53→60, MPC writes "60" once, register reverts, next MPC tick
        # is also "60" → no transition → no rewrite → device stays
        # off). Walks each Crowdergize-active device once per tick and
        # restarts the hold task if it's gone.
        for device_id in list(result.keys()):
            if not self._active_state.get(device_id, False):
                continue
            if device_id not in self._on_state:
                continue  # never commanded — don't synthesise a write
            task = self._hold_tasks.get(device_id)
            if task is not None and not task.done():
                continue
            dev = next(
                (d for d in self.devices if d.get(CONF_DEVICE_ID) == device_id),
                None,
            )
            if dev is None:
                continue
            mode = (
                dev.get(CONF_ENTITY_CONTROL_HOLD, ENTITY_CONTROL_HOLD_ALWAYS)
                or ENTITY_CONTROL_HOLD_ALWAYS
            )
            if mode == ENTITY_CONTROL_HOLD_NEVER:
                continue  # user opted out of periodic rewriting
            await self._apply_device_state(
                device_id, self._on_state[device_id]
            )

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
                    # Wallbox charge_mode snapshot/restore. Only fires
                    # for wallbox devices that have BOTH an entity_
                    # charge_mode configured AND a backend-side
                    # charge_mode_value_crowdergy set (= user has
                    # explicitly opted into the override behaviour).
                    crowdergy_value = payload.get("charge_mode_value_crowdergy")
                    if new_value and crowdergy_value:
                        await self._snapshot_and_override_charge_mode(
                            device_id, str(crowdergy_value)
                        )
                    elif not new_value:
                        await self._restore_charge_mode(device_id)
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

    async def _snapshot_and_override_charge_mode(
        self, device_id: str, override_value: str
    ) -> None:
        """Crowdergize ON for a wallbox: snapshot the current
        entity_charge_mode state (so we can restore on OFF) and
        write the user-configured "MPC controls this" value (e.g.
        "Power Mode") so the wallbox firmware doesn't self-optimise
        against the MPC plan.

        Idempotent: if a snapshot for this device already exists
        (Crowdergize toggled OFF→ON→OFF→ON without going through
        the restore branch — shouldn't happen, but defensive), we
        keep the existing snapshot so a future restore still hits
        the user's original value."""
        dev = next(
            (d for d in self.devices if d.get(CONF_DEVICE_ID) == device_id),
            None,
        )
        if dev is None:
            return
        entity_id = dev.get(CONF_ENTITY_CHARGE_MODE, "") or ""
        if not entity_id:
            # No charge_mode entity configured; nothing to override.
            return
        # Snapshot current value if we don't already have one.
        if device_id not in self._pre_crowdergize_charge_mode:
            state = self.hass.states.get(entity_id)
            if state is not None and state.state not in ("unknown", "unavailable"):
                self._pre_crowdergize_charge_mode[device_id] = state.state
                _LOGGER.info(
                    "Snapshotted charge_mode for %s: %r",
                    device_id, state.state,
                )
        # Override.
        await self._apply_charge_mode(device_id, override_value)

    async def _restore_charge_mode(self, device_id: str) -> None:
        """Crowdergize OFF for a wallbox: write the snapshotted
        pre-Crowdergize value back to entity_charge_mode so the
        user's original Solar-Pure / Eco / whatever logic resumes
        owning the wallbox."""
        snapshot = self._pre_crowdergize_charge_mode.pop(device_id, None)
        if snapshot is None:
            # No snapshot — either no override was applied (no
            # entity / no crowdergy_value), or the snapshot was lost
            # to an HA restart mid-session. Either way nothing to do.
            return
        _LOGGER.info(
            "Restoring charge_mode for %s: %r",
            device_id, snapshot,
        )
        await self._apply_charge_mode(device_id, snapshot)

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
        from the config entry, dispatches the right HA service via
        `_write_entity_control`, and kicks off the hold loop that
        guards against auto-revert. No-ops cleanly if anything's
        missing so a partial / new-style config can't crash the
        coordinator.
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

        await self._write_entity_control(
            entity_id, domain, raw_value, on, verbose=True,
        )

        # After the initial write, kick off (or replace) the hold loop
        # that handles devices reverting their entity_control value
        # back to a default — Kostal-style auto-reset Modbus registers,
        # some OCPP-wallbox transaction timeouts, etc.
        self._start_hold(device_id, entity_id, raw_value, domain, on)

    async def _write_entity_control(
        self,
        entity_id: str,
        domain: str,
        raw_value: Any,
        on: bool,
        *,
        verbose: bool,
    ) -> None:
        """Single domain-dispatched write to a HA entity_control entity.
        Shared by the apply-on-transition path (`_apply_device_state`,
        `verbose=True`) and the hold-loop rewrite (`_hold_loop`,
        `verbose=False`).

        `verbose=True` surfaces WARNING-level breadcrumbs for missing
        config / unsupported domains so the user notices a misconfig
        on first toggle. The hold path stays quiet — the same issue
        would otherwise log every HOLD_POLL_INTERVAL.
        """
        try:
            if domain in ("switch", "input_boolean", "light", "fan"):
                # Bool-style entities: on/off is implicit (turn_on /
                # turn_off services). Honour any configured value_on /
                # value_off string only as an override — empty string is
                # fine and just maps to the natural on/off semantic.
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
            # All non-binary domains below need an explicit value.
            if raw_value in ("", None):
                if verbose:
                    _LOGGER.warning(
                        "%s has no value_%s configured — skipping HA write",
                        entity_id, "on" if on else "off",
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
            elif verbose:
                _LOGGER.warning(
                    "Unsupported entity_control domain %s for %s",
                    domain, entity_id,
                )
        except (ValueError, TypeError) as err:
            if verbose:
                _LOGGER.warning(
                    "Bad value_%s=%r for %s (%s) — %s",
                    "on" if on else "off", raw_value, entity_id, domain, err,
                )
        except Exception as err:  # noqa: BLE001
            if verbose:
                _LOGGER.exception("entity_control write failed: %s", err)
            else:
                _LOGGER.warning("hold re-apply failed for %s: %s", entity_id, err)

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
        # Hold mode: only `never` (no loop) vs everything-else (= the
        # periodic rewrite). The legacy `auto` value left over in old
        # config entries collapses to the rewrite path too — field-
        # testing on 2026-05-22 showed hysteresis-laden devices
        # (warmwasser, Kostal Modbus regs) need the periodic write to
        # ever take effect, and the rewrite is harmless on devices
        # that hold fine on their own.
        mode = dev.get(CONF_ENTITY_CONTROL_HOLD) or ENTITY_CONTROL_HOLD_ALWAYS
        if mode == ENTITY_CONTROL_HOLD_NEVER:
            return

        self._hold_tasks[device_id] = asyncio.create_task(
            self._hold_loop(device_id, entity_id, raw_value, domain, on)
        )

    def _cancel_hold(self, device_id: str) -> None:
        task = self._hold_tasks.pop(device_id, None)
        if task is not None and not task.done():
            task.cancel()

    def forget_device(self, device_id: str) -> None:
        """Prune every per-device bookkeeping dict for a removed
        device. Called from `async_remove_config_entry_device` so
        stale keys don't accumulate across the lifetime of one
        coordinator instance (HA doesn't force a reload on device
        removal). Idempotent — missing keys are silently ignored."""
        self._cancel_hold(device_id)
        for d in (
            self._active_state,
            self._on_state,
            self._prev_energy_kwh,
            self._prev_energy_kwh_discharged,
            self._last_sent_payload,
            self._last_send_at,
            self._pre_crowdergize_charge_mode,
        ):
            d.pop(device_id, None)

    async def _hold_loop(
        self,
        device_id: str,
        entity_id: str,
        raw_value: Any,
        domain: str,
        on: bool,
    ) -> None:
        """Keep entity_control sticking to its commanded value: re-
        write every HOLD_POLL_INTERVAL seconds for as long as
        Crowdergize is active for this device. The loop also reads
        the current HA state and surfaces an INFO log whenever the
        entity drifted from the commanded value between rewrites —
        useful for debugging hysteresis-prone devices.

        Bails out if Crowdergize gets switched off (the `_cancel_hold`
        path covers that) or on coordinator shutdown.
        """
        try:
            # Initial delay gives the apply call's effect time to
            # propagate before the first rewrite (avoids a duplicate
            # service call back-to-back).
            await asyncio.sleep(HOLD_INITIAL_DELAY)
            while True:
                if not self._active_state.get(device_id, False):
                    return
                expected = self._expected_state_value(raw_value, on, domain)
                actual = self._read_current_state(entity_id)
                if (
                    actual is not None
                    and expected is not None
                    and actual != expected
                ):
                    _LOGGER.info(
                        "hold: %s reverted (%r → %r), re-writing",
                        entity_id, expected, actual,
                    )
                await self._write_entity_control(
                    entity_id, domain, raw_value, on, verbose=False,
                )
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

