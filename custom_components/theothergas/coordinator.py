"""DataUpdateCoordinator for Crowdergy Connector."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import timedelta
from typing import Any

# aiohttp + aiohttp_client raus seit FEAT-5 Phase B (2026-06-09) —
# Stream-Reader nutzt sie drüben in sse_client.py.
import httpx
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_API_URL,
    OPT_CONSENT_REMOTE_CONTROL,
    OPT_CONSENT_TELEMETRY,
    CONF_ENTITY_BATTERY_MODE,
    CONF_VALUE_BATTERY_MODE_ACTIVE,
    CONF_VALUE_BATTERY_MODE_PASSIVE,
    CONF_ENTITY_BATTERY_POWER_SETPOINT,
    CONF_BATTERY_SETPOINT_INVERT_SIGN,
    CONF_DEVICE_ID,
    CONF_DEVICE_TYPE,
    CONF_DEVICES,
    CONF_ENTITY_CHARGE_MODE,
    CONF_ENTITY_CLIMATE,
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
    CONF_ENTITY_VORLAUF_SETPOINT,
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
    CONF_VEHICLE_STATUS_VALUE_ERROR,
    CONF_VEHICLE_STATUS_VALUE_PLUGGED,
    CONF_VEHICLE_STATUS_VALUE_UNPLUGGED,
    DOMAIN,
    ENTITY_CONTROL_HOLD_ALWAYS,
    ENTITY_CONTROL_HOLD_AUTO,
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

STATE_RESYNC_INTERVAL = 90.0
"""Periodischer Backstop für SSE-Drops (v3.5.0+). Pollt alle 90 s
GET /api/v1/devices, vergleicht Backend-State (is_active, is_on,
cool_on) mit dem lokalen Cache und re-applyt bei Drift via
`_apply_device_state` / `_apply_cool_state`.

Hintergrund 2026-06-02 (zillmann-Case): SSE ist fire-and-forget +
Backend publisht nur bei state-Transitions, nicht idempotent. Wenn
der Connector zum Publish-Zeitpunkt nicht subscribed ist (Netzwerk-
Flap, HA-Restart, NAT-Idle-Timeout), geht der Solver-Befehl verloren
und wird nie repliziert. Ergebnis: WP heizte 16 min weiter über die
Komfortzone hinaus weil das OFF nie ankam.

90 s = Worst-Case-Drift-Fenster nach Solver-Decision. Kürzer wäre
besser für UX, kostet aber Backend-Last. 90 s passt zu den anderen
periodischen Loops (Telemetry 30 s, Mirror 60 s)."""

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

# SSE-Konstanten (Reconnect-Backoff, Read-Timeout) leben seit FEAT-5
# Phase B (2026-06-09) in sse_client.py.


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
        # v3.5.1: httpx.AsyncClient + manifest read sind blocking I/O
        # (SSL-Cert-Load synchron) — HA's event-loop checker meckert
        # ab 2024.x. Beide werden in `async_init()` deferred angelegt;
        # bis dahin als Placeholder None / "0.0.0" damit Attribut
        # existiert falls etwas vor async_init darauf zugreift.
        self._client: httpx.AsyncClient | None = None  # type: ignore[assignment]
        self._unsub_listeners: list[Any] = []
        self._entity_to_devices: dict[str, list[str]] = {}
        # FEAT-5 Phase B (2026-06-09): SSE-Stream-Reader in sse_client.py
        # extrahiert. Coordinator besitzt jetzt nur noch die SSEClient-
        # Instanz + einen Consumer-Task der Frames aus der Queue holt
        # und auf `_handle_ws_message` mapped. Lifecycle (start/stop)
        # bleibt im Coordinator. Vorteile: testbar ohne Apply-Stack;
        # Backpressure-Queue (Cap 512) statt direkter Coupling.
        from .sse_client import SSEClient
        self._sse_client: SSEClient | None = None
        self._sse_consumer_task: asyncio.Task | None = None
        self._SSEClient = SSEClient  # for late instantiation in async_init
        # FEAT-5 Phase D (2026-06-09): TelemetryComposer hält die 3
        # Background-Loops (heartbeat, device-mirror, state-resync) +
        # bootstrap/outdoor-temp-helpers. Coordinator delegiert die
        # vorigen `_run_*_loop`-Methoden und `_bootstrap_active_state`
        # / `_push_outdoor_temp` an `self._composer.*`.
        from .telemetry_composer import TelemetryComposer
        self._composer = TelemetryComposer(self)
        # Cluster A Connector (2026-06-09): single-flight Lock + CAS für
        # _refresh_access_token. Vorher konnten parallele 401s (Telemetry-
        # PATCH + State-Resync GET + Outdoor-Temp POST treffen gleichzeitig
        # nach Token-Expiry) jeweils einen eigenen /auth/refresh-Call
        # starten — Backend invalidiert das alte Refresh-Token per Use,
        # nur einer gewinnt, der Rest hat einen invaliden Refresh-Token →
        # Logout-Kaskade. Mit Lock: erste Caller refresht, alle weiteren
        # warten am Lock und sehen dann das neue Token via CAS-Check.
        self._refresh_lock: asyncio.Lock = asyncio.Lock()
        # v2.5.4: dedicated liveness ping. Decoupled from the
        # per-device telemetry stream so a fully idle home no longer
        # has to PATCH N devices every 30 s purely to keep iOS's
        # connection dot green. See `_heartbeat_loop` docstring.
        self._heartbeat_task: asyncio.Task | None = None
        self._device_mirror_task: asyncio.Task | None = None
        self._state_resync_task: asyncio.Task | None = None
        # FEAT-5 Phase A (2026-06-09): per-Device-State-Cache wandert
        # in eine eigene `DeviceStateMirror`-Dataclass. Die alten
        # Attribut-Namen (`_active_state`, `_on_state`, `_cool_state`,
        # `_hold_tasks`, `_charge_mode_hold_tasks`, `_held_charge_mode`,
        # `_last_sse_event_at`) bleiben über @property-Shims weiter
        # zugreifbar damit die ~250 bestehenden Call-Sites unverändert
        # laufen. Phase B migriert die Sites pro Cluster auf typed
        # Accessor-Methoden. Siehe state_mirror.py.
        from .state_mirror import DeviceStateMirror
        self.state: DeviceStateMirror = DeviceStateMirror()
        # `_last_sse_event_at` ist jetzt auch im DeviceStateMirror —
        # siehe @property-Shim weiter unten.
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
        # Manifest-Read deferred (siehe Kommentar oben bei _client).
        self._connector_version: str = "0.0.0"
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
        """SSE-Reader + Message-Consumer starten. Reader liest die
        Stream-Bytes und legt JSON-Frames in eine Queue; Consumer holt
        sie raus und dispatcht via `_handle_ws_message`."""
        if not self._user_id:
            _LOGGER.warning("No user_id stored — skipping WS listener setup")
            return
        if self._sse_client is None:
            self._sse_client = self._SSEClient(
                hass=self.hass,
                api_url=self.api_url,
                get_token=lambda: self._access_token,
                refresh_token=self._refresh_access_token,
            )
        self._sse_client.start(task_name=f"{DOMAIN}_sse_listener")
        if (
            self._sse_consumer_task is None
            or self._sse_consumer_task.done()
        ):
            self._sse_consumer_task = self.hass.async_create_background_task(
                self._sse_consume_loop(),
                name=f"{DOMAIN}_sse_consumer",
            )

    async def _sse_consume_loop(self) -> None:
        """Consumer-Loop: nimmt Frames aus der SSEClient-Queue und
        dispatcht jeden auf `_handle_ws_message`. Exceptions in einem
        einzelnen Frame werden gelogged + geschluckt, damit ein
        kaputtes Apply nicht den Stream blockt."""
        assert self._sse_client is not None
        while True:
            try:
                msg = await self._sse_client.messages.get()
                await self._handle_ws_message(msg)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("Failed to handle SSE event: %s", err)

    async def async_init(self) -> None:
        """v3.5.1 — Defered blocking I/O aus dem event loop.

        HA 2024.x flagt zwei Operationen in `__init__` als blocking:
        - `httpx.AsyncClient(...)` laedt das CA-Bundle synchron
          (load_verify_locations → blocking ssl-init)
        - `_load_manifest_version()` macht `open(... manifest.json)`

        Beides hier per `async_add_executor_job` in einen Worker-Thread
        ausgelagert, sodass der event loop frei bleibt. Wird einmalig
        in `__init__.py:async_setup_entry` direkt nach Coordinator-
        Konstruktion aufgerufen, bevor die ersten Refreshes/Listeners
        laufen.
        """
        self._client = await self.hass.async_add_executor_job(
            lambda: httpx.AsyncClient(base_url=self.api_url, timeout=15.0)
        )
        self._connector_version = await self.hass.async_add_executor_job(
            _load_manifest_version
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

    def start_state_resync(self) -> None:
        """Start the SSE-drop-Backstop polling loop (v3.5.0+).
        Idempotent. Holt alle STATE_RESYNC_INTERVAL Sekunden den
        autoritativen Device-State vom Backend und repariert
        Cache-Drift via _apply_device_state / _apply_cool_state."""
        if self._state_resync_task and not self._state_resync_task.done():
            return
        self._state_resync_task = self.hass.async_create_background_task(
            self._run_state_resync_loop(),
            name=f"{DOMAIN}_state_resync",
        )

    async def _run_heartbeat_loop(self) -> None:
        """Delegate auf TelemetryComposer (FEAT-5 Phase D, 2026-06-09)."""
        await self._composer.heartbeat_loop()

    async def _run_device_mirror_loop(self) -> None:
        """Delegate auf TelemetryComposer (FEAT-5 Phase D, 2026-06-09)."""
        await self._composer.device_mirror_loop()

    async def _run_state_resync_loop(self) -> None:
        """Delegate auf TelemetryComposer (FEAT-5 Phase D, 2026-06-09)."""
        await self._composer.state_resync_loop()


    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token}",
            # Lets the backend stamp users.connector_version so the iOS
            # app can compare against min_connector_version and surface
            # an "Update verfügbar" banner. Read from manifest.json at
            # config-flow time and threaded through `entry.data`.
            "X-Crowdergy-Connector-Version": self._connector_version,
        }

    async def _refresh_access_token(self, *, seen_token: str | None = None) -> bool:
        """Single-flight Refresh mit Compare-and-Swap.

        Cluster A Connector (2026-06-09): mehrere parallele 401-Responses
        konnten vorher jeweils ein eigenes /auth/refresh feuern → das
        Backend invalidiert das alte Refresh-Token, nur einer gewinnt,
        Rest hat ungültige Token → kaskadiertem Logout. Jetzt:

        - `seen_token`: das `_access_token`, das der Caller bei seinem
          401 gesehen hat. Wenn beim Lock-Aquire ein anderer Thread das
          Token bereits rotiert hat (CAS missed), refresh wir nicht
          nochmal — der Caller sollte sein Original-Request mit dem
          aktuellen Token retryen.
        """
        async with self._refresh_lock:
            if seen_token is not None and self._access_token != seen_token:
                # Anderer Caller hat während des Lock-Wait bereits
                # rotiert — wir nehmen das neue Token kommentarlos.
                return True
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
        # Snapshot des aktuellen Tokens für CAS — wenn ein anderer
        # Caller während unseres 401-Roundtrips bereits rotiert, lassen
        # wir den nächsten Refresh dann sausen.
        seen_token = self._access_token
        response = await self._client.request(
            method, path, headers=self._auth_headers(), **kwargs
        )
        if response.status_code == 401:
            if await self._refresh_access_token(seen_token=seen_token):
                response = await self._client.request(
                    method, path, headers=self._auth_headers(), **kwargs
                )
        return response

    async def delete_device_backend(self, device_id: str) -> bool:
        """Backend-DELETE für ein Device, mit Auth-Refresh.

        Cluster B Connector (2026-06-09): vorher hatte
        `async_remove_config_entry_device` in `__init__.py` einen
        eigenen httpx-Client OHNE 401-Refresh-Pfad → bei abgelaufenem
        Token blieb das Device als Orphan im Backend. Jetzt geht's
        durch denselben authentifizierten Pfad wie alle anderen
        Backend-Calls.
        """
        try:
            response = await self._authenticated_request(
                "DELETE", f"/api/v1/devices/{device_id}"
            )
        except httpx.RequestError as err:
            _LOGGER.warning(
                "Backend delete request for %s failed transport: %s",
                device_id, err,
            )
            return False
        # 404 = backend hat das schon nicht mehr → für unsere Zwecke ok.
        if response.status_code in (200, 204, 404):
            return True
        _LOGGER.warning(
            "Backend delete for %s returned %s",
            device_id, response.status_code,
        )
        return False

    async def async_shutdown(self) -> None:
        for unsub in self._unsub_listeners:
            unsub()
        self._unsub_listeners.clear()
        if self._sse_consumer_task and not self._sse_consumer_task.done():
            self._sse_consumer_task.cancel()
            try:
                await self._sse_consumer_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._sse_client is not None:
            await self._sse_client.stop()
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
        if self._state_resync_task and not self._state_resync_task.done():
            self._state_resync_task.cancel()
            try:
                await self._state_resync_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        for task in list(self.state.hold_tasks.values()):
            task.cancel()
        self.state.hold_tasks.clear()
        for task in list(self.state.charge_mode_hold_tasks.values()):
            task.cancel()
        self.state.charge_mode_hold_tasks.clear()
        self.state.held_charge_mode.clear()
        await self._client.aclose()

    @property
    def last_sse_event_at(self) -> float:
        """Public Accessor für externe Reader (z.B. binary_sensor) —
        bleibt als API-Stable-Surface auch nach FEAT-5 Phase-A-Migration
        auf `self.state.last_sse_event_at`."""
        return self.state.last_sse_event_at

    # FEAT-5 Phase A Finish (2026-06-09): @property-Shims für die alten
    # Coordinator-State-Dicts entfernt. 47 Call-Sites lesen/schreiben
    # jetzt direkt auf `self.state.*` (typed accessors auf DeviceStateMirror).
    # Damit reduziert sich coordinator.py um ~80 Zeilen und das
    # State-Mirror-Modul ist alleinige Source-of-Truth.

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
        """Delegate auf TelemetryComposer (FEAT-5 Phase D, 2026-06-09)."""
        await self._composer.bootstrap_active_state()

    async def _push_outdoor_temp(self) -> None:
        """Delegate auf TelemetryComposer (FEAT-5 Phase D, 2026-06-09)."""
        await self._composer.push_outdoor_temp()

    def _consent(self, option_key: str) -> bool:
        """Box-Consent-Gate (Phase 4). Default True — Self-Hosted-
        Installationen ohne Box-Manager bleiben unverändert; auf der
        Box schreibt `box_set_consent` die Flags in die Entry-Options."""
        return bool(self.entry.options.get(option_key, True))

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        if not self._consent(OPT_CONSENT_TELEMETRY):
            # Consent entzogen: KEIN Outdoor-Temp-Push, KEINE Telemetrie-
            # PATCHes. Lokale Entity-Werte bleiben auf letztem Stand,
            # damit HA-seitig nichts kaputt aussieht.
            return self.data or {}

        if not self.state.active_state_bootstrapped:
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
            # aircon-Fallback: bei Split-AC ist climate.current_temperature
            # die echte Raumtemp. Geräte aus v3.6.0 (vor v3.6.2-Auto-Copy)
            # haben entity_current_temp leer → ohne Fallback bleibt Tile
            # ohne Temperatur. Heating (Stiebel & Co.) bleibt
            # ausgeschlossen, weil dort climate.current_temperature die
            # Vorlauf-Temp ist und nicht ins Thermomodell darf.
            if (
                not entity_current_temp
                and dev.get(CONF_DEVICE_TYPE) == "aircon"
            ):
                entity_current_temp = dev.get(CONF_ENTITY_CLIMATE, "")
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
            # underlying power_kw convention: POSITIVE = energy flowed
            # FROM the device INTO the home. For one-direction devices
            # (heating/wallbox/solar/…) the delta is just the device's
            # consumption (always positive, except solar where the
            # mapped counter is PV-production).
            #
            # **E2E-Konvention 2026-06-11 (Connector v3.21.3):** für
            # Grid + Battery sind die beiden Counter explizit:
            #   * `entity_energy_total`           = Bezug-Zähler (Grid)
            #                                       Entladen-Zähler (Battery)
            #     = kWh die VOM Gerät INS HAUS geflossen sind
            #   * `entity_energy_discharged_total` = Einspeisung-Zähler (Grid)
            #                                        Laden-Zähler (Battery)
            #     = kWh die AUS DEM HAUS INS Gerät geflossen sind
            #
            # `delta = in_delta − out_delta` → positiv = Bezug/Entladen
            # (Energie kam ins Haus), negativ = Einspeisung/Laden
            # (Energie ging raus). Backend speichert das signed delta;
            # `kwh_in`-Sum = Bezug, `kwh_out`-Sum = Einspeisung.
            #
            # **Breaking-Hinweis für Bestand (vor v3.21.3):** die Labels
            # waren vorher generisch („Energy counter (kWh)"), die Math
            # war `out − in`. User müssen ihre Entity-Mappings einmalig
            # tauschen: was bisher unter `entity_energy_total` lag,
            # gehört jetzt unter `entity_energy_discharged_total` und
            # umgekehrt. Siehe HACS-Release-Notes v3.21.3.
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
            # E2E-Konvention 2026-06-11 (v3.21.4): zwei UNSIGNED
            # Felder pro Tick, eines pro Richtung. Backend ≥ heutiger
            # Deploy weiß was zu tun ist (deriviert signed
            # energy_kwh_delta = in - out für Backward-Compat).
            # `energy_kwh_delta` wird in v3.21.4 weiterhin mitgesendet
            # damit ältere Backend-Versionen die signed Form lesen
            # können (Übergangs-Schutz; einer der beiden Pfade gewinnt
            # je nach Backend-Stand).
            energy_kwh_in_delta_out: float | None = in_delta
            energy_kwh_out_delta_out: float | None = out_delta
            if energy_kwh_in_delta_out is None and energy_kwh_out_delta_out is None:
                # Kein Counter mapped — nichts zu senden.
                energy_kwh_delta = None
            else:
                energy_kwh_delta = (
                    (energy_kwh_in_delta_out or 0.0)
                    - (energy_kwh_out_delta_out or 0.0)
                )
            # invert_power_sign muss alle Energie-Felder konsistent
            # spiegeln. Vor 2026-05-30 wurde nur power_kw invertiert →
            # kWh-Bezug/Einspeisung kamen vertauscht beim Backend an.
            # Inversion = "Counter A ist eigentlich Counter B" → in/out
            # tauschen, und das daraus deriviert signed delta wird
            # automatisch negiert.
            if dev.get(CONF_INVERT_POWER_SIGN):
                energy_kwh_in_delta_out, energy_kwh_out_delta_out = (
                    energy_kwh_out_delta_out,
                    energy_kwh_in_delta_out,
                )
                if energy_kwh_delta is not None:
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
            # Neue explizite Felder (Backend ≥ heutiger Deploy
            # liest diese vorrangig; ältere Backends ignorieren sie
            # weil Pydantic mit `extra="forbid"` nur die deklarierten
            # Felder annimmt — der signed energy_kwh_delta deckt den
            # Fall ab).
            if energy_kwh_in_delta_out is not None:
                payload["energy_kwh_in_delta"] = energy_kwh_in_delta_out
            if energy_kwh_out_delta_out is not None:
                payload["energy_kwh_out_delta"] = energy_kwh_out_delta_out
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

            # v3.5.2: v3.4.6's Auto-Routing von `climate.current_temperature`
            # → `vorlauf_temp_c` ist hier raus. War zu aggressiv:
            # echte Klimaanlagen melden `current_temperature` als
            # ECHTE RAUMTEMP, nur Stiebel-/FBH-WPs reporten dort Vorlauf.
            # Default-Behaviour ist jetzt wieder „climate.current_temperature
            # → current_temp_c (Raumtemp)". User mit Stiebel-Vorlauf-via-
            # climate können einen separaten Vorlauf-Sensor unter
            # `entity_vorlauf_temp_c` konfigurieren (typisch
            # `sensor.warmepumpe_actual_temperature_hk1` etc.).

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
                "is_active": self.state.active_state.get(device_id, False),
                "is_on": self.state.on_state.get(device_id, False),
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
            if not self.state.active_state.get(device_id, False):
                continue
            if device_id not in self.state.on_state:
                continue  # never commanded — don't synthesise a write
            task = self.state.hold_tasks.get(device_id)
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
                device_id, self.state.on_state[device_id]
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
    # SSE-Stream-Reader extrahiert nach `sse_client.py` (FEAT-5 Phase B,
    # 2026-06-09). Coordinator hält nur noch SSEClient-Instanz +
    # Consumer-Task; das Stream-Reading + Reconnect-Backoff + Auth-
    # Refresh-Trigger leben drüben. Vorteile: testbar ohne Apply-Stack,
    # 512-Slot-Queue als Backpressure gegen hängende Apply-Calls.

    async def _handle_ws_message(self, data: dict[str, Any]) -> None:
        # Update liveness clock for the charge-mode hold-loop kill
        # switch — any received message (ping included) counts as
        # "Crowdergy still talking to us". The 15-s server ping is
        # the most common message type when nothing else is changing.
        self.state.last_sse_event_at = time.time()
        # Box-Consent-Gate (Phase 4): ohne Remote-Control-Consent werden
        # eingehende Steuer-Frames komplett ignoriert — keine Entity-
        # Writes, keine Hold-Loops, kein Cache-Sync. Liveness-Clock oben
        # bleibt aktuell (der Stream selbst ist kein Steuer-Akt).
        if not self._consent(OPT_CONSENT_REMOTE_CONTROL):
            return
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
            # v3.9.2 (2026-06-04): manuelle App-Befehle kommen als
            # telemetry-Frames mit `is_on`/`is_active`. Ohne dieses Log
            # war „kommt manueller Befehl durch?"-Diagnose blind —
            # einzige Signatur war ein späterer state-resync-Reapply.
            # 2026-06-09 (Cluster D): von WARNING → DEBUG runter — bei
            # 10+ Devices und 30s Heartbeat-Echos lief das auf ~1200
            # WARNINGs/Stunde und wuchs home-assistant.log unnötig.
            # Aktivierung via `logger:` config oder ENV `LOG_LEVEL=DEBUG`.
            if any(k in payload for k in ("is_active", "is_on", "cool_on", "vorlauf_setpoint_c")):
                _LOGGER.debug(
                    "Crowdergy SSE telemetry frame: device=%s is_active=%s is_on=%s cool_on=%s vorlauf=%s",
                    device_id,
                    payload.get("is_active"),
                    payload.get("is_on"),
                    payload.get("cool_on"),
                    payload.get("vorlauf_setpoint_c"),
                )
            if "is_active" in payload:
                new_value = bool(payload["is_active"])
                if self.state.active_state.get(device_id) != new_value:
                    self.state.active_state[device_id] = new_value
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
                        desired_on = bool(self.state.on_state.get(device_id, False))
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
                            # v3.8.0 (2026-06-02) AI-off Battery-Übergabe:
                            # Lademodus auf "Passiv" schreiben → HA-
                            # Automation lässt den Setpoint los, WR
                            # übernimmt PV-Native-Priority.
                            self._cancel_charge_mode_hold(device_id)
                            await self._apply_battery_setpoint(
                                device_id, "passive", 0.0,
                            )
                        elif dev_type == "wallbox":
                            # Cluster B Connector (2026-06-09): Finding
                            # #13 — wenn der User KEIN charge_mode_value_
                            # crowdergy gesetzt hat, hatte AI-off keinen
                            # Cleanup-Pfad für die Wallbox. Der Solver-
                            # Lademodus blieb damit hängen. Charge-Mode-
                            # Hold-Loop hier zumindest abräumen damit der
                            # User die Wallbox manuell übernehmen kann.
                            # (Wenn crowdergy_value gesetzt war, ist
                            # snapshot/restore oben schon gelaufen.)
                            self._cancel_charge_mode_hold(device_id)
                        elif dev_type in ("heating", "warmwater", "aircon", "generic"):
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
            # v3.6.4: cool_on VOR is_on verarbeiten — `_cool_state`
            # muss gesetzt sein bevor `_apply_device_state(False)` läuft,
            # sonst sieht der Skip-Guard auf der heat-side `_cool_state`
            # als False und schreibt "off" auf die climate-Entity die
            # gerade vom cool-Pfad mit "cool" gefüllt wurde.
            if "cool_on" in payload:
                new_cool = bool(payload["cool_on"])
                if self.state.cool_state.get(device_id) != new_cool:
                    self.state.cool_state[device_id] = new_cool
                    self._sync_field_into_data(device_id, "cool_on", new_cool)
                    await self._apply_cool_state(device_id, new_cool)
            if "is_on" in payload:
                new_on = bool(payload["is_on"])
                if self.state.on_state.get(device_id) != new_on:
                    self.state.on_state[device_id] = new_on
                    self._sync_field_into_data(device_id, "is_on", new_on)
                    await self._apply_device_state(device_id, new_on)
            # Phase 2b (2026-06-02): Vorlauf-Setpoint von modulierenden
            # Heizungen. Backend sendet pro Solver-Tick °C; wir dispatchen
            # via climate.set_temperature gegen die User-konfigurierte
            # entity_vorlauf_setpoint. Skip wenn keine Entity gemapped
            # ist (User darf vorerst weiter on/off-only fahren).
            if "vorlauf_setpoint_c" in payload:
                try:
                    setpoint_val = float(payload["vorlauf_setpoint_c"])
                except (ValueError, TypeError):
                    setpoint_val = None
                if setpoint_val is not None:
                    await self._apply_vorlauf_setpoint(device_id, setpoint_val)
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
            # Frame-Logging: alle action-relevanten Keys (außer
            # Boilerplate). Vorgängerversion printete nur `value`, was
            # für Battery-Frames immer None ist (Battery liest `mode` +
            # `setpoint_kw` aus separaten Keys) — das war misleading bei
            # „kommt Steuerung durch"-Diagnose. v3.9.2 (2026-06-04).
            payload_keys = {
                k: v for k, v in data.items()
                if k not in ("type",)
            }
            _LOGGER.warning("Crowdergy SSE command frame: %s", payload_keys)
            if action == "set_charge_mode" and device_id:
                # Dispatch nach Device-Typ:
                #   * battery  → Phase 3 Option D: Lademodus-Select +
                #                Power-Setpoint-Number
                #   * wallbox  → Lademodus-Select (Mode-String-Dispatch)
                dev = next(
                    (d for d in self.devices
                     if d.get(CONF_DEVICE_ID) == device_id),
                    None,
                )
                dev_type = (dev or {}).get(CONF_DEVICE_TYPE, "")
                if dev_type == "battery":
                    mode = data.get("mode") or "passive"
                    setpoint_kw = data.get("setpoint_kw")
                    await self._apply_battery_setpoint(
                        device_id, mode,
                        float(setpoint_kw) if setpoint_kw is not None else 0.0,
                    )
                else:
                    # Wallbox: bisher unverändert.
                    if value is None:
                        self._cancel_charge_mode_hold(device_id)
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
            self.state.held_charge_mode[device_id] = mode
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
        prev = self.state.charge_mode_hold_tasks.pop(device_id, None)
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
        # 2026-06-09 (Cluster B Connector Review #11): von
        # async_create_task auf async_create_background_task — Symmetrie
        # mit _hold_tasks und damit HA's Shutdown-Tracker beide
        # Hold-Loop-Familien gleich behandelt.
        self.state.charge_mode_hold_tasks[device_id] = self.hass.async_create_background_task(
            self._charge_mode_hold_loop(device_id),
            name=f"theothergas_charge_mode_hold_{device_id}",
        )

    def _cancel_charge_mode_hold(self, device_id: str) -> None:
        """Stop the per-device charge_mode hold and drop the cached
        held value. Called on `passive` commands (worker signals
        inverter should follow its own logic) and on device removal.
        """
        self.state.held_charge_mode.pop(device_id, None)
        task = self.state.charge_mode_hold_tasks.pop(device_id, None)
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
                mode = self.state.held_charge_mode.get(device_id)
                if mode is None:
                    return
                # Liveness check: if Crowdergy isn't talking to us,
                # stop holding so the user's inverter can take over.
                # When SSE reconnects, the next MPC tick re-establishes
                # the hold via a fresh `set_charge_mode` event.
                staleness = time.time() - self.state.last_sse_event_at
                if staleness > SSE_STALE_THRESHOLD_S:
                    _LOGGER.warning(
                        "charge_mode hold: bailing for %s — Crowdergy "
                        "SSE silent for %.1fs (> %ds). Inverter "
                        "native logic resumes.",
                        device_id, staleness, SSE_STALE_THRESHOLD_S,
                    )
                    self.state.held_charge_mode.pop(device_id, None)
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

        # v3.6.4: bei climate.* teilen heat- und cool-Pfad dieselbe
        # Entity. Wenn cool-side gerade live ist (cool_on=True), dann
        # darf die heat-side hier KEIN "off" mehr schreiben — das war
        # der „AC geht nach 2 Sek aus"-Bug: Backend emittiert is_on=False
        # (oft als initial-frame für dieses Device, _on_state war None)
        # nach dem cool-Tick, _apply_device_state(False) findet expected=
        # "off" vs actual="cool" → schreibt "off" → AC aus.
        if (
            domain == "climate"
            and not on
            and self.state.cool_state.get(device_id, False)
        ):
            _LOGGER.debug(
                "skip heat-off write for %s: cool_on=True owns climate entity",
                device_id,
            )
            self._cancel_hold(device_id)
            return

        # v3.6.3: idempotent guard — wenn die Entity schon im commanded
        # state sitzt, nicht nochmal schreiben. Heat-Pumpen tolerieren
        # redundante Service-Calls, aber Split-AC's piepen bei jedem
        # set_hvac_mode-Call. Drift-Repair übernimmt der Hold-Loop nach
        # HOLD_INITIAL_DELAY falls actual doch noch != expected ist.
        expected = self._expected_state_value(raw_value, on, domain)
        actual = self._read_current_state(entity_id)
        if expected is not None and actual == expected:
            self._start_hold(device_id, entity_id, raw_value, domain, on)
            return

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
            # v3.6.4: symmetrisch zur heat-side: cool→off darf die
            # entity nicht überschreiben wenn heat-side gerade live ist
            # (is_on=True). Würde sonst die heat-Mode beim cool_on=False
            # SSE-Frame nach "off" zwingen statt heat sauber zu
            # übernehmen.
            if (
                not cool_on
                and self.state.on_state.get(device_id, False)
            ):
                _LOGGER.debug(
                    "skip cool-off write for %s: is_on=True owns climate entity",
                    device_id,
                )
                return
            mode = "cool" if cool_on else "off"
            # v3.6.3: bei climate.* teilen Heat- und Cool-Seite dieselbe
            # Entity. Der Heat-Side-Hold-Loop hätte sonst seinen
            # raw_value (= value_off bei is_on=False) alle 30s gegen
            # unser "cool" gesetzt — Endlos-Pingpong + Piep-Bestätigung
            # bei jeder Klimaanlage. Beim Cool-Flip kapern wir die
            # Mode-Hoheit: bisherigen Heat-Hold canceln, kein neuer
            # Cool-Hold (set_hvac_mode ist sticky, Drift-Repair übernimmt
            # die nächste Solver-Tick falls nötig). Beim Cool-Off-Flip
            # startet `_apply_device_state` automatisch wieder eine
            # Heat-Hold falls is_on=True.
            self._cancel_hold(device_id)
            # v3.5.1: war fälschlich auf .warning gesetzt → tauchte als
            # „Fehler" in HA's Protokoll auf obwohl es eine normale
            # Operation ist. Jetzt auf .debug damit's nur bei aktivem
            # Debug-Logging sichtbar wird.
            _LOGGER.debug(
                "set_hvac_mode: %s → %s (cool side)", heat_entity, mode,
            )
            # v3.6.3: idempotent — wenn der climate-Mode schon stimmt,
            # nicht nochmal schreiben (sonst piept die AC bei jedem
            # SSE-Echo / Re-Tick). Drift-Repair kommt von der nächsten
            # backend-decision, nicht von redundanten Re-Asserts hier.
            actual = self._read_current_state(heat_entity)
            if actual == mode:
                return
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

    async def _apply_battery_setpoint(
        self, device_id: str, mode: str, setpoint_kw: float
    ) -> None:
        """Phase 3 Option D (2026-06-02): Battery-Dispatch via 2 HA-
        Entities — Lademodus-Select (Aktiv/Passiv) + Power-Setpoint-
        Number (signed Watts, + = laden, − = entladen).

        Dispatch-Pfade:
          * mode == "passive"  → schreibe value_battery_mode_passive an
            entity_battery_mode. KEIN Power-Write — HA-Automation hält
            den Setpoint nicht mehr, WR übernimmt PV-Native-Priority.
          * mode != "passive"  → schreibe Setpoint (in Watts) an
            entity_battery_power_setpoint, dann value_battery_mode_active
            an entity_battery_mode. HA-Automation hält den Setpoint.

        Vorzeichen: solver-intern + = laden. Wenn der User
        `battery_setpoint_invert_sign` aktiviert hat (umgekehrter WR),
        multipliziert der Connector mit −1.

        Idempotent: skip write wenn HA-State bereits gleich (Mode-
        Select + Setpoint-Toleranz ±10 W).
        """
        dev = next(
            (d for d in self.devices if d.get(CONF_DEVICE_ID) == device_id),
            None,
        )
        if dev is None:
            return
        mode_entity = dev.get(CONF_ENTITY_BATTERY_MODE, "") or ""
        active_val = dev.get(CONF_VALUE_BATTERY_MODE_ACTIVE, "") or ""
        passive_val = dev.get(CONF_VALUE_BATTERY_MODE_PASSIVE, "") or ""
        setpoint_entity = dev.get(CONF_ENTITY_BATTERY_POWER_SETPOINT, "") or ""
        invert = bool(dev.get(CONF_BATTERY_SETPOINT_INVERT_SIGN, False))

        if not mode_entity or not active_val or not passive_val:
            _LOGGER.debug(
                "Battery %s: mode-entity / mode-values nicht gemapped — skip",
                device_id,
            )
            return

        if mode == "passive":
            current_mode = self._read_current_state(mode_entity)
            if current_mode != passive_val:
                _LOGGER.info(
                    "Battery %s passive: %s → %s",
                    device_id, current_mode, passive_val,
                )
                try:
                    await self.hass.services.async_call(
                        mode_entity.split(".", 1)[0], "select_option",
                        {"entity_id": mode_entity, "option": passive_val},
                        blocking=True,
                    )
                except Exception as err:  # noqa: BLE001
                    _LOGGER.exception(
                        "Battery passive-write failed for %s: %s",
                        mode_entity, err,
                    )
            return

        # Aktiv-Pfad: Setpoint berechnen + schreiben, dann Mode aktivieren.
        target_w = float(setpoint_kw) * 1000.0
        if invert:
            target_w = -target_w
        if setpoint_entity:
            actual_raw = self._read_current_state(setpoint_entity)
            should_write = True
            if actual_raw is not None:
                try:
                    if abs(float(actual_raw) - target_w) <= 10.0:
                        should_write = False
                except (ValueError, TypeError):
                    pass
            if should_write:
                _LOGGER.info(
                    "Battery %s setpoint %s → %.0f W (mode=%s, invert=%s)",
                    device_id, setpoint_entity, target_w, mode, invert,
                )
                try:
                    await self.hass.services.async_call(
                        setpoint_entity.split(".", 1)[0], "set_value",
                        {"entity_id": setpoint_entity, "value": target_w},
                        blocking=True,
                    )
                except Exception as err:  # noqa: BLE001
                    _LOGGER.error(
                        "Battery setpoint-write FAILED for %s: %s — "
                        "skipping subsequent mode-switch to %s damit "
                        "der Inverter nicht mit alt/null-Setpoint auf "
                        "Aktiv läuft (Cluster B Connector 2026-06-09).",
                        setpoint_entity, err, active_val,
                    )
                    return

        # Mode-Select zuletzt setzen — sodass der Setpoint schon
        # geschrieben ist wenn HA's Automation auf "Aktiv" reagiert.
        current_mode = self._read_current_state(mode_entity)
        if current_mode != active_val:
            _LOGGER.info(
                "Battery %s mode: %s → %s",
                device_id, current_mode, active_val,
            )
            try:
                await self.hass.services.async_call(
                    mode_entity.split(".", 1)[0], "select_option",
                    {"entity_id": mode_entity, "option": active_val},
                    blocking=True,
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception(
                    "Battery active-mode-write failed for %s: %s",
                    mode_entity, err,
                )

    async def _apply_vorlauf_setpoint(
        self, device_id: str, temperature_c: float
    ) -> None:
        """Phase 2b (2026-06-02): write the solver's Vorlauf-Setpoint
        in °C to the user-configured entity_vorlauf_setpoint. Dispatch-
        Pfad nach Domain:
          * `climate.*` → `climate.set_temperature(temperature=°C)`
          * `number.*` / `input_number.*` → `set_value(value=°C)`
          * sonst → skip (User-Misconfig oder Phase-3-Entity-Domain
            die wir noch nicht unterstützen).

        Fallback-Kaskade:
          1. `entity_vorlauf_setpoint` explicit gemapped (Manuell-Mode-
             User, SG-Ready-WP mit eigenem number/input_number) → nutzen.
          2. Leer + `entity_control` ist climate.* (Climate-Mode-User,
             v3.7.1+) → entity_control hat `set_temperature` eingebaut,
             dispatch direkt dagegen.
          3. Sonst (Manuell-Mode ohne expliziten Setpoint) → skip,
             Heizung läuft weiter binary on/off."""
        dev = next(
            (d for d in self.devices if d.get(CONF_DEVICE_ID) == device_id),
            None,
        )
        if dev is None:
            return
        entity_id = dev.get(CONF_ENTITY_VORLAUF_SETPOINT, "") or ""
        if not entity_id:
            # Climate-Mode-Fallback: entity_control ist die climate.*-
            # Entity, die kennt `set_temperature` selbst.
            control_entity = dev.get(CONF_ENTITY_CONTROL, "") or ""
            if control_entity.startswith("climate."):
                entity_id = control_entity
        if not entity_id:
            return
        domain = entity_id.split(".", 1)[0]
        # Idempotenz: wenn HA bereits den exakten Wert reportet (≤ 0.05 °C
        # Toleranz für Float-Rauschen), kein Re-Write — climate.set_
        # temperature ist auf manchen WPs ebenso piepend wie set_hvac_mode.
        actual_raw = self._read_current_state(entity_id)
        if actual_raw is not None:
            try:
                if abs(float(actual_raw) - temperature_c) <= 0.05:
                    return
            except (ValueError, TypeError):
                pass
        _LOGGER.debug(
            "set_vorlauf_setpoint: %s → %.1f °C", entity_id, temperature_c,
        )
        try:
            if domain == "climate":
                await self.hass.services.async_call(
                    "climate", "set_temperature",
                    {"entity_id": entity_id, "temperature": temperature_c},
                    blocking=True,
                )
            elif domain in ("number", "input_number"):
                await self.hass.services.async_call(
                    domain, "set_value",
                    {"entity_id": entity_id, "value": temperature_c},
                    blocking=True,
                )
            else:
                _LOGGER.warning(
                    "Device %s entity_vorlauf_setpoint=%s domain %s nicht "
                    "unterstützt — bitte climate / number / input_number "
                    "wählen.",
                    device_id, entity_id, domain,
                )
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception(
                "set_vorlauf_setpoint failed for %s: %s", entity_id, err,
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
        existing = self.state.hold_tasks.pop(device_id, None)
        if existing is not None and not existing.done():
            existing.cancel()

        dev = next(
            (d for d in self.devices if d.get(CONF_DEVICE_ID) == device_id),
            None,
        )
        if dev is None:
            return
        # Hold-Mode-Semantik (v3.6.3+):
        #   * NEVER  → kein Loop, sofortiges Return.
        #   * AUTO   → smart: nur re-write bei Drift (Default). Stoppt
        #     piepende Re-Asserts bei climate.* Devices.
        #   * ALWAYS → blind re-write jeden Tick. Für "schwarze" Devices
        #     wo der HA-State nicht zuverlässig reflektiert was am Gerät
        #     passiert (Kostal Modbus Auto-Reset etc.).
        mode = dev.get(CONF_ENTITY_CONTROL_HOLD) or ENTITY_CONTROL_HOLD_AUTO
        if mode == ENTITY_CONTROL_HOLD_NEVER:
            return

        # Cluster B Connector (2026-06-09, Review #11): Background-Task
        # bei HA registrieren statt naked asyncio.create_task. Vorher
        # wusste HA's Task-Tracker nichts von diesen Tasks → bei
        # Shutdown-Edge-Cases (Exception in async_shutdown vorm Cancel)
        # blieben sie als Orphan-Tasks im Loop. SSE/Heartbeat nutzen
        # das Pattern schon — Symmetrie war hier nur noch nicht da.
        self.state.hold_tasks[device_id] = self.hass.async_create_background_task(
            self._hold_loop(device_id, entity_id, raw_value, domain, on, mode),
            name=f"theothergas_entity_hold_{device_id}",
        )

    def _cancel_hold(self, device_id: str) -> None:
        task = self.state.hold_tasks.pop(device_id, None)
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
            self.state.active_state,
            self.state.on_state,
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
        hold_mode: str,
    ) -> None:
        """Keep entity_control sticking to its commanded value.

        Hold-Mode-Semantik (v3.6.3+):
          * `NEVER`  → kein Loop (in `_start_hold` gefiltert)
          * `AUTO`   → smart: nur re-write wenn `actual != expected`
            (Drift-Repair). Default seit v3.6.3. Verhindert blindes
            Bombardieren von Devices die einen state korrekt halten —
            insbesondere `climate.set_hvac_mode` piept bei jeder
            Klimaanlage, redundante Re-Asserts müssen weg.
          * `ALWAYS` → blind re-write jeden Tick. Für „schwarze"
            Devices wo der HA-state nicht zuverlässig reflektiert was
            wirklich am Gerät passiert (Kostal Modbus etc. — Register
            auto-reset ohne dass HA es als state-change sieht).

        Bails out if Crowdergize gets switched off (the `_cancel_hold`
        path covers that) or on coordinator shutdown.
        """
        try:
            # Initial delay gives the apply call's effect time to
            # propagate before the first rewrite (avoids a duplicate
            # service call back-to-back).
            await asyncio.sleep(HOLD_INITIAL_DELAY)
            while True:
                if not self.state.active_state.get(device_id, False):
                    return
                # Cluster B Connector (2026-06-09): SSE-Stale-Bail
                # mirror'd vom `_charge_mode_hold_loop`. Vorher schrieb
                # diese Loop blind weiter, auch wenn Crowdergy gar
                # nicht mehr antwortet → User-Manuelle-Änderung an
                # Heizung/WP/AC während 2h SSE-Outage wurde im 30s-
                # Tick wieder überschrieben. Bei plugged WP/SG-Ready
                # potentiell gefährlich.
                staleness = time.time() - self.state.last_sse_event_at
                if staleness > SSE_STALE_THRESHOLD_S:
                    _LOGGER.warning(
                        "entity_control hold: bailing for %s — Crowdergy "
                        "SSE silent for %.1fs (> %ds). User regains "
                        "manual control.",
                        device_id, staleness, SSE_STALE_THRESHOLD_S,
                    )
                    return
                expected = self._expected_state_value(raw_value, on, domain)
                actual = self._read_current_state(entity_id)
                if (
                    hold_mode == ENTITY_CONTROL_HOLD_AUTO
                    and expected is not None
                    and actual == expected
                ):
                    # AUTO + state stimmt → skip. Spart Service-Calls
                    # und Piep-Bestätigungen bei AC.
                    await asyncio.sleep(HOLD_POLL_INTERVAL)
                    continue
                if actual is not None and expected is not None and actual != expected:
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

