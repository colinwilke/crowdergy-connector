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
    CONF_BATTERY_VALUE_PASSIVE,
    CONF_DEVICE_ID,
    CONF_DEVICE_TYPE,
    CONF_DEVICES,
    CONF_ENTITY_CHARGE_MODE,
    CONF_ENTITY_CONTROL,
    CONF_ENTITY_CONTROL_HOLD,
    CONF_ENTITY_POWER,
    CONF_ENTITY_SOC,
    CONF_ENTITY_VEHICLE_STATUS,
    CONF_ENTITY_CURRENT_TEMP,
    CONF_ENTITY_ENERGY_TOTAL,
    CONF_INVERT_POWER_SIGN,
    CONF_ENTITY_ENERGY_DISCHARGED_TOTAL,
    CONF_ENTITY_OUTDOOR_TEMP,
    CONF_ENTITY_VORLAUF_TEMP,
    CONF_REFRESH_TOKEN,
    CONF_USER_ID,
    CONF_VALUE_OFF,
    CONF_VALUE_ON,
    CONF_ENTITY_COOL_CONTROL,
    CONF_ENTITY_POWER_2,
    CONF_SUPPORTS_COOLING,
    CONF_VALUE_COOL_ON,
    CONF_VALUE_COOL_OFF,
    CONF_BATTERY_VALUE_IDLE,
    CONF_VEHICLE_STATUS_VALUE_ERROR,
    CONF_VEHICLE_STATUS_VALUE_PLUGGED,
    CONF_VEHICLE_STATUS_VALUE_UNPLUGGED,
    DOMAIN,
    ENTITY_CONTROL_HOLD_ALWAYS,
    ENTITY_CONTROL_HOLD_NEVER,
    HOLD_INITIAL_DELAY,
    HOLD_POLL_INTERVAL,
    CHARGE_MODE_HOLD_INITIAL_DELAY,
    CHARGE_MODE_HOLD_INTERVAL,
    SSE_STALE_THRESHOLD_S,
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

PER_DEVICE_HEARTBEAT_INTERVAL = 90.0
"""Soft-Heartbeat (2026-06-01+, C7): nach 90 s wird ein PATCH gesendet
WENN der payload-Hash sich seit dem letzten Send verändert hat (z.B.
durch klein-rauschende Werte unter SEND_THRESHOLDS). 90 s matched
weiterhin iOS's 120-s tile-freshness threshold für aktive Geräte.

Pre-C7 (vor 2026-06-01) lief das hier als HARD-Floor, der auch
identical-payload-PATCHes alle 90 s rausschickte — auf truly quiet
Geräten (Solar nachts, Wallbox idle, Heizung im Sommer aus) bedeutete
das ~960 unnötige HTTP-Calls/Tag/Gerät. Mit der Hash-Bedingung
fällt das auf den IDENTICAL_HEARTBEAT_INTERVAL-Floor zurück."""

IDENTICAL_HEARTBEAT_INTERVAL = 600.0
"""Hard-Ceiling für payload-identische PATCHes (C7): auch wenn nichts
am Payload changed, mindestens alle 10 min ein PATCH zur Backend-
Cache-Aktualisierung + Self-Healing der near-duplicate-Gate (falls
`_should_send`s in-memory state vom DB-Stand abdriftet).

10 min ist ein Trade-off: lang genug für signifikante HTTP-Reduktion
(~6.7× ggü. 90 s), kurz genug um die hash-dedup-gate self-heilen zu
lassen. Per-Device-Frische auf iOS-Seite kommt NICHT von hier — das
übernimmt der `_device_mirror_loop` mit `PER_DEVICE_MIRROR_INTERVAL`.
Pre-v3.4.3 hat hier ein falscher Kommentar suggeriert dass der 25-s
user-level Heartbeat die device-tiles frisch hält — der refresht aber
nur `connector_last_seen`, nicht das per-Device telemetry-Timestamp."""

PER_DEVICE_MIRROR_INTERVAL = 60.0
"""Per-device heartbeat-mirror cadence (v3.4.3+). Pushed das zuletzt
gesendete Payload erneut (ohne `energy_kwh_delta`, sonst würde der
Δ-kWh doppelt landen), wenn seit dem letzten echten PATCH ≥60 s
vergangen sind. Refresht das telemetry-row-Timestamp im Backend
sodass iOS `Telemetry.isFresh(staleAfter: 120)` für Idle-Geräte
weiterhin `true` zurückgibt — ohne den Mirror flippten Kaffeemaschine,
unbenutzte Wallbox-Stellplätze, WW im Bereitschaftsmodus alle 2 min
auf offline, weil der Hard-Ceiling-PATCH nur alle 10 min feuert."""

HEARTBEAT_PING_INTERVAL = 25.0
"""Cadence of the lightweight liveness ping the connector POSTs to
`/api/v1/users/me/heartbeat`. Independent of any device's PATCH
schedule — exists so the backend can stamp
`users.connector_last_seen` (and thus iOS's connection dot) without
relying on the high-frequency telemetry stream. Slightly under
30 s so iOS's 35 s 'live' threshold has one full ping of grace
even if the request lands at the back of a network queue."""

# Per-field "changed enough to be worth a row" thresholds. When NO
# field crosses these AND the per-device heartbeat hasn't expired,
# the entire PATCH is skipped. Categorical fields (vehicle_status,
# charge_mode, is_on) trigger on ANY change.
SEND_THRESHOLDS: dict[str, float] = {
    "power_kw": 0.05,         # 50 W
    "soc_percent": 1.0,       # 1 percentage point
    "current_temp_c": 0.3,    # 0.3 °C
}

SSE_RECONNECT_INITIAL = 1
SSE_RECONNECT_MAX = 60


# ── Solver-only Extra-Field-Registry (v3.3+) ─────────────────────────
#
# Pro Gerätetyp: Liste von (payload_key, conf_key, reader) Tupeln. Pro
# Tick liest der Coordinator jede mappte Entity, packt das Resultat in
# `payload["extra"]`. Backend filtert + validiert serverseitig
# (app/mpc/solver_fields.py) — Single Source of Truth bleibt dort.
#
# Neues Solver-Feld hier hinzufügen → fertig connector-seitig. Sobald
# der Backend-Registry-Eintrag steht, fließt das Feld pro Telemetry-
# Tick durch zum Solver.
#
# `reader` muss eine der Reader-Methoden auf der Coordinator-Klasse
# sein (siehe `_read_extra_value` im Loop). Aktuell "temp" → liest
# °C-Sensoren oder die `current_temperature` aus climate-Attributen.
_SOLVER_EXTRA_FIELDS: dict[str, list[tuple[str, str, str]]] = {
    "heating": [
        ("vorlauf_temp_c", CONF_ENTITY_VORLAUF_TEMP, "temp"),
    ],
    "warmwater": [
        # Brauchwasser-WPs liefern oft eine eigene Vorlauf-Temperatur
        # fürs Aufheizen — typisch höher als HK-VL. Backend nutzt das
        # gleiche cop_at_outdoor_temp(t_vorlauf_c=…) Modell auch hier.
        ("vorlauf_temp_c", CONF_ENTITY_VORLAUF_TEMP, "temp"),
    ],
    # Andere Gerätetypen können ihre Solver-only-Felder hier
    # anhängen ohne den eigentlichen `_async_update_data`-Loop
    # anfassen zu müssen.
}


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
        self._sse_task: asyncio.Task | None = None
        # v2.5.4: dedicated liveness ping. Decoupled from the
        # per-device telemetry stream so a fully idle home no longer
        # has to PATCH N devices every 30 s purely to keep iOS's
        # connection dot green. See `_heartbeat_loop` docstring.
        self._heartbeat_task: asyncio.Task | None = None
        self._device_mirror_task: asyncio.Task | None = None
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
        # v2.5: per-device cooling state mirror. Decoupled from
        # `_on_state` even though the solver enforces a mutex —
        # tracking them separately lets the connector dispatch the
        # heating-side and cooling-side writes independently when
        # they live on different HA entities. {True, False} only;
        # missing keys are treated as False.
        self._cool_state: dict[str, bool] = {}
        # Hold-loops: one asyncio.Task per device, keyed by device_id.
        # Started after each `_apply_device_state` if the device's
        # configured hold mode is anything but 'never'. Cancelled on
        # Crowdergize OFF, on shutdown, or when a fresh apply happens
        # (the old loop is replaced).
        self._hold_tasks: dict[str, asyncio.Task] = {}
        # v2.4: separate hold-loop tracker for the charge_mode entity.
        # Battery + wallbox Lademodus need a fresh write every ~15 s
        # because some inverters reset the mode otherwise. Keyed by
        # device_id, replaced on every fresh `_apply_charge_mode`.
        self._charge_mode_hold_tasks: dict[str, asyncio.Task] = {}
        # Last charge_mode value commanded per device — re-written
        # by the hold loop. Cleared when a "passive" command arrives.
        self._held_charge_mode: dict[str, str] = {}
        # Wall-clock des letzten SSE-Events (any type — ping,
        # telemetry, command). Hold-Loops gaten darauf, sodass ein
        # Backend-Outage oder SSE-Drop die periodische Re-Write-Logik
        # pausiert und dem Inverter die Steuerung zurückgibt statt
        # den letzten Mode-Wert ewig zu halten. Externe Reader (z.B.
        # binary_sensor.is_on) sollten über `last_sse_event_at`
        # zugreifen (public property), nicht direkt aufs Private-Attr.
        self._last_sse_event_at: float = 0.0
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
        # C7 (2026-06-01) payload-hash dedup: stabilen content-hash
        # des letzten gesendeten payloads pro Gerät. Wenn der neue
        # hash identisch ist, hat der 90s-Soft-Heartbeat nichts neues
        # zu erzählen → skip bis IDENTICAL_HEARTBEAT_INTERVAL.
        self._last_sent_hash: dict[str, int] = {}
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

    def start_sse_listener(self) -> None:
        """Open a WS connection to the backend and react to inbound command frames."""
        if not self._user_id:
            _LOGGER.warning("No user_id stored — skipping WS listener setup")
            return
        if self._sse_task and not self._sse_task.done():
            return
        self._sse_task = self.hass.async_create_background_task(
            self._run_sse_loop(),
            name=f"{DOMAIN}_sse_listener",
        )

    def start_heartbeat(self) -> None:
        """Start the dedicated liveness ping loop (v2.5.4+). Idempotent."""
        if self._heartbeat_task and not self._heartbeat_task.done():
            return
        self._heartbeat_task = self.hass.async_create_background_task(
            self._run_heartbeat_loop(),
            name=f"{DOMAIN}_heartbeat",
        )

    def start_device_mirror(self) -> None:
        """Start the per-device heartbeat-mirror loop (v3.4.3+).
        Idempotent. Pusht zuletzt gesendete Payloads erneut wenn der
        echte PATCH > PER_DEVICE_MIRROR_INTERVAL ago war, damit iOS-
        Tiles für Idle-Geräte (Kaffeemaschine aus, Wallbox leer, WW
        im Standby) nicht alle 2 min auf offline flippen."""
        if self._device_mirror_task and not self._device_mirror_task.done():
            return
        self._device_mirror_task = self.hass.async_create_background_task(
            self._run_device_mirror_loop(),
            name=f"{DOMAIN}_device_mirror",
        )

    async def _run_heartbeat_loop(self) -> None:
        """POST /users/me/heartbeat every HEARTBEAT_PING_INTERVAL.

        Backend stamps `connector_last_seen` + `connector_version`
        from this call — same effect as a telemetry PATCH used to
        have, but without writing a row to the `telemetry` table.

        Failures are logged at DEBUG (transient network blips are
        expected) and the loop continues. A sustained outage just
        means iOS will mark the connector offline after 70 s —
        identical to pre-v2.5.4 behaviour.

        Exponential backoff on consecutive failures: 25 s → 50 s →
        ... → cap at 120 s. Resets to the base interval on the next
        successful ping. Without this the ping kept hammering a
        down backend at 25 s indefinitely.
        """
        _HEARTBEAT_BACKOFF_MAX_S = 120.0
        consecutive_failures = 0
        while True:
            failed = False
            try:
                response = await self._authenticated_request(
                    "POST", "/api/v1/users/me/heartbeat"
                )
                if response.status_code >= 400:
                    _LOGGER.debug(
                        "heartbeat ping returned %s: %s",
                        response.status_code, response.text,
                    )
                    failed = True
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("heartbeat ping failed: %s", err)
                failed = True
            if failed:
                consecutive_failures += 1
                sleep_for = min(
                    HEARTBEAT_PING_INTERVAL * (2 ** (consecutive_failures - 1)),
                    _HEARTBEAT_BACKOFF_MAX_S,
                )
            else:
                consecutive_failures = 0
                sleep_for = HEARTBEAT_PING_INTERVAL
            await asyncio.sleep(sleep_for)

    async def _run_device_mirror_loop(self) -> None:
        """Periodic per-device telemetry-timestamp refresh.

        Pro Tick (= PER_DEVICE_MIRROR_INTERVAL): für jedes Gerät mit
        einem zuvor gesendeten Payload PATCHen wir das letzte Payload
        erneut, sofern der letzte echte Send ≥ PER_DEVICE_MIRROR_INTERVAL
        her ist. `energy_kwh_delta` wird weggelassen — das hatte schon
        beim Original-Send seine Δ-kWh ins Backend gebracht; nochmal
        zu senden würde doppelt zählen.

        Bookkeeping:
        - `_last_send_at` wird gestempelt damit der nächste Mirror-Tick
          das Gerät überspringt (sonst feuert jeder Tick alle Geräte).
        - `_last_sent_payload` + `_last_sent_hash` bleiben unangetastet
          — die spiegeln den letzten ECHTEN State; `_should_send` soll
          den nächsten echten Tick weiterhin anhand des letzten echten
          Hashs entscheiden, nicht anhand des Mirror-Hashs.

        Failures sind DEBUG-Log + weiter — ein Mirror-Drop ist nicht
        kritisch, der nächste Tick versucht's erneut.
        """
        while True:
            try:
                now_ts = time.time()
                for device_id, last_payload in list(self._last_sent_payload.items()):
                    age = now_ts - self._last_send_at.get(device_id, 0.0)
                    if age < PER_DEVICE_MIRROR_INTERVAL:
                        continue
                    mirror = {
                        k: v for k, v in last_payload.items()
                        if k != "energy_kwh_delta"
                    }
                    try:
                        response = await self._authenticated_request(
                            "PATCH",
                            f"/api/v1/devices/{device_id}/telemetry",
                            json=mirror,
                        )
                        if response.status_code < 400:
                            self._last_send_at[device_id] = now_ts
                        else:
                            _LOGGER.debug(
                                "device-mirror PATCH %s returned %s: %s",
                                device_id, response.status_code, response.text,
                            )
                    except asyncio.CancelledError:
                        raise
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.debug(
                            "device-mirror PATCH failed for %s: %s",
                            device_id, err,
                        )
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("device-mirror loop iteration error: %s", err)
            await asyncio.sleep(PER_DEVICE_MIRROR_INTERVAL)

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
        if self._sse_task and not self._sse_task.done():
            self._sse_task.cancel()
            try:
                await self._sse_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._device_mirror_task and not self._device_mirror_task.done():
            self._device_mirror_task.cancel()
            try:
                await self._device_mirror_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        for task in list(self._hold_tasks.values()):
            task.cancel()
        self._hold_tasks.clear()
        for task in list(self._charge_mode_hold_tasks.values()):
            task.cancel()
        self._charge_mode_hold_tasks.clear()
        self._held_charge_mode.clear()
        await self._client.aclose()

    @property
    def last_sse_event_at(self) -> float:
        """Public Accessor für externe Reader (z.B. binary_sensor) —
        statt das _last_sse_event_at Privat-Attribut direkt zu lesen."""
        return self._last_sse_event_at

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

    def _read_temp_c(self, entity_id: str) -> Any:
        """Ist-Temperatur lesen. Bei climate.* / water_heater.* steht
        im state ein Mode-String (z.B. 'heat' / 'eco') und die echte
        Temperatur sitzt im Attribut `current_temperature`. Für
        sensor-/number-Entities Fallback auf den State.
        """
        if not entity_id:
            return None
        domain = entity_id.split(".", 1)[0]
        if domain in ("climate", "water_heater"):
            state = self.hass.states.get(entity_id)
            if state is None:
                return None
            attr = state.attributes.get("current_temperature")
            if attr is None:
                return None
            try:
                return float(attr)
            except (ValueError, TypeError):
                return None
        return self._read_entity_state(entity_id)

    @staticmethod
    def _payload_hash(payload: dict[str, Any]) -> int:
        """Stable content-hash für payload-dedup (C7). `json.dumps` mit
        sort_keys + default=str für mixed-type Stabilität; built-in
        hash() ist OK weil wir nur identity-vs-difference brauchen,
        keine kryptografische Eigenschaft."""
        return hash(json.dumps(payload, sort_keys=True, default=str))

    def _should_send(self, device_id: str, payload: dict[str, Any]) -> bool:
        """Decide whether the just-computed payload differs enough
        from the last sent one to be worth a new telemetry row.

        Returns True if any of:
          * No previous payload exists yet for this device (first send).
          * `IDENTICAL_HEARTBEAT_INTERVAL` (Hard-Ceiling 10 min) seit
            letztem Send (Backend-Cache + Self-Healing der near-dup-Gate).
          * `PER_DEVICE_HEARTBEAT_INTERVAL` (Soft-Heartbeat 90 s) seit
            letztem Send UND payload-Hash unterscheidet sich
            (klein-rauschende Sub-Threshold-Werte).
          * A numeric field crossed its SEND_THRESHOLDS magnitude.
          * A categorical field (vehicle_status / charge_mode / is_on)
            differs at all from the last sent value.
          * `energy_kwh_delta` carries a positive value (any energy
            since last send is worth recording).
        """
        prev = self._last_sent_payload.get(device_id)
        if prev is None:
            return True
        age = time.time() - self._last_send_at.get(device_id, 0.0)
        # Hard ceiling — Backend-Cache + Self-Healing der near-dup-Gate.
        if age >= IDENTICAL_HEARTBEAT_INTERVAL:
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
        for key in ("vehicle_status", "charge_mode", "is_on", "cool_on"):
            if payload.get(key) != prev.get(key):
                return True
        # Soft heartbeat NUR wenn der payload-Hash sich vom letzten
        # Send unterscheidet — sonst hat der 90s-Tick nichts Neues zu
        # erzählen und wir warten auf den Hard-Ceiling. Spart auf
        # truly-quiet Geräten ~6.7× HTTP-Calls.
        if age >= PER_DEVICE_HEARTBEAT_INTERVAL:
            if self._payload_hash(payload) != self._last_sent_hash.get(device_id):
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
        """Read an entity state as the raw `state.state` string.

        C4 (2026-06-01): docstring previously claimed a friendly_value
        fallback, but the code never read attributes. The raw state IS
        the right thing — friendly_value would have masked the raw
        token the user's HA Frontend translates per locale, which
        would silently break our downstream value-matching (e.g.
        vehicle_status mapping). Aligned docstring to reality.
        """
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        text = str(state.state)
        return text if text else None

    def _normalised_vehicle_status(
        self, dev: dict[str, Any], raw: str | None
    ) -> str | None:
        """Translate a wallbox's vehicle-status sensor reading into one
        of the normalised values the backend / iOS expects:
        'plugged' / 'unplugged' / 'error'.

        Each mapping field is treated as a COMMA-SEPARATED list — most
        wallboxes have multiple states that semantically mean the same
        thing (e.g. "Connected, Charging, Paused" all = plugged). The
        user can comma-list them in a single field; the connector
        matches case-insensitively after stripping whitespace.

        Returns:
          - the matching normalised value when raw matches a mapping,
          - the RAW string when nothing matches (v2.1 used to force
            "error" here, which alarmed users whose wallbox had a
            state they hadn't mapped yet — better to pass through and
            let iOS display the actual wallbox label),
          - raw when no mapping is configured at all (pre-v2.0 setups).
        """
        if raw is None:
            return None
        plugged = dev.get(CONF_VEHICLE_STATUS_VALUE_PLUGGED, "")
        unplugged = dev.get(CONF_VEHICLE_STATUS_VALUE_UNPLUGGED, "")
        error = dev.get(CONF_VEHICLE_STATUS_VALUE_ERROR, "")
        # No mapping at all → pass through raw.
        if not plugged and not unplugged and not error:
            return raw
        normalised = raw.strip().lower()

        def _matches(mapping: str) -> bool:
            if not mapping:
                return False
            return any(
                normalised == part.strip().lower()
                for part in mapping.split(",")
                if part.strip()
            )

        if _matches(plugged):
            return "plugged"
        if _matches(unplugged):
            return "unplugged"
        if _matches(error):
            return "error"
        # Unmapped state — surface the wallbox's raw label rather than
        # mis-labelling it "error" and panicking the user.
        return raw

    def _read_is_on_state(self, dev: dict[str, Any]) -> bool | None:
        """Translate the device's entity_control current state into a
        Boolean `is_on`. Returns None when we can't decide cleanly so the
        backend keeps its existing value rather than guessing.

        - switch / input_boolean / light / fan: HA's native "on" / "off".
        - number / select / climate: compare against value_on / value_off.
          Equal to value_on → True, equal to value_off → False, anything
          else (a user setting a different value manually) → None.

        Spezialfall climate-Entity mit supports_cooling: ein "cool"
        State zählt explizit als is_on=False (nicht heizen), damit das
        Backend die Heat/Cool-Trennung sauber sieht.
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
        # Cooling-aware: wenn die selbe Entity gerade auf cool-Wert
        # steht (climate.* mit value_cool_on = "cool"), ist das Gerät
        # NICHT am heizen.
        if dev.get(CONF_SUPPORTS_COOLING):
            value_cool_on = dev.get(CONF_VALUE_COOL_ON, "")
            if _matches(value_cool_on):
                return False
        return None

    def _read_cool_on_state(self, dev: dict[str, Any]) -> bool | None:
        """Translate cooling-side state into a Boolean `cool_on`.

        Drei Konfigurationen:
        1. supports_cooling=False → immer None (Backend bleibt 0).
        2. Separate entity_cool_control gemapped → diese Entity gegen
           value_cool_on / value_cool_off (bzw. value_off).
        3. Geteilte entity_control (typisch climate.*) → die selbe
           Entity gegen value_cool_on / value_off (Heizung-Off-Wert
           dient auch als Cool-Off).

        Returns None bei unklarem State, sodass Backend cool_on
        unverändert lässt.
        """
        if not dev.get(CONF_SUPPORTS_COOLING):
            return None
        cool_entity = dev.get(CONF_ENTITY_COOL_CONTROL, "") or ""
        if cool_entity:
            entity_id = cool_entity
            value_cool_on = dev.get(CONF_VALUE_COOL_ON, "")
            value_cool_off = dev.get(CONF_VALUE_COOL_OFF, "")
        else:
            entity_id = dev.get(CONF_ENTITY_CONTROL, "") or ""
            value_cool_on = dev.get(CONF_VALUE_COOL_ON, "")
            value_cool_off = dev.get(CONF_VALUE_OFF, "")
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        domain = entity_id.split(".", 1)[0]
        raw_state = str(state.state)

        def _matches(target: Any) -> bool:
            if target in ("", None):
                return False
            if domain in ("number", "input_number"):
                try:
                    return float(raw_state) == float(target)
                except (TypeError, ValueError):
                    return False
            return raw_state == str(target)

        if _matches(value_cool_on):
            return True
        if _matches(value_cool_off):
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
            entity_power_2 = dev.get(CONF_ENTITY_POWER_2, "")
            entity_soc = dev.get(CONF_ENTITY_SOC, "")
            entity_vehicle_status = dev.get(CONF_ENTITY_VEHICLE_STATUS, "")
            entity_charge_mode = dev.get(CONF_ENTITY_CHARGE_MODE, "")
            entity_current_temp = dev.get(CONF_ENTITY_CURRENT_TEMP, "")
            entity_energy_total = dev.get(CONF_ENTITY_ENERGY_TOTAL, "")
            entity_energy_discharged_total = dev.get(
                CONF_ENTITY_ENERGY_DISCHARGED_TOTAL, ""
            )

            current_power = self._read_power_kw(entity_power)
            # v3.0 bidirektional: zweites Power-Feld vorhanden → signed
            # power = power_1 - power_2 (analog zur energy_kwh_delta-
            # Berechnung). Bei nur einer Power-Entity bleibt der
            # invert_power_sign-Pfad aktiv.
            if entity_power_2:
                power_2 = self._read_power_kw(entity_power_2)
                if current_power is not None and power_2 is not None:
                    current_power = current_power - power_2
                elif current_power is None and power_2 is not None:
                    current_power = -power_2
            elif current_power is not None and dev.get(CONF_INVERT_POWER_SIGN):
                # Sign-flip nur wenn KEIN zweites Power-Feld — sonst
                # ist die Richtung über das Differenzpaar eindeutig.
                current_power = -current_power
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
            current_temp_c = self._read_temp_c(entity_current_temp)
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
            #   * heating / warmwater / wallbox / generic / haushalt /
            #     solar (eine Entity gemapped) → POSITIVE consumption Δ
            #   * battery / grid (zwei Entities gemapped)
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
            # invert_power_sign muss SOWOHL power_kw ALS AUCH
            # energy_kwh_delta umkehren — sonst entsteht ein
            # Sign-Mismatch bei Inverter-Setups die beide
            # Konventionen entgegengesetzt zu Crowdergy haben (z.B.
            # Wechselrichter die Einspeisung positiv exposen + die
            # Einspeise-Zähl-Entity als entity_energy_total mapped).
            # Vor 2026-05-30 wurde nur power_kw invertiert →
            # kWh-Bezug/Einspeisung kamen vertauscht beim Backend an.
            if (
                energy_kwh_delta is not None
                and dev.get(CONF_INVERT_POWER_SIGN)
            ):
                energy_kwh_delta = -energy_kwh_delta
            # Derive is_on from the live HA state of entity_control so a
            # user-driven HA-side toggle propagates up to the backend
            # (and from there to iOS via SSE). Returns None when we
            # can't decide (no mapping, unknown state, ambiguous values);
            # the backend then leaves device.is_on untouched.
            is_on = self._read_is_on_state(dev)
            # Cool-State Detection für cooling-fähige Heizungs-Devices.
            # Sendet das Backend cool_on=True/False sodass die iOS-Tile
            # "Kühlt" sauber anzeigt — auch bei manuellem User-Wechsel
            # über HA. None = unverändert lassen.
            cool_on = self._read_cool_on_state(dev)

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
            if energy_kwh_total is not None:
                payload["energy_kwh_total"] = energy_kwh_total
            if energy_kwh_delta is not None:
                payload["energy_kwh_delta"] = energy_kwh_delta
            if is_on is not None:
                payload["is_on"] = is_on
            if cool_on is not None:
                payload["cool_on"] = cool_on

            # Solver-only extras (Vorlauf-Temp, später T_supply, …).
            # JSONB-Bag im Backend; UI bekommt davon nichts mit. Nur
            # senden wenn mindestens ein Feld einen Wert liefert,
            # sonst Payload nicht aufblähen.
            extra_payload: dict[str, Any] = {}
            for payload_key, conf_key, reader in _SOLVER_EXTRA_FIELDS.get(
                dev.get(CONF_DEVICE_TYPE, ""), []
            ):
                entity_id = dev.get(conf_key, "")
                if not entity_id:
                    continue
                if reader == "temp":
                    value = self._read_temp_c(entity_id)
                else:
                    value = self._read_entity_state(entity_id)
                if isinstance(value, (int, float)):
                    extra_payload[payload_key] = float(value)
            if extra_payload:
                payload["extra"] = extra_payload

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
                    self._last_sent_hash[device_id] = self._payload_hash(payload)
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
            # Hold-Mode: auto (Default) und always rewriten, never skipt.
            mode = (
                dev.get(CONF_ENTITY_CONTROL_HOLD)
                or ENTITY_CONTROL_HOLD_AUTO
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
        return f"{self.api_url}/api/v1/stream"

    async def _run_sse_loop(self) -> None:
        """Reconnecting SSE listener for inbound commands from the backend.

        Auth: `Authorization: Bearer …` (not `?token=…`). The query-param
        form was deprecated 2026-05-27 because URL-embedded tokens leak
        into nginx + reverse-proxy access logs; aiohttp can set headers
        on streamed GETs cleanly so we get the same SSE semantics with
        proper authentication.
        """
        delay = SSE_RECONNECT_INITIAL
        session = aiohttp_client.async_get_clientsession(self.hass)
        while True:
            try:
                async with session.get(
                    self._sse_url(),
                    headers={
                        "Accept": "text/event-stream",
                        "Cache-Control": "no-cache",
                        "Authorization": f"Bearer {self._access_token}",
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
                    _LOGGER.info("Crowdergy SSE connected to %s/api/v1/stream", self.api_url)
                    # Reset the back-off only after we've actually
                    # received data on the body stream. A handshake-
                    # OK / immediate-disconnect cycle would otherwise
                    # spin at the 1-s floor and hammer a down backend.
                    saw_body = False
                    async for raw in resp.content:
                        if not saw_body:
                            saw_body = True
                            delay = SSE_RECONNECT_INITIAL
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
            delay = min(delay * 2, SSE_RECONNECT_MAX)

    async def _handle_ws_message(self, data: dict[str, Any]) -> None:
        # Update liveness clock for the charge-mode hold-loop kill
        # switch — any received message (ping included) counts as
        # "Crowdergy still talking to us". The 15-s server ping is
        # the most common message type when nothing else is changing.
        self._last_sse_event_at = time.time()
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
                    if new_value:
                        # Crowdergize on → force an entity_control write
                        # + start the hold loop using the currently
                        # cached desired state. Without this, the user
                        # sees no HA-side action until the backend
                        # publishes the FIRST is_on *transition*, which
                        # never happens when the device's current state
                        # already matches the solver's decision (common
                        # case: WW physically off, solver decides off,
                        # backend's "publish only on change" guard
                        # suppresses the SSE frame). Result: value_off
                        # never written, hold loop never started.
                        desired_on = bool(self._on_state.get(device_id, False))
                        await self._apply_device_state(device_id, desired_on)
                    else:
                        # Crowdergize off → schreibe einen sicheren
                        # Default-State, statt den letzten AI-Zustand
                        # hängen zu lassen. User-Erwartung 2026-05-30:
                        # SG-Ready bleibt nicht auf „Erhöht", Batterie
                        # nicht auf force-charge. Wallbox-Spezialpfad
                        # (snapshot/restore) lief schon oben.
                        dev = next(
                            (d for d in self.devices
                             if d.get(CONF_DEVICE_ID) == device_id),
                            None,
                        )
                        dev_type = (dev or {}).get(CONF_DEVICE_TYPE, "")
                        if dev_type == "battery":
                            # AI-off Battery-Übergabe: Hold-Loop
                            # canceln (sonst schreibt der weiter den
                            # letzten AI-Wert), dann battery_value_
                            # passive an HA — das übergibt die
                            # Batterie an die PV-Eigenverbrauchs-Logik
                            # des Wechselrichters.
                            #
                            # v3.2.3 (2026-05-31): passive ist ein
                            # Pflichtfeld im Battery-Values-Step;
                            # leere Werte können nur in pre-v3.2.3-
                            # Setups vorkommen und werden tolerant
                            # geskippt (kein neuer Write, kein
                            # Hold-Loop — Inverter macht weiter was
                            # er gerade machte).
                            self._cancel_charge_mode_hold(device_id)
                            passive_val = (
                                (dev or {}).get(CONF_BATTERY_VALUE_PASSIVE, "")
                                or ""
                            )
                            if passive_val:
                                await self._apply_charge_mode(
                                    device_id, passive_val
                                )
                        elif dev_type in ("heating", "warmwater", "generic"):
                            # entity_control auf value_off — sorgt
                            # dafür dass das Gerät definitiv stoppt.
                            # _apply_device_state startet danach den
                            # hold-loop; den wollen wir hier NICHT
                            # → direkt nach dem write die hold-loop
                            # canceln (siehe unten).
                            await self._apply_device_state(device_id, False)
                            # Cooling-side auf off bringen falls cool_on
                            # war (SG-Ready / climate / dedizierter
                            # Kühl-Switch — alle drei Pfade über
                            # _apply_cool_state).
                            try:
                                await self._apply_cool_state(device_id, False)
                            except Exception:
                                # Cool-state ist optional; nicht
                                # blockieren wenn Helper bei diesem
                                # device nichts schreiben kann.
                                pass
                        # Cancel hold-loop NACH dem explicit write,
                        # sodass der letzte write das Letzte ist was
                        # wir auf das entity_control schreiben — danach
                        # ist das Gerät dem User überlassen.
                        self._cancel_hold(device_id)
            if "is_on" in payload:
                new_on = bool(payload["is_on"])
                if self._on_state.get(device_id) != new_on:
                    self._on_state[device_id] = new_on
                    self._sync_field_into_data(device_id, "is_on", new_on)
                    await self._apply_device_state(device_id, new_on)
            # Cooling-side mirror. Worker emittiert cool_on-Transitions
            # für cooling-fähige heating devices. Heat/cool-Mutex ist
            # upstream im Solver enforced — wir vertrauen dem Paar und
            # schreiben durch auf die konfigurierte Kühl-Entity (oder
            # Fallback auf climate.set_hvac_mode wenn die Kühl-Seite
            # sich entity_control teilt + climate.* ist).
            if "cool_on" in payload:
                new_cool = bool(payload["cool_on"])
                if self._cool_state.get(device_id) != new_cool:
                    self._cool_state[device_id] = new_cool
                    self._sync_field_into_data(device_id, "cool_on", new_cool)
                    await self._apply_cool_state(device_id, new_cool)
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
            if action == "set_charge_mode" and device_id:
                # `value is None` ist das "passive" Signal — Backend
                # unterdrückt den Write damit die Inverter-native PV-
                # Priorität kickt. Hold-Loop ALWAYS canceln; ob wir
                # zusätzlich idle pre-writen hängt vom current state ab.
                if value is None:
                    mode_tag = data.get("mode") or "passive"
                    _LOGGER.info(
                        "set_charge_mode passive on %s (mode=%s)",
                        device_id, mode_tag,
                    )
                    self._cancel_charge_mode_hold(device_id)
                    # Pre-write idle nur dann wenn current state der
                    # aktive CHARGE-Wert ist — ohne das hatten wir die
                    # v2.5.2-Regression: nach einer Charge-Session
                    # blieb die input_select bei "Laden" hängen und
                    # der Inverter zog nachts vom Netz weiter. Wenn
                    # current bereits idle ist (Crowdergy hat's eben
                    # neutral) oder die input_select gerade auf
                    # DISCHARGE steht (typisch eine User-seitige HA-
                    # Automation, z.B. Modbus-Discharge-Schreiber),
                    # NICHT überschreiben — passive heißt "Crowdergy
                    # lässt los, du kannst regeln".
                    dev = next(
                        (d for d in self.devices
                         if d.get(CONF_DEVICE_ID) == device_id),
                        None,
                    )
                    if dev is not None:
                        entity_id = dev.get(CONF_ENTITY_CHARGE_MODE, "")
                        current = (
                            self._read_string(entity_id)
                            if entity_id else ""
                        )
                        charge_value = dev.get(
                            CONF_BATTERY_VALUE_CHARGE, ""
                        )
                        if (
                            charge_value
                            and str(current).strip() == str(charge_value).strip()
                        ):
                            idle_value = (
                                dev.get(CONF_BATTERY_VALUE_IDLE)
                                or dev.get(CONF_VALUE_OFF)
                                or ""
                            )
                            if idle_value:
                                _LOGGER.info(
                                    "passive: clearing stuck CHARGE "
                                    "(%s → %s) on %s",
                                    current, idle_value, entity_id,
                                )
                                await self._apply_charge_mode(
                                    device_id, str(idle_value),
                                    schedule_hold=False,
                                )
                else:
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

    async def _apply_charge_mode(
        self, device_id: str, mode: str, *, schedule_hold: bool = True
    ) -> None:
        """Write the device's configured entity_charge_mode entity.

        Two domains supported:
          * `select` / `input_select` — `select.select_option` with the
            mode string as the option. Used by wallbox Lademodus and by
            batteries whose mode-entity happens to be a select.
          * `number` / `input_number` — `number.set_value` with the
            mode string parsed as a float. Used by batteries whose
            mode-entity is a number taking e.g. +5000 / 0 / -5000 W.

        `schedule_hold=True` (the default for fresh SSE commands)
        replaces any existing hold-loop task for this device with one
        that re-writes the same value every `CHARGE_MODE_HOLD_INTERVAL`
        seconds. The loop is what keeps inverters that reset their
        mode after ~15 s of silence (Kostal, BYD, some Sungrow firmware)
        actually obeying Crowdergy's commanded mode. The hold loop
        itself calls this method with `schedule_hold=False` so it
        doesn't keep reseeding its own task.
        """
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
                "configured", device_id,
            )
            return
        domain = entity_id.split(".", 1)[0]
        # First write (fresh SSE command) keeps the WARNING so the
        # user sees Crowdergy acting; the hold-loop rewrites drop to
        # DEBUG so a healthy 15-s cadence doesn't flood the HA log.
        log_level = logging.WARNING if schedule_hold else logging.DEBUG
        _LOGGER.log(log_level, "set_charge_mode: %s → %s", entity_id, mode)
        # Update the held value BEFORE the actual write so any in-
        # flight old hold-tick that wakes up between our cancel and
        # its next read still sees the new value rather than re-
        # writing the previous one. Race-safe even if `_start_charge_
        # mode_hold` below loses the cancel race.
        if schedule_hold:
            self._held_charge_mode[device_id] = mode
        try:
            if domain in ("select", "input_select"):
                await self.hass.services.async_call(
                    domain, "select_option",
                    {"entity_id": entity_id, "option": mode},
                    blocking=True,
                )
            elif domain in ("number", "input_number"):
                try:
                    value = float(mode)
                except (TypeError, ValueError):
                    _LOGGER.warning(
                        "set_charge_mode: '%s' is not numeric, can't write "
                        "to %s entity %s", mode, domain, entity_id,
                    )
                    return
                await self.hass.services.async_call(
                    domain, "set_value",
                    {"entity_id": entity_id, "value": value},
                    blocking=True,
                )
            else:
                _LOGGER.warning(
                    "set_charge_mode: entity %s domain %s not supported "
                    "(expected select / number)", entity_id, domain,
                )
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("set_charge_mode service call failed: %s", err)

        if schedule_hold:
            self._start_charge_mode_hold(device_id)

    def _start_charge_mode_hold(self, device_id: str) -> None:
        """Replace any existing charge_mode hold task for this device.
        Idempotent — cancelling a not-yet-started task is a no-op.

        Respektiert ENTITY_CONTROL_HOLD: auto/always → hold-loop läuft,
        never → User-Opt-Out, kein Re-Write nach dem ersten Apply.
        """
        prev = self._charge_mode_hold_tasks.pop(device_id, None)
        if prev is not None and not prev.done():
            prev.cancel()
        dev = next(
            (d for d in self.devices if d.get(CONF_DEVICE_ID) == device_id),
            None,
        )
        if dev is None:
            return
        mode = dev.get(CONF_ENTITY_CONTROL_HOLD) or ENTITY_CONTROL_HOLD_AUTO
        if mode == ENTITY_CONTROL_HOLD_NEVER:
            return
        self._charge_mode_hold_tasks[device_id] = self.hass.async_create_task(
            self._charge_mode_hold_loop(device_id),
            name=f"theothergas_charge_mode_hold_{device_id}",
        )

    def _cancel_charge_mode_hold(self, device_id: str) -> None:
        """Stop the per-device charge_mode hold and drop the cached
        held value. Called on `passive` commands (worker signals
        inverter should follow its own logic) and on device removal.
        """
        self._held_charge_mode.pop(device_id, None)
        task = self._charge_mode_hold_tasks.pop(device_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def _charge_mode_hold_loop(self, device_id: str) -> None:
        """Re-write the last commanded charge_mode every
        CHARGE_MODE_HOLD_INTERVAL seconds. Bails when the held value
        is cleared (cancel via `_cancel_charge_mode_hold` — called on
        `passive` from the worker, on `_restore_charge_mode` when
        Crowdergize toggles off, on device removal, and on coordinator
        shutdown) OR when the SSE channel has been silent past
        SSE_STALE_THRESHOLD_S (Crowdergy backend / network unreachable
        → the inverter's native logic regains control rather than
        being stuck on the last command forever).

        Deliberately does NOT gate on `_active_state`. The user can
        manually tap Wallbox modes (Aus / An / Solar) while AI is off,
        and those still need to hold against firmwares that auto-
        revert. AI-off scenarios are handled by the explicit cancel
        paths above, not by an inline is_active check.
        """
        try:
            await asyncio.sleep(CHARGE_MODE_HOLD_INITIAL_DELAY)
            while True:
                mode = self._held_charge_mode.get(device_id)
                if mode is None:
                    return
                # Liveness check: if Crowdergy isn't talking to us,
                # stop holding so the user's inverter can take over.
                # When SSE reconnects, the next MPC tick re-establishes
                # the hold via a fresh `set_charge_mode` event.
                staleness = time.time() - self._last_sse_event_at
                if staleness > SSE_STALE_THRESHOLD_S:
                    _LOGGER.warning(
                        "charge_mode hold: bailing for %s — Crowdergy "
                        "SSE silent for %.1fs (> %ds). Inverter "
                        "native logic resumes.",
                        device_id, staleness, SSE_STALE_THRESHOLD_S,
                    )
                    self._held_charge_mode.pop(device_id, None)
                    return
                await self._apply_charge_mode(
                    device_id, mode, schedule_hold=False
                )
                await asyncio.sleep(CHARGE_MODE_HOLD_INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "charge_mode hold loop for %s crashed", device_id
            )

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

    async def _apply_cool_state(self, device_id: str, cool_on: bool) -> None:
        """Schreibt den Kühl-State für ein heating-Device. Drei
        Dispatch-Patterns nach Priorität:

          1. Dedicated `entity_cool_control` configured → write
             `value_cool_on` / `value_cool_off` against it (mirror of
             the heating-side _apply_device_state path).
          2. No `entity_cool_control` AND the heating-side
             `entity_control` is a `climate.*` entity → call
             `climate.set_hvac_mode("cool")` or `"off"` against it
             (single HA entity handles both modes).
          3. Otherwise: skip — the device isn't actually wired for
             cooling at the HA layer, log debug and move on.
        """
        dev = next(
            (d for d in self.devices if d.get(CONF_DEVICE_ID) == device_id),
            None,
        )
        if dev is None:
            return
        cool_entity = dev.get(CONF_ENTITY_COOL_CONTROL, "") or ""
        if cool_entity:
            domain = cool_entity.split(".", 1)[0]
            raw_value = dev.get(
                CONF_VALUE_COOL_ON if cool_on else CONF_VALUE_COOL_OFF, ""
            )
            await self._write_entity_control(
                cool_entity, domain, raw_value, cool_on, verbose=True,
            )
            return
        # Climate-domain single-entity fallback: heating and cooling
        # share the same `entity_control`, and the connector
        # translates the mode via `climate.set_hvac_mode`.
        heat_entity = dev.get(CONF_ENTITY_CONTROL, "") or ""
        if heat_entity.startswith("climate."):
            mode = "cool" if cool_on else "off"
            _LOGGER.warning(
                "set_hvac_mode: %s → %s (cool side)", heat_entity, mode,
            )
            try:
                await self.hass.services.async_call(
                    "climate", "set_hvac_mode",
                    {"entity_id": heat_entity, "hvac_mode": mode},
                    blocking=True,
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception(
                    "set_hvac_mode failed for %s: %s", heat_entity, err,
                )
            return
        _LOGGER.debug(
            "Device %s flipped cool_on but has no cooling entity "
            "(entity_cool_control empty, entity_control not climate.*)",
            device_id,
        )

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
            elif domain == "water_heater":
                # HA's water_heater integration uses set_operation_mode
                # with operation_mode parameter (vs climate's hvac_mode).
                # Typical operation_modes are "eco" / "performance" /
                # "electric" / "off" — the user maps these via
                # value_on / value_off in the device_values step just
                # like with climate.
                await self.hass.services.async_call(
                    domain, "set_operation_mode",
                    {"entity_id": entity_id, "operation_mode": str(raw_value)},
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
        mode = dev.get(CONF_ENTITY_CONTROL_HOLD) or ENTITY_CONTROL_HOLD_AUTO
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
        self._cancel_charge_mode_hold(device_id)
        for d in (
            self._active_state,
            self._on_state,
            self._prev_energy_kwh,
            self._prev_energy_kwh_discharged,
            self._last_sent_payload,
            self._last_send_at,
            self._last_sent_hash,
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
        against. Returns a string (HA states are strings) or None
        wenn wir's nicht entscheiden können — dann re-write die
        Hold-Loop blind im 'always' Modus (es gab früher einen
        'auto' Modus der bei None passiv blieb; collapse zu 'always'
        seit v1.20.0, also kein Branching mehr nötig).
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

