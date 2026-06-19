"""DataUpdateCoordinator for Crowdergy Connector."""
from __future__ import annotations

import asyncio
import base64
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
    CONF_DEVICE_ID,
    CONF_DEVICE_TYPE,
    CONF_DEVICES,
    CONF_ENTITY_CHARGE_MODE,
    CONF_ENTITY_CLIMATE,
    CONF_ENTITY_CONTROL,
    CONF_ENTITY_POWER,
    CONF_ENTITY_SOC,
    CONF_ENTITY_VEHICLE_STATUS,
    CONF_ENTITY_CURRENT_TEMP,
    CONF_ENTITY_ENERGY_TOTAL,
    CONF_INVERT_POWER_SIGN,
    CONF_ENTITY_ENERGY_DISCHARGED_TOTAL,
    CONF_ENTITY_VORLAUF_TEMP,
    CONF_ENTITY_HC_PV_POWER,
    CONF_ENTITY_HC_BATTERY_POWER,
    CONF_ENTITY_HC_GRID_POWER,
    CONF_ENTITY_PV_TO_BATTERY_POWER,
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
)

from .command_dispatcher import CommandDispatcherMixin
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
# sein (siehe `_compose_extra`). "temp" → liest °C-Sensoren oder die
# `current_temperature` aus climate-Attributen; "power" → liest einen
# Leistungssensor und normalisiert auf kW (W→kW über das HA-Unit-Attr).
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
    # Hausverbrauchs-Flow-Sensoren (#42, NICHT solver-gelesen — reine
    # Chart-Eingabe für Backend #41 `GET /users/me/energy/today`). Je
    # Wert wird auf kW normalisiert und als `*_power_kw` im extra-Bag
    # mitgeschickt; die Keys spiegeln 1:1 die Backend-`SOLVER_FIELDS`.
    "solar": [
        ("hc_pv_power_kw", CONF_ENTITY_HC_PV_POWER, "power"),
    ],
    "battery": [
        ("hc_battery_power_kw", CONF_ENTITY_HC_BATTERY_POWER, "power"),
        ("pv_to_battery_power_kw", CONF_ENTITY_PV_TO_BATTERY_POWER, "power"),
    ],
    "grid": [
        ("hc_grid_power_kw", CONF_ENTITY_HC_GRID_POWER, "power"),
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


# ── #18 Telemetry-PATCH Retry/Backoff ────────────────────────────────
#
# Vorher: ein transienter Transport-Fehler (Netzwerk-Flap, kurzer
# Backend-Hiccup) auf dem Telemetry-PATCH wurde nur geloggt; der Send
# ging verloren und wurde erst beim nächsten `_should_send`-True-Tick
# (bis zu PER_DEVICE_HEARTBEAT_INTERVAL/90 s später) wiederholt → iOS-
# Tile flippte unnötig auf stale. Jetzt: kurzer bounded Retry mit
# Exponential-Backoff direkt im Tick. Bewusst KURZ gehalten — das blockt
# den Coordinator-Tick, der sequenziell über alle Geräte läuft. kWh geht
# ohnehin nie verloren (Δ wird gegen `_prev_energy_kwh` gerechnet, das
# nur bei Erfolg fortschreibt), hier geht es um die Frische des
# power_kw/soc-Snapshots.
TELEMETRY_RETRY_ATTEMPTS = 3
"""Gesamt-Versuche inkl. des ersten (also max. 2 Retries)."""
TELEMETRY_RETRY_BASE_BACKOFF_S = 0.5
"""Backoff vor Retry: 0.5 s, dann 1.0 s. Worst-Case-Zusatzlatenz pro
Gerät ~1.5 s — vertretbar im 30-s-Tick bei typisch 1–5 Geräten."""
_TELEMETRY_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
"""Nur DIESE Status-Codes werden retried. 4xx (inkl. 404/410-Eviction)
gibt der Caller unverändert zurück — kein Retry auf permanente Fehler."""


# ── Energie-Δ aus total_increasing-Zählern ───────────────────────────
ENERGY_RESET_RATIO = 0.9
"""Schwelle für die Reset- vs. Rausch-Unterscheidung eines
`total_increasing`-Zählers (analog HA-Core-Statistik). Fällt der
Zählerstand auf < diesem Anteil des letzten Werts, werten wir es als
echten Meter-Reset (Firmware-Update/Geräte-Austausch); ein kleinerer
Rückschritt ist Sensor-Rauschen/Rundung."""


def _counter_delta(
    current: float, prev: float | None
) -> tuple[float | None, float]:
    """Positiver kWh-Δ eines `total_increasing`-Zählers seit dem letzten
    GESENDETEN Wert `prev`, plus die Baseline, die als Nächstes
    gespeichert werden soll. Gibt `(delta, next_baseline)` zurück.

    `delta` ist `None`, wenn noch keine Baseline existiert (erster Read
    nach Coordinator-Restart) — dann gibt es nichts zu buchen.

    **High-Water-Mark (Fix: Solar-kWh 10–15 % zu hoch):** ein
    Rückschritt (`current < prev`) wird NICHT mit fortgeschriebener
    Baseline auf den niedrigeren Wert quittiert. Sonst zählt der
    Wieder-Anstieg ein zweites Mal und der Backend-Summen-Zähler driftet
    systematisch nach oben — gerade Solar-Zähler jittern im µ-kWh-Bereich
    und der aktive Inverter sendet quasi jeden Tick (Power-Schwelle), so
    dass jeder Dip die Baseline regressierte und der Re-Anstieg doppelt
    landete:

    * `current >= prev`               → normaler Δ, Baseline = `current`.
    * kleiner Dip (Rauschen)          → Δ = 0, Baseline GEHALTEN (`prev`).
    * großer Sturz (Meter-Reset)      → Δ = 0, Baseline neu = `current`
      (ab dem nächsten Tick wieder ab `current` zählen, KEIN Spike vom
      Re-Anstieg auf den alten Stand).
    """
    if prev is None:
        return None, current
    raw = current - prev
    if raw >= 0:
        return raw, current
    if current >= ENERGY_RESET_RATIO * prev:
        # Kleiner Rückschritt → Sensor-Rauschen: kein Δ, Baseline halten.
        return 0.0, prev
    # Großer Sturz → echter Zähler-Reset: neu baseline-n, nichts buchen.
    return 0.0, current


# ── #19 Proaktives Token-Refresh ─────────────────────────────────────
PROACTIVE_REFRESH_MARGIN_S = 120.0
"""Refresh das Access-Token proaktiv, sobald es in ≤ dieser Spanne
abläuft — statt erst reaktiv auf einen 401 zu warten. > der höchsten
periodischen Loop-Cadence (Heartbeat 25 s, Resync 90 s), damit ein
laufender Loop das Token verlässlich erneuert, bevor es kippt. Refresh
bleibt single-flight + CAS-sicher über `_refresh_lock`."""


def _jwt_exp(token: str) -> float | None:
    """Lies den `exp`-Claim (Unix-Sekunden) aus einem JWT ohne
    Signatur-Verifikation. Wir vertrauen unserem eigenen Token; `exp`
    dient nur dem Scheduling des proaktiven Refreshes (#19). Bei jedem
    Defekt (kein JWT, kaputtes Base64, fehlender Claim) → None, dann
    fällt der Pfad auf rein-reaktives 401-Refresh zurück."""
    try:
        payload_b64 = token.split(".")[1]
        # JWT nutzt base64url ohne Padding — wieder auffüllen.
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
        exp = claims.get("exp")
        return float(exp) if exp is not None else None
    except Exception:  # noqa: BLE001
        return None


class CrowdergyCoordinator(CommandDispatcherMixin, DataUpdateCoordinator[dict[str, dict[str, Any]]]):
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
        # #19: Ablauf-Zeitpunkt des Access-Tokens (Unix-s) für proaktiven
        # Refresh. None = kein/kein-dekodierbares exp → rein reaktiver
        # 401-Pfad. Wird bei jedem erfolgreichen Refresh fortgeschrieben.
        self._access_token_exp: float | None = _jwt_exp(self._access_token)
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
        # (E-2 / XR-1, 2026-06-11: das frühere
        # `_pre_crowdergize_charge_mode`-Snapshot-Dict ist entfernt —
        # toter Restore-Pfad, das Backend sendet
        # `charge_mode_value_crowdergy` seit 2026-06-03 nicht mehr.)
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
        # CN-5 (2026-06-11): eigener Timestamp für den Device-Mirror.
        # `_last_send_at` gehört EXKLUSIV den echten `_should_send`-Sends
        # in `_async_update_data` — vorher hat der Mirror-Loop ihn
        # mitgeschrieben und damit den 90-s-Soft-Heartbeat und das
        # 600-s-Hard-Ceiling in `_should_send` dauerhaft ausgehebelt
        # (sub-threshold Drift wurde nie gemeldet).
        self._last_mirror_at: dict[str, float] = {}
        # C7 (2026-06-01) payload-hash dedup: stabilen content-hash
        # des letzten gesendeten payloads pro Gerät. Wenn der neue
        # hash identisch ist, hat der 90s-Soft-Heartbeat nichts neues
        # zu erzählen → skip bis IDENTICAL_HEARTBEAT_INTERVAL.
        self._last_sent_hash: dict[str, int] = {}
        # v3.26.0 (2026-06-15): Device-IDs die das Backend mit 404/410
        # auf einen Telemetry-PATCH quittiert hat. Backend-Fix für die
        # FK-Violation-Race (Device gelöscht zwischen SELECT und INSERT)
        # gibt jetzt 410 zurück. Der Connector merkt sich diese IDs
        # in-memory und skippt weitere PATCHes, damit kein endloser
        # Retry-Loop läuft. Reset bei HA-Restart oder Config-Reload —
        # `async_remove_config_entry_device` / `async_unload_entry`
        # legen das Set ohnehin neu an.
        self._backend_gone_device_ids: set[str] = set()
        # Throttle bookkeeping for the event-driven `async_refresh`
        # path. The scheduled 30 s tick is unaffected.
        self._last_event_refresh_at: float = 0.0
        # CN-1 (2026-06-11): per-context dedup for the consent-gate
        # DEBUG log so the periodic loops (resync 90 s, self-heal 30 s,
        # hold loops) don't spam the log while consent is revoked.
        self._consent_denied_logged: set[str] = set()
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
                on_auth_failed=self._start_reauth,
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

    def _start_reauth(self) -> None:
        """CN-11 (2026-06-11): SSE-Auth endgültig tot — der Stream hat
        SSE_AUTH_FAILURE_LIMIT 401-Zyklen hinter sich, Refresh hilft
        nicht mehr. Aus einem Hintergrund-Task ist
        `entry.async_start_reauth(hass)` der dokumentierte Weg, den
        Reauth-Flow zu starten (`ConfigEntryAuthFailed` wirkt nur im
        Coordinator-/Setup-Kontext). Idempotent — HA dedupliziert
        laufende Reauth-Flows pro Entry."""
        self.entry.async_start_reauth(self.hass)

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
                    # #19: neues exp übernehmen, damit der nächste
                    # proaktive Check gegen das frische Token rechnet.
                    self._access_token_exp = _jwt_exp(self._access_token)
                    new_data = {**self.entry.data}
                    new_data[CONF_ACCESS_TOKEN] = self._access_token
                    new_data[CONF_REFRESH_TOKEN] = self._refresh_token
                    self.hass.config_entries.async_update_entry(self.entry, data=new_data)
                    return True
                _LOGGER.warning("Token refresh returned %s", response.status_code)
            except httpx.RequestError as err:
                _LOGGER.error("Token refresh failed: %s", err)
            return False

    def _token_expires_within(self, margin_s: float) -> bool:
        """True wenn das Access-Token in ≤ margin_s abläuft (oder schon
        abgelaufen ist). Reine Funktion vom gecachten `_access_token_exp`
        — None (kein dekodierbares exp) ⇒ False (kein proaktiver Refresh,
        reaktiver 401-Pfad bleibt)."""
        if self._access_token_exp is None:
            return False
        return self._access_token_exp - time.time() <= margin_s

    async def _maybe_proactive_refresh(self) -> None:
        """#19: Refresh das Token VOR Ablauf statt erst reaktiv auf 401.
        Best-effort — schlägt der Refresh fehl (Backend kurz weg), läuft
        der Caller mit dem aktuellen (ggf. noch gültigen) Token weiter
        und fällt nötigenfalls auf den reaktiven 401-Pfad zurück. Der
        `seen_token`-CAS macht parallele Aufrufe single-flight: erneuert
        ein Loop bereits, sehen die anderen das frische Token, statt ein
        zweites `/auth/refresh` zu feuern (Refresh-Token-Rotation!)."""
        if not self._token_expires_within(PROACTIVE_REFRESH_MARGIN_S):
            return
        seen_token = self._access_token
        try:
            await self._refresh_access_token(seen_token=seen_token)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Proactive token refresh skipped: %s", err)

    async def _authenticated_request(
        self, method: str, path: str, **kwargs: Any
    ) -> httpx.Response:
        # #19: vor dem Call proaktiv erneuern, wenn das Token bald
        # abläuft — so erreichen reguläre Requests im Normalbetrieb nie
        # den 401-Zustand. Best-effort, blockt den Request nicht.
        await self._maybe_proactive_refresh()
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

    async def _patch_telemetry_with_retry(
        self, device_id: str, payload: dict[str, Any]
    ) -> httpx.Response:
        """#18: PATCH /devices/{id}/telemetry mit bounded Retry/Backoff
        auf transiente Fehler (Transport-Error + 5xx/429). Permanente
        4xx (inkl. 404/410-Eviction) werden NICHT retried, sondern
        unverändert zurückgegeben — der Caller interpretiert den Status.
        Versuche/Backoff sind bewusst kurz (siehe Modul-Konstanten), weil
        der Aufruf im Coordinator-Tick blockt.

        Terminal-Verhalten ist identisch zum Vor-#18-Stand: nach
        erschöpften Retries wird entweder die letzte (transiente)
        Response zurückgegeben (Caller loggt/`raise_for_status`) oder der
        letzte Transport-Fehler re-geraist (Caller fängt `RequestError`).
        """
        last_exc: httpx.RequestError | None = None
        response: httpx.Response | None = None
        for attempt in range(TELEMETRY_RETRY_ATTEMPTS):
            try:
                response = await self._authenticated_request(
                    "PATCH",
                    f"/api/v1/devices/{device_id}/telemetry",
                    json=payload,
                )
            except httpx.RequestError as err:
                last_exc = err
                response = None
            else:
                last_exc = None
                if response.status_code not in _TELEMETRY_RETRY_STATUS:
                    return response  # Erfolg oder permanenter Fehler
            if attempt < TELEMETRY_RETRY_ATTEMPTS - 1:
                backoff = TELEMETRY_RETRY_BASE_BACKOFF_S * (2 ** attempt)
                _LOGGER.debug(
                    "Telemetry PATCH for %s transient-failed "
                    "(attempt %d/%d) — retrying in %.1fs",
                    device_id, attempt + 1, TELEMETRY_RETRY_ATTEMPTS, backoff,
                )
                await asyncio.sleep(backoff)
        if last_exc is not None:
            raise last_exc
        # response ist hier gebunden (letzter Versuch lieferte einen
        # transienten Status) — Caller behandelt ihn wie zuvor.
        return response  # type: ignore[return-value]

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
        # P3 (2026-06-11): super().async_shutdown() stoppt den
        # Scheduled-Refresh + Debouncer der Basisklasse — vorher
        # konnte ein bereits geplanter Tick nach unserem Cleanup
        # noch feuern.
        await super().async_shutdown()
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
        # P3 (2026-06-11): `_client` ist bis `async_init()` None —
        # ein Shutdown vor/abseits des regulären Setups darf nicht
        # an `None.aclose()` sterben.
        if self._client is not None:
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
        # v3.26.0: Device wurde vom Backend mit 404/410 quittiert
        # (User hat es in der iOS-App gelöscht). Kein weiterer PATCH
        # bis HA-Restart bzw. Config-Reload.
        if device_id in self._backend_gone_device_ids:
            return False
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

    def _compose_extra(self, dev: dict[str, Any]) -> dict[str, Any]:
        """Read this device's registered `telemetry.extra` sensors into a
        flat bag. Driven by `_SOLVER_EXTRA_FIELDS` (per device type) so a
        new extra field is one registry line + one Backend `SolverField`
        — the `_async_update_data` loop never changes.

        Readers: "temp" → °C (sensor or climate `current_temperature`),
        "power" → kW (W→kW normalised). Non-numeric / unavailable reads
        are skipped so the bag only carries live values. Empty dict when
        nothing maps — caller drops `extra` entirely then.
        """
        extra_payload: dict[str, Any] = {}
        for payload_key, conf_key, reader in _SOLVER_EXTRA_FIELDS.get(
            dev.get(CONF_DEVICE_TYPE, ""), []
        ):
            entity_id = dev.get(conf_key, "")
            if not entity_id:
                continue
            if reader == "power":
                value = self._read_power_kw(entity_id)
            elif reader == "temp":
                value = self._read_temp_c(entity_id)
            else:
                value = self._read_entity_state(entity_id)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                extra_payload[payload_key] = float(value)
        return extra_payload

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

    def _remote_control_allowed(self, context: str) -> bool:
        """Zentrales Remote-Control-Consent-Gate (CN-1, 2026-06-11).

        ALLE cloud-getriebenen Schreibpfade laufen durch die
        `_apply_*`-Methoden:
          * SSE-Dispatch (`_handle_ws_message`)
          * State-Resync-Loop (`telemetry_composer.state_resync_loop`)
          * Hold-Self-Heal (`_self_heal_holds` aus `_async_update_data`)
          * Hold-Loops (`_hold_loop` startet nur über
            `_apply_device_state`/`_start_hold`; `_charge_mode_hold_loop`
            schreibt über `_apply_charge_mode`)
        — deshalb darf das Gate zentral am Anfang jeder `_apply_*`-
        Methode sitzen und deckt damit jeden Pfad ab. Vorher saß es
        NUR im SSE-Dispatch; Resync-Loop und Self-Heal haben es
        umgangen (Backend steuerte mit ≤90 s Latenz trotz
        `consent_remote_control=False` weiter).

        Loggt pro `context` genau EINMAL auf DEBUG, damit die
        periodischen Loops das Log nicht fluten, solange Consent
        entzogen ist.
        """
        if self._consent(OPT_CONSENT_REMOTE_CONTROL):
            # Consent (wieder) da → Log-Dedup zurücksetzen, damit ein
            # erneuter Entzug wieder sichtbar wird.
            if self._consent_denied_logged:
                self._consent_denied_logged.clear()
            return True
        if context not in self._consent_denied_logged:
            self._consent_denied_logged.add(context)
            _LOGGER.debug(
                "remote-control consent revoked — skipping %s "
                "(logged once per context)", context,
            )
        return False

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
            # Baseline, die nach erfolgreichem Send gespeichert wird —
            # High-Water-Mark-aware (siehe `_counter_delta`), damit ein
            # Zähler-Dip die Baseline NICHT regressiert (sonst doppelt
            # gezählter Re-Anstieg → Solar-kWh 10–15 % zu hoch).
            next_prev_in = energy_kwh_total
            next_prev_out = energy_kwh_total_out
            if energy_kwh_total is not None:
                in_delta, next_prev_in = _counter_delta(
                    energy_kwh_total, self._prev_energy_kwh.get(device_id)
                )
            if energy_kwh_total_out is not None:
                out_delta, next_prev_out = _counter_delta(
                    energy_kwh_total_out,
                    self._prev_energy_kwh_discharged.get(device_id),
                )
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

            # Solver-only + Chart-only extras (Vorlauf-Temp, HC-Flow-
            # Sensoren #42, …). JSONB-Bag im Backend; UI bekommt davon
            # nichts mit. Nur senden wenn mindestens ein Feld einen Wert
            # liefert, sonst Payload nicht aufblähen.
            extra_payload = self._compose_extra(dev)

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
                    # #18: bounded Retry/Backoff auf transiente Fehler
                    # statt sofortigem Aufgeben + Warten auf den nächsten
                    # Tick.
                    response = await self._patch_telemetry_with_retry(
                        device_id, payload,
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
                        # next_prev_in = energy_kwh_total bei Anstieg/Reset,
                        # = alter Wert bei einem Rausch-Dip (Baseline halten,
                        # nicht auf den niedrigeren Wert regressieren).
                        self._prev_energy_kwh[device_id] = next_prev_in
                    if energy_kwh_total_out is not None:
                        self._prev_energy_kwh_discharged[device_id] = (
                            next_prev_out
                        )
                except httpx.HTTPStatusError as err:
                    sc = err.response.status_code
                    # v3.26.0: 404/410 = Device existiert backend-seitig
                    # nicht mehr (iOS-DELETE oder Race). Mirror evicten
                    # statt endlosem Retry-Loop. INFO statt ERROR, weil
                    # das ein erwarteter User-Flow ist.
                    if sc in (404, 410):
                        self._backend_gone_device_ids.add(device_id)
                        _LOGGER.info(
                            "Device %s vom Backend gelöscht (HTTP %s) — "
                            "weitere PATCHes für diese ID werden geskippt",
                            device_id, sc,
                        )
                    else:
                        _LOGGER.error(
                            "Backend returned %s for device %s: %s",
                            sc, device_id, err.response.text,
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

        await self._self_heal_holds(list(result.keys()))

        return result
