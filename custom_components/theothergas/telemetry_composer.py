"""TelemetryComposer — Background-Loops + Bootstrap-Helpers aus
coordinator.py extrahiert (FEAT-5 Phase D, 2026-06-09).

Scope dieser Iteration (D-Step-1):
* 3 Background-Loops (Heartbeat, Device-Mirror, State-Resync)
* `bootstrap_active_state()` — initial GET /devices nach HA-Start
* `push_outdoor_temp()` — Outdoor-Temp-Sensor → /users/me/outdoor

NICHT in dieser Iteration:
* `_async_update_data` (HA DataUpdateCoordinator-Interface, bleibt
  Coordinator-Methode; Body kann in einem Follow-Up nach hier wandern)
* Entity-Reader-Helpers (`_read_temp_c`, `_read_energy_kwh`, etc.) —
  von vielen Stellen genutzt, Follow-Up

Pattern: Composer hält eine `coord`-Referenz und ruft Coordinator-
State (`coord.devices`, `coord.state.*`, `coord._authenticated_request`)
durch. Selbst-Statelos außer den Mirror-Bookkeeping-Dicts die wir
übernehmen.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from .coordinator import CrowdergyCoordinator

_LOGGER = logging.getLogger(__name__)

# Loop-Intervalle (Sekunden). Seit #97 (2026-07-02) ist DIES die einzige
# Definition — coordinator.py RE-EXPORTIERT die Namen (analog der
# telemetry_reader-Konstanten), statt eine zweite, von den Loops nie
# gelesene Kopie zu tragen (Drift-Trap aus dem #21-Split).

HEARTBEAT_PING_INTERVAL = 25.0
"""Cadence of the lightweight liveness ping the connector POSTs to
`/api/v1/users/me/heartbeat`. Independent of any device's PATCH
schedule — exists so the backend can stamp `users.connector_last_seen`
(and thus iOS's connection dot) without relying on the high-frequency
telemetry stream. Slightly under 30 s so iOS's 35 s 'live' threshold
has one full ping of grace even if the request lands at the back of a
network queue."""

PER_DEVICE_MIRROR_INTERVAL = 60.0
"""Per-device heartbeat-mirror cadence (v3.4.3+). Pushed das zuletzt
gesendete Payload erneut (ohne Δ-Felder, s. `_DELTA_FIELDS` — sonst
landet die Δ-kWh doppelt, #62), wenn seit dem letzten echten PATCH
≥60 s vergangen sind. Refresht das telemetry-row-Timestamp im Backend
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

_HEARTBEAT_BACKOFF_MAX_S = 120.0

# #62: the device-mirror replays the last real payload verbatim to keep
# the backend freshness clock ticking for idle devices — but it must NOT
# resend ANY energy-Δ field, or the backend lands that Δ a second time
# (double-counting kWh). Both the legacy signed `energy_kwh_delta` AND the
# unsigned `energy_kwh_in_delta`/`energy_kwh_out_delta` pair (sent since
# v3.21.4; the backend prefers it and re-derives the signed Δ from it)
# carry energy. They live here as a single set so a future Δ field is
# stripped centrally by name instead of being forgotten one more time.
_DELTA_FIELDS = frozenset(
    {"energy_kwh_delta", "energy_kwh_in_delta", "energy_kwh_out_delta"}
)


class TelemetryComposer:
    """Owns die langlebigen Background-Tasks die Telemetry zum Backend
    pushen (Heartbeat, Device-Mirror) bzw. Backend-State zurücklesen
    (State-Resync).

    Composer enthält **keinen** eigenen Persistenz-State über die
    Lebenszeit eines HA-Reloads hinaus — alle Telemetry-Bookkeeping-
    Dicts (`_last_sent_payload`, `_last_send_at`, `_last_sent_hash`)
    leben weiterhin am Coordinator damit `_async_update_data` und
    die Mirror-Loops dieselbe Quelle teilen.
    """

    def __init__(self, coord: "CrowdergyCoordinator") -> None:
        self.coord = coord

    # ── Initial-Bootstrap ──────────────────────────────────────────

    async def bootstrap_active_state(self) -> None:
        """One-shot GET /devices to seed the Crowdergize + on/off caches.

        Ohne diesen Boot-Strap booten die HA-Switch-Entities mit `False`
        (Coordinator-Default), und ein HA-Restart würde silent einen
        vorher-on State verlieren. Backend ist Source-of-Truth für beide
        Flags.

        Cluster B Connector (2026-06-09): `cool_state` mit bootstrappen —
        sonst defaultete der Skip-Guard in `_apply_device_state` für
        climate-Entities auf False und ein SSE `is_on=False`-Frame
        kippte die heat-Mode statt cool sauber stehen zu lassen.
        """
        try:
            response = await self.coord._authenticated_request(
                "GET", "/api/v1/devices",
            )
            response.raise_for_status()
            for d in response.json():
                self.coord.state.active_state[d["id"]] = bool(
                    d.get("is_active", False)
                )
                self.coord.state.on_state[d["id"]] = bool(d.get("is_on", False))
                self.coord.state.cool_state[d["id"]] = bool(
                    d.get("cool_on", False)
                )
            self.coord.state.active_state_bootstrapped = True
        except (httpx.HTTPStatusError, httpx.RequestError) as err:
            _LOGGER.warning(
                "Bootstrap of device state failed (%s) — "
                "will retry next refresh",
                err,
            )

    async def push_outdoor_temp(self) -> None:
        """Optional Outdoor-Temp-Sensor read + POST to /users/me/outdoor.
        Silent skip wenn nicht gemapped — Backend fällt dann auf
        eigenen Open-Meteo-Poll für diesen User zurück.
        """
        from .const import CONF_ENTITY_OUTDOOR_TEMP

        entity_id = self.coord.entry.data.get(CONF_ENTITY_OUTDOOR_TEMP, "")
        if not entity_id:
            return
        temp = self.coord._read_entity_state(entity_id)
        if temp is None:
            return
        try:
            response = await self.coord._authenticated_request(
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

    # ── Background-Loops ───────────────────────────────────────────

    async def heartbeat_loop(self) -> None:
        """POST /users/me/heartbeat every HEARTBEAT_PING_INTERVAL.

        Backend stempelt `connector_last_seen` + `connector_version`
        aus diesem Call. Exponential-Backoff bei consecutive failures
        25 → 50 → … → cap 120 s. Reset auf base-interval bei nächstem
        Success.

        E-4 (2026-06-11, bewusste Entscheidung): der Heartbeat ist NICHT
        auf `consent_telemetry` gegated. Telemetrie-Consent deckt Energie-
        und Messwerte ab (`mirror_once` / `_async_update_data` sind
        gegated); der Heartbeat trägt nur Betriebsdaten (last_seen +
        Connector-Version) und muss auch bei telemetry=false laufen,
        damit die Box „Connector online" anzeigen kann. Dasselbe gilt
        für das Device-Polling.
        """
        consecutive_failures = 0
        while True:
            failed = False
            try:
                response = await self.coord._authenticated_request(
                    "POST", "/api/v1/users/me/heartbeat",
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

    async def device_mirror_loop(self) -> None:
        """Endlos-Loop um `mirror_once()` — Body separat, damit eine
        einzelne Iteration ohne Task-/Sleep-Maschinerie testbar ist
        (CN-5-Regression-Tests)."""
        while True:
            try:
                await self.mirror_once()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "device-mirror loop iteration error: %s", err,
                )
            await asyncio.sleep(PER_DEVICE_MIRROR_INTERVAL)

    async def mirror_once(self) -> None:
        """Eine Mirror-Iteration: für jedes Gerät mit früherem Payload
        PATCHen wir den letzten Payload erneut wenn
        ≥ PER_DEVICE_MIRROR_INTERVAL seit dem letzten echten Send UND
        dem letzten Mirror vergangen ist. JEDES Energie-Δ-Feld
        (`_DELTA_FIELDS`) wird weggelassen — der Original-Send hat seine
        Δ-kWh schon ins Backend gebracht, ein zweites Mal würde doppelt
        zählen (#62: vorher fiel nur `energy_kwh_delta` raus, das
        unsigned `energy_kwh_in/out_delta`-Paar blieb drin → Backend
        re-derivte daraus den signed Δ und zählte ihn erneut).

        CN-5 (2026-06-11): der Mirror bucht auf den EIGENEN Timestamp
        `_last_mirror_at`. Vorher hat er `_last_send_at` resettet und
        damit den 90-s-Soft-Heartbeat + das 600-s-Hard-Ceiling in
        `_should_send` dauerhaft ausgehebelt (sub-threshold Drift
        wurde nie gemeldet — toter Code seit v3.4.3).

        Telemetrie-Consent-Gate (2026-06-11): das Privacy-Modell
        verspricht „telemetry=false stoppt ALLE Telemetrie-Pushes"
        (services.yaml) — der Mirror re-PATCHt Telemetrie-Payloads
        und muss deshalb genauso gated sein wie `_async_update_data`.
        """
        from .const import OPT_CONSENT_TELEMETRY

        if not self.coord._consent(OPT_CONSENT_TELEMETRY):
            return
        now_ts = time.time()
        for device_id, last_payload in list(
            self.coord._last_sent_payload.items()
        ):
            last_activity = max(
                self.coord._last_send_at.get(device_id, 0.0),
                self.coord._last_mirror_at.get(device_id, 0.0),
            )
            if now_ts - last_activity < PER_DEVICE_MIRROR_INTERVAL:
                continue
            mirror = {
                k: v for k, v in last_payload.items()
                if k not in _DELTA_FIELDS
            }
            # v3.26.0: skip Mirror, wenn Device backend-seitig weg ist
            if device_id in self.coord._backend_gone_device_ids:
                continue
            try:
                # #18: gleicher bounded Retry/Backoff wie der primäre
                # Send — ein transienter Blip soll auch den Mirror nicht
                # für eine ganze PER_DEVICE_MIRROR_INTERVAL aussetzen.
                response = await self.coord._patch_telemetry_with_retry(
                    device_id, mirror,
                )
                if response.status_code < 400:
                    self.coord._last_mirror_at[device_id] = now_ts
                elif response.status_code in (404, 410):
                    self.coord._backend_gone_device_ids.add(device_id)
                    _LOGGER.info(
                        "Mirror: Device %s vom Backend gelöscht (HTTP %s) — "
                        "weitere PATCHes werden geskippt",
                        device_id, response.status_code,
                    )
                else:
                    _LOGGER.debug(
                        "device-mirror PATCH %s returned %s: %s",
                        device_id, response.status_code,
                        response.text,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "device-mirror PATCH failed for %s: %s",
                    device_id, err,
                )

    async def state_resync_loop(self) -> None:
        """Endlos-Loop um `resync_once()` — Body separat, damit eine
        einzelne Iteration ohne Task-/Sleep-Maschinerie testbar ist
        (CN-1-Regression-Tests)."""
        while True:
            try:
                await self.resync_once()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "state-resync loop iteration error: %s", err,
                )
            await asyncio.sleep(STATE_RESYNC_INTERVAL)

    async def resync_once(self) -> None:
        """Eine Resync-Iteration: Pull von GET /api/v1/devices als
        SSE-Drop-Backstop. Vergleicht Backend-State (is_active, is_on,
        cool_on) mit lokalem Cache; bei Drift Cache-Update + Re-Apply
        via coordinator._apply_device_state / _apply_cool_state.

        SSE-Limitation: Backend publisht nur bei state-Transitions,
        nicht idempotent. Connector kann zum Publish-Moment nicht
        subscribed sein (Netzwerk-Flap, HA-Restart, NAT-Idle). Polling-
        Backstop fängt das innerhalb STATE_RESYNC_INTERVAL ab.

        CN-1 (2026-06-11): früher Skip ohne Remote-Control-Consent —
        die zentralen Gates in den `_apply_*`-Methoden würden ohnehin
        greifen, aber der ganze Resync existiert NUR um Cloud-State
        re-zuapplyen; ohne Consent ist schon der GET + das Diffing
        sinnlos (und das Cache-Update bliebe inkonsistent zum
        SSE-Dispatch, der ohne Consent ebenfalls nichts synct).
        """
        if not self.coord._remote_control_allowed("state-resync"):
            return
        response = await self.coord._authenticated_request(
            "GET", "/api/v1/devices",
        )
        if response.status_code >= 400:
            _LOGGER.debug(
                "state-resync GET returned %s: %s",
                response.status_code, response.text,
            )
            return
        for d in response.json():
            device_id = d["id"]
            bk_active = bool(d.get("is_active", False))
            bk_on = bool(d.get("is_on", False))
            bk_cool = bool(d.get("cool_on", False))

            local_on = self.coord.state.on_state.get(device_id)
            local_cool = self.coord.state.cool_state.get(device_id)
            local_active = self.coord.state.active_state.get(device_id)

            # Cache unkonditional aktualisieren — Backend
            # ist source of truth.
            self.coord.state.active_state[device_id] = bk_active
            self.coord.state.on_state[device_id] = bk_on
            self.coord.state.cool_state[device_id] = bk_cool

            if not bk_active:
                continue
            if local_on is not None and local_on != bk_on:
                _LOGGER.warning(
                    "state-resync: %s is_on drifted "
                    "(cache=%s, backend=%s) — reapplying",
                    device_id, local_on, bk_on,
                )
                await self.coord._apply_device_state(
                    device_id, bk_on,
                )
            if local_cool is not None and local_cool != bk_cool:
                _LOGGER.warning(
                    "state-resync: %s cool_on drifted "
                    "(cache=%s, backend=%s) — reapplying",
                    device_id, local_cool, bk_cool,
                )
                try:
                    await self.coord._apply_cool_state(
                        device_id, bk_cool,
                    )
                except Exception:  # noqa: BLE001
                    _LOGGER.debug(
                        "state-resync: cool-reapply failed for %s",
                        device_id,
                    )
            if local_active is False and bk_active:
                _LOGGER.info(
                    "state-resync: %s is_active drifted "
                    "False→True (cache vs backend) — "
                    "User-Toggle wird empfohlen für "
                    "Charge-Mode-Snapshot",
                    device_id,
                )
