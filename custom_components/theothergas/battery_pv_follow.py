"""Lokale PV-Nachregelung des Akku-Lade-Setpoints (backend#401).

Das Backend setzt einen Akku per PV-Lade-Guard auf ``charge``, wenn PV aus
seinem Subtree abfließt, die passiv niemand in den Akku bringt. Ein für den
ganzen 15-min-Slot gehaltener Setpoint lädt bei Wolken aus dem Netz. Trägt
das Akku-Kommando den Key ``pv_follow``, regelt der Connector den Setpoint
deshalb hier im `BATTERY_PV_FOLLOW_INTERVAL_S`-Takt auf den gemessenen
Überschuss nach.

Vertrag: crowdergy-backend docs/battery-passive-cascade-v0.2.md, Abschnitt
„PV-Ladung lokal nachregeln".

Regeln:
  * Überschuss ``S = −(P_grid + P_akku)`` am genannten Zähler,
    home-zentrisch (``P_akku`` > 0 = Entladung) — invariant gegen die
    eigene Wirkung: was der Akku lädt, fehlt dem Export.
  * Läuft: Setpoint ``min(S, max_kw)``, nie über ``S``. Fällt ``S`` unter
    ``min_kw``, hält er ``min_kw`` höchstens `BATTERY_PV_FOLLOW_STOP_DELAY_S`
    und geht dann auf Setpoint 0 (Aktiv-Modus: hält, entlädt nicht).
    Steht: Start nach `BATTERY_PV_FOLLOW_START_DELAY_S` mit
    ``S ≥ min_kw + Puffer``.
  * Fehlt eine Messung oder ist sie eingefroren: sofort Setpoint 0.
  * Schreibt über `_apply_battery_setpoint` — Consent, Write-Breaker und
    Clamp gelten. Ein Write nur bei einer Änderung ab
    `BATTERY_PV_FOLLOW_WRITE_STEP_KW` (abwärts sofort).
  * Jedes Akku-Kommando ohne ``pv_follow`` und AI aus beenden den Regler
    ohne Write. Ohne Erneuerung nach `BATTERY_PV_FOLLOW_REFRESH_TIMEOUT_S`
    schreibt er ``passive`` und endet. Er lädt nur aus gemessenem
    Überschuss — auch bei toter Cloud nie aus dem Netz.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from typing import Any

from .const import CONF_DEVICE_TYPE

_LOGGER = logging.getLogger(__name__)

BATTERY_PV_FOLLOW_INTERVAL_S = 15.0
BATTERY_PV_FOLLOW_START_DELAY_S = 30.0
BATTERY_PV_FOLLOW_STOP_DELAY_S = 30.0
BATTERY_PV_FOLLOW_START_BUFFER_KW = 0.2
# Setpoint-Raster und Mindeständerung für einen Write (aufwärts).
BATTERY_PV_FOLLOW_STEP_KW = 0.05
BATTERY_PV_FOLLOW_WRITE_STEP_KW = 0.15
# Das Backend erneuert die Freigabe jeden Solver-Tick (15 min); eine
# Stunde ohne Erneuerung heißt: der Guard greift nicht mehr oder die
# Cloud schweigt → zurück auf passiv.
BATTERY_PV_FOLLOW_REFRESH_TIMEOUT_S = 3600.0
BATTERY_PV_FOLLOW_WRITE_RETRY_S = 60.0


@dataclass
class BatteryPvFollowParams:
    """Die Freigabe aus dem ``pv_follow``-Key des Akku-Kommandos."""

    grid_device_id: str
    max_kw: float
    min_kw: float

    @classmethod
    def from_frame(cls, data: Any) -> "BatteryPvFollowParams | None":
        if not isinstance(data, dict):
            return None
        try:
            max_kw = float(data["max_kw"])
            min_kw = float(data.get("min_kw") or 0.0)
            grid = str(data["grid_device_id"])
        except (KeyError, TypeError, ValueError):
            return None
        if not grid or max_kw <= 0.0 or min_kw < 0.0:
            return None
        return cls(grid_device_id=grid, max_kw=max_kw, min_kw=min_kw)


@dataclass
class BatteryPvFollowRun:
    params: BatteryPvFollowParams
    refreshed_at: float
    setpoint_kw: float = 0.0  # zuletzt erfolgreich geschriebener Setpoint
    above_since: float | None = None
    below_since: float | None = None
    retry_at: float = 0.0

    @property
    def running(self) -> bool:
        return self.setpoint_kw > 0.0


def battery_surplus_kw(
    grid_kw: float | None, battery_kw: float | None
) -> float | None:
    """Pure: PV, die den Zähler ohne Akku verließe. None, wenn eine Messung
    fehlt."""
    if grid_kw is None or battery_kw is None:
        return None
    return -(float(grid_kw) + float(battery_kw))


def _grid(kw: float) -> float:
    steps = math.floor(kw / BATTERY_PV_FOLLOW_STEP_KW + 1e-9)
    return round(steps * BATTERY_PV_FOLLOW_STEP_KW, 3)


def decide_setpoint(
    run: BatteryPvFollowRun, s_kw: float | None, now: float
) -> float | None:
    """Pure Regler-Entscheidung: neuer Setpoint (kW) oder None = kein Write.
    Mutiert nur die Timer im ``run``."""
    p = run.params
    current = run.setpoint_kw
    if s_kw is None:
        run.above_since = run.below_since = None
        return 0.0 if current > 0.0 else None
    if not run.running:
        run.below_since = None
        if s_kw >= p.min_kw + BATTERY_PV_FOLLOW_START_BUFFER_KW:
            if run.above_since is None:
                run.above_since = now
            if now - run.above_since >= BATTERY_PV_FOLLOW_START_DELAY_S:
                target = _grid(min(s_kw, p.max_kw))
                return target if target > 0.0 else None
        else:
            run.above_since = None
        return None
    run.above_since = None
    if s_kw < p.min_kw:
        if run.below_since is None:
            run.below_since = now
        if now - run.below_since >= BATTERY_PV_FOLLOW_STOP_DELAY_S:
            return 0.0
        target = p.min_kw
    else:
        run.below_since = None
        target = min(s_kw, p.max_kw)
    target = _grid(max(target, 0.0))
    if target < current - 1e-9:
        return target
    if target - current >= BATTERY_PV_FOLLOW_WRITE_STEP_KW - 1e-9:
        return target
    return None


class BatteryPvFollowMixin:
    """Regler-Lebenszyklus am Coordinator. Nutzt `_dev`,
    `_signed_power_kw` und `_ai_off` der anderen Mixins."""

    def _battery_pv_follow_runs(self) -> dict[str, BatteryPvFollowRun]:
        runs = getattr(self, "_battery_pv_follow_run_state", None)
        if runs is None:
            runs = self._battery_pv_follow_run_state = {}
        return runs

    def _battery_pv_follow_tasks(self) -> dict[str, asyncio.Task]:
        tasks = getattr(self, "_battery_pv_follow_task_state", None)
        if tasks is None:
            tasks = self._battery_pv_follow_task_state = {}
        return tasks

    async def _handle_battery_command(
        self, device_id: str, data: dict[str, Any]
    ) -> None:
        """Akku-``set_charge_mode``: mit ``pv_follow`` Regler starten bzw.
        erneuern, sonst einen laufenden Regler beenden. Danach den
        Befehl wie gehabt anwenden."""
        mode = data.get("mode") or "passive"
        raw_sp = data.get("setpoint_kw")
        setpoint_kw = float(raw_sp) if raw_sp is not None else 0.0
        params = (
            BatteryPvFollowParams.from_frame(data.get("pv_follow"))
            if mode == "charge" else None
        )
        if params is None:
            self._stop_battery_pv_follow(device_id, reason="Solver-Dispatch")
            await self._apply_battery_setpoint(device_id, mode, setpoint_kw)
            return
        runs = self._battery_pv_follow_runs()
        run = runs.get(device_id)
        if run is not None:
            # Erneuerung: der Regler behält seinen Setpoint, nur Grenzen
            # und Freigabe-Uhr wandern.
            run.params = params
            run.refreshed_at = time.time()
            return
        setpoint_kw = _grid(max(0.0, min(setpoint_kw, params.max_kw)))
        if not await self._apply_battery_setpoint(
            device_id, "charge", setpoint_kw,
        ):
            # Kein Consent / Write gescheitert: kein Regler — die nächste
            # Erneuerung des Backends versucht es wieder.
            return
        runs[device_id] = BatteryPvFollowRun(
            params=params, refreshed_at=time.time(), setpoint_kw=setpoint_kw,
        )
        _LOGGER.warning(
            "battery pv_follow: lokale PV-Nachregelung für %s gestartet "
            "(%.2f kW, Zähler %s)", device_id, setpoint_kw,
            params.grid_device_id,
        )
        self._battery_pv_follow_tasks()[device_id] = (
            self.hass.async_create_background_task(
                self._battery_pv_follow_loop(device_id),
                name=f"theothergas_battery_pv_follow_{device_id}",
            )
        )

    def _stop_battery_pv_follow(self, device_id: str, *, reason: str) -> None:
        run = self._battery_pv_follow_runs().pop(device_id, None)
        task = self._battery_pv_follow_tasks().pop(device_id, None)
        if (
            task is not None and not task.done()
            and task is not asyncio.current_task()
        ):
            task.cancel()
        if run is not None:
            _LOGGER.warning(
                "battery pv_follow: Regler für %s beendet (%s)",
                device_id, reason,
            )

    def _cancel_battery_pv_follow_all(self) -> None:
        for task in list(self._battery_pv_follow_tasks().values()):
            task.cancel()
        self._battery_pv_follow_tasks().clear()
        self._battery_pv_follow_runs().clear()

    async def _battery_pv_follow_loop(self, device_id: str) -> None:
        while True:
            await asyncio.sleep(BATTERY_PV_FOLLOW_INTERVAL_S)
            run = self._battery_pv_follow_runs().get(device_id)
            if run is None:
                return
            if self._ai_off(device_id):
                self._stop_battery_pv_follow(device_id, reason="AI aus")
                return
            now = time.time()
            if now - run.refreshed_at > BATTERY_PV_FOLLOW_REFRESH_TIMEOUT_S:
                self._stop_battery_pv_follow(
                    device_id, reason="keine Erneuerung",
                )
                await self._apply_battery_setpoint(device_id, "passive", 0.0)
                return
            try:
                await self._battery_pv_follow_tick(device_id, run, now)
            except Exception:  # noqa: BLE001 — der Regler darf nie sterben
                _LOGGER.exception(
                    "battery pv_follow tick failed for %s", device_id
                )

    async def _battery_pv_follow_tick(
        self, device_id: str, run: BatteryPvFollowRun, now: float
    ) -> None:
        dev = self._dev(device_id)
        if dev is None or dev.get(CONF_DEVICE_TYPE) != "battery":
            self._stop_battery_pv_follow(device_id, reason="Gerät weg")
            return
        s_kw = battery_surplus_kw(
            self._signed_power_kw(self._dev(run.params.grid_device_id), now),
            self._signed_power_kw(dev, now),
        )
        target = decide_setpoint(run, s_kw, now)
        if target is None or now < run.retry_at:
            return
        s_txt = f"{s_kw:.2f}" if s_kw is not None else "—"
        log = _LOGGER.warning if (target == 0.0) != (run.setpoint_kw == 0.0) else _LOGGER.debug
        log(
            "battery pv_follow %s: Setpoint %.2f → %.2f kW (Überschuss %s kW)",
            device_id, run.setpoint_kw, target, s_txt,
        )
        mode = "charge" if target > 0.0 else "freeze"
        ok = await self._apply_battery_setpoint(device_id, mode, target)
        if not ok:
            run.retry_at = now + BATTERY_PV_FOLLOW_WRITE_RETRY_S
            return
        run.setpoint_kw = target
