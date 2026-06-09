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

# Intervalle (Sekunden). Symmetrisch zu den Werten die vor der
# Extraktion direkt in coordinator.py standen — keine Verhaltens-
# änderung in dieser Phase.
HEARTBEAT_PING_INTERVAL = 25.0
PER_DEVICE_MIRROR_INTERVAL = 60.0
STATE_RESYNC_INTERVAL = 90.0
_HEARTBEAT_BACKOFF_MAX_S = 120.0


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
        """Pro Tick (PER_DEVICE_MIRROR_INTERVAL): für jedes Gerät mit
        früherem Payload PATCHen wir den letzten Payload erneut wenn
        ≥ PER_DEVICE_MIRROR_INTERVAL seit dem letzten echten Send
        vergangen ist. `energy_kwh_delta` wird weggelassen — das hat
        beim Original-Send seine Δ-kWh ins Backend gebracht, doppelt
        zu senden würde doppelt zählen.
        """
        while True:
            try:
                now_ts = time.time()
                for device_id, last_payload in list(
                    self.coord._last_sent_payload.items()
                ):
                    age = now_ts - self.coord._last_send_at.get(device_id, 0.0)
                    if age < PER_DEVICE_MIRROR_INTERVAL:
                        continue
                    mirror = {
                        k: v for k, v in last_payload.items()
                        if k != "energy_kwh_delta"
                    }
                    try:
                        response = await self.coord._authenticated_request(
                            "PATCH",
                            f"/api/v1/devices/{device_id}/telemetry",
                            json=mirror,
                        )
                        if response.status_code < 400:
                            self.coord._last_send_at[device_id] = now_ts
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
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "device-mirror loop iteration error: %s", err,
                )
            await asyncio.sleep(PER_DEVICE_MIRROR_INTERVAL)

    async def state_resync_loop(self) -> None:
        """Periodischer Pull von GET /api/v1/devices als SSE-Drop-
        Backstop. Vergleicht Backend-State (is_active, is_on, cool_on)
        mit lokalem Cache; bei Drift Cache-Update + Re-Apply via
        coordinator._apply_device_state / _apply_cool_state.

        SSE-Limitation: Backend publisht nur bei state-Transitions,
        nicht idempotent. Connector kann zum Publish-Moment nicht
        subscribed sein (Netzwerk-Flap, HA-Restart, NAT-Idle). Polling-
        Backstop fängt das innerhalb STATE_RESYNC_INTERVAL ab.
        """
        while True:
            try:
                response = await self.coord._authenticated_request(
                    "GET", "/api/v1/devices",
                )
                if response.status_code >= 400:
                    _LOGGER.debug(
                        "state-resync GET returned %s: %s",
                        response.status_code, response.text,
                    )
                else:
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
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "state-resync loop iteration error: %s", err,
                )
            await asyncio.sleep(STATE_RESYNC_INTERVAL)
