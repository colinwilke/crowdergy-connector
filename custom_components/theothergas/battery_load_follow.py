"""Lokale Lastfolge des Akku-Entlade-Setpoints (backend#495).

Ein für den ganzen 15-min-Slot gehaltener Entlade-Setpoint speist ein,
sobald die Last hinter dem Zähler des Akkus darunter fällt. Trägt das
Akku-Kommando ``discharge`` den Key ``load_follow``, regelt der Connector
den Setpoint deshalb hier nach — nur nach unten, nie über den Setpoint des
Kommandos.

Vertrag: crowdergy-backend docs/battery-passive-cascade-v0.2.md, Abschnitt
„Entladung lokal der Last nachführen".

Regeln:
  * Defizit ``D = P_grid + P_akku`` am genannten Zähler, home-zentrisch
    (``P_akku`` > 0 = Entladung) — invariant gegen die eigene Wirkung: was
    der Akku entlädt, fehlt dem Bezug.
  * Entschieden wird auf dem Mittel von ``D`` über
    `BATTERY_LOAD_FOLLOW_AVG_S`, nie auf einem einzelnen Messwert. Bis das
    Fenster lückenlos gefüllt ist, bleibt der Setpoint stehen.
  * Läuft: Setpoint ``min(Mittel, max_kw)``, mindestens ``min_kw``. Fällt
    das Mittel unter ``stop_below_kw`` (Default ``min_kw``), geht er auf
    Setpoint 0 (Aktiv-Modus: hält, entlädt nicht). Steht: Start mit
    Mittel ≥ ``stop_below_kw`` + Puffer — dieselbe Schwelle, die ihn
    angehalten hat, plus Hysterese; nicht ``min_kw``, sonst bliebe er bei
    einer Last zwischen Stopp-Schwelle und Mindestleistung stehen.
  * Fehlt eine Messung oder ist sie eingefroren: sofort Setpoint 0, das
    Fenster beginnt neu.
  * Schreibt über `_apply_battery_setpoint` — Consent, Write-Breaker und
    Clamp gelten. Ein Write nur bei einer Änderung ab
    `BATTERY_LOAD_FOLLOW_WRITE_STEP_KW`.
  * Jedes Akku-Kommando ohne ``load_follow``, AI aus und der Lease-Ablauf
    beenden den Regler. Ohne Erneuerung nach
    `BATTERY_LOAD_FOLLOW_REFRESH_TIMEOUT_S` schreibt er ``passive`` und
    endet.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any

from .const import CONF_DEVICE_TYPE

_LOGGER = logging.getLogger(__name__)

BATTERY_LOAD_FOLLOW_INTERVAL_S = 15.0
# Mittelungs-Fenster des Defizits: der Regler folgt einer Last, die anhält,
# nicht einem einzelnen Messwert.
BATTERY_LOAD_FOLLOW_AVG_S = 60.0
# Hysterese über der Stopp-Schwelle für den Wiederanlauf.
BATTERY_LOAD_FOLLOW_START_BUFFER_KW = 0.1
BATTERY_LOAD_FOLLOW_STEP_KW = 0.05
BATTERY_LOAD_FOLLOW_WRITE_STEP_KW = 0.1
# Das Backend erneuert die Freigabe jeden Solver-Tick (15 min).
BATTERY_LOAD_FOLLOW_REFRESH_TIMEOUT_S = 3600.0
BATTERY_LOAD_FOLLOW_WRITE_RETRY_S = 60.0


@dataclass
class BatteryLoadFollowParams:
    """Die Freigabe aus dem ``load_follow``-Key des Akku-Kommandos."""

    grid_device_id: str
    max_kw: float
    min_kw: float
    stop_below_kw: float

    @classmethod
    def from_frame(cls, data: Any) -> BatteryLoadFollowParams | None:
        if not isinstance(data, dict):
            return None
        try:
            max_kw = float(data["max_kw"])
            min_kw = float(data.get("min_kw") or 0.0)
            raw_stop = data.get("stop_below_kw")
            stop_kw = min_kw if raw_stop is None else float(raw_stop)
            grid = str(data["grid_device_id"])
        except (KeyError, TypeError, ValueError):
            return None
        if (
            not grid or max_kw <= 0.0 or min_kw < 0.0 or stop_kw < 0.0
            or min_kw > max_kw or stop_kw > min_kw
        ):
            return None
        return cls(
            grid_device_id=grid, max_kw=max_kw, min_kw=min_kw,
            stop_below_kw=stop_kw,
        )


@dataclass
class BatteryLoadFollowRun:
    params: BatteryLoadFollowParams
    refreshed_at: float
    setpoint_kw: float = 0.0  # zuletzt geschriebene Entladeleistung (≥ 0)
    samples: list[tuple[float, float]] = field(default_factory=list)
    series_since: float | None = None  # Beginn der lückenlosen Messreihe
    retry_at: float = 0.0

    @property
    def running(self) -> bool:
        return self.setpoint_kw > 0.0


def battery_deficit_kw(
    grid_kw: float | None, battery_kw: float | None
) -> float | None:
    """Pure: Bezug, den der Zähler ohne Akku hätte. None, wenn eine
    Messung fehlt."""
    if grid_kw is None or battery_kw is None:
        return None
    return float(grid_kw) + float(battery_kw)


def _grid(kw: float) -> float:
    steps = math.floor(kw / BATTERY_LOAD_FOLLOW_STEP_KW + 1e-9)
    return round(steps * BATTERY_LOAD_FOLLOW_STEP_KW, 3)


def mean_deficit_kw(
    run: BatteryLoadFollowRun, d_kw: float | None, now: float
) -> float | None:
    """Pure: Messwert in die Reihe aufnehmen und das Mittel über
    `BATTERY_LOAD_FOLLOW_AVG_S` liefern — None, solange die Reihe das
    Fenster nicht lückenlos füllt. Eine fehlende Messung leert die Reihe."""
    if d_kw is None:
        run.samples.clear()
        run.series_since = None
        return None
    if run.series_since is None:
        run.series_since = now
    run.samples.append((now, float(d_kw)))
    cutoff = now - BATTERY_LOAD_FOLLOW_AVG_S
    run.samples = [s for s in run.samples if s[0] > cutoff - 1e-9]
    if now - run.series_since < BATTERY_LOAD_FOLLOW_AVG_S - 1e-9:
        return None
    return sum(v for _, v in run.samples) / len(run.samples)


def decide_discharge(
    run: BatteryLoadFollowRun, d_kw: float | None, now: float
) -> float | None:
    """Pure Regler-Entscheidung: neue Entladeleistung (kW, ≥ 0) oder None =
    kein Write. Mutiert nur die Messreihe im ``run``."""
    p = run.params
    current = run.setpoint_kw
    mean = mean_deficit_kw(run, d_kw, now)
    if d_kw is None:
        return 0.0 if current > 0.0 else None
    if mean is None:
        return None
    if not run.running:
        if mean >= p.stop_below_kw + BATTERY_LOAD_FOLLOW_START_BUFFER_KW:
            target = _grid(min(max(mean, p.min_kw), p.max_kw))
            return target if target > 0.0 else None
        return None
    if mean < p.stop_below_kw:
        return 0.0
    target = _grid(min(max(mean, p.min_kw), p.max_kw))
    if abs(target - current) >= BATTERY_LOAD_FOLLOW_WRITE_STEP_KW - 1e-9:
        return target
    return None


class BatteryLoadFollowMixin:
    """Regler-Lebenszyklus am Coordinator. Nutzt `_dev`,
    `_signed_power_kw` und `_ai_off` der anderen Mixins."""

    def _battery_load_follow_runs(self) -> dict[str, BatteryLoadFollowRun]:
        runs = getattr(self, "_battery_load_follow_run_state", None)
        if runs is None:
            runs = self._battery_load_follow_run_state = {}
        return runs

    def _battery_load_follow_tasks(self) -> dict[str, asyncio.Task]:
        tasks = getattr(self, "_battery_load_follow_task_state", None)
        if tasks is None:
            tasks = self._battery_load_follow_task_state = {}
        return tasks

    async def _start_battery_load_follow(
        self,
        device_id: str,
        params: BatteryLoadFollowParams,
        setpoint_kw: float,
    ) -> None:
        """``discharge`` mit ``load_follow``: Regler starten bzw. erneuern.
        ``setpoint_kw`` ist die Entladeleistung des Kommandos (≥ 0)."""
        runs = self._battery_load_follow_runs()
        run = runs.get(device_id)
        if run is not None:
            # Erneuerung: Grenzen und Freigabe-Uhr wandern, die Messreihe
            # bleibt. Über dem neuen Deckel wird sofort gesenkt.
            run.params = params
            run.refreshed_at = time.time()
            if run.setpoint_kw > params.max_kw + 1e-9:
                target = _grid(params.max_kw)
                if await self._apply_battery_setpoint(
                    device_id, "discharge", -target,
                ):
                    run.setpoint_kw = target
            return
        setpoint_kw = _grid(max(0.0, min(setpoint_kw, params.max_kw)))
        mode = "discharge" if setpoint_kw > 0.0 else "freeze"
        if not await self._apply_battery_setpoint(
            device_id, mode, -setpoint_kw,
        ):
            # Kein Consent / Write gescheitert: kein Regler — die nächste
            # Erneuerung des Backends versucht es wieder.
            return
        runs[device_id] = BatteryLoadFollowRun(
            params=params, refreshed_at=time.time(), setpoint_kw=setpoint_kw,
        )
        _LOGGER.warning(
            "battery load_follow: lokale Lastfolge für %s gestartet "
            "(%.2f kW, Zähler %s)", device_id, setpoint_kw,
            params.grid_device_id,
        )
        self._battery_load_follow_tasks()[device_id] = (
            self.hass.async_create_background_task(
                self._battery_load_follow_loop(device_id),
                name=f"theothergas_battery_load_follow_{device_id}",
            )
        )

    def _stop_battery_load_follow(self, device_id: str, *, reason: str) -> None:
        run = self._battery_load_follow_runs().pop(device_id, None)
        task = self._battery_load_follow_tasks().pop(device_id, None)
        if (
            task is not None and not task.done()
            and task is not asyncio.current_task()
        ):
            task.cancel()
        if run is not None:
            _LOGGER.warning(
                "battery load_follow: Regler für %s beendet (%s)",
                device_id, reason,
            )

    def _cancel_battery_load_follow_all(self) -> None:
        for task in list(self._battery_load_follow_tasks().values()):
            task.cancel()
        self._battery_load_follow_tasks().clear()
        self._battery_load_follow_runs().clear()

    async def _battery_load_follow_loop(self, device_id: str) -> None:
        while True:
            await asyncio.sleep(BATTERY_LOAD_FOLLOW_INTERVAL_S)
            run = self._battery_load_follow_runs().get(device_id)
            if run is None:
                return
            if self._ai_off(device_id):
                self._stop_battery_load_follow(device_id, reason="AI aus")
                return
            now = time.time()
            if now - run.refreshed_at > BATTERY_LOAD_FOLLOW_REFRESH_TIMEOUT_S:
                self._stop_battery_load_follow(
                    device_id, reason="keine Erneuerung",
                )
                await self._apply_battery_setpoint(device_id, "passive", 0.0)
                return
            try:
                await self._battery_load_follow_tick(device_id, run, now)
            except Exception:  # der Regler darf nie sterben
                _LOGGER.exception(
                    "battery load_follow tick failed for %s", device_id
                )

    async def _battery_load_follow_tick(
        self, device_id: str, run: BatteryLoadFollowRun, now: float
    ) -> None:
        dev = self._dev(device_id)
        if dev is None or dev.get(CONF_DEVICE_TYPE) != "battery":
            self._stop_battery_load_follow(device_id, reason="Gerät weg")
            return
        d_kw = battery_deficit_kw(
            self._signed_power_kw(self._dev(run.params.grid_device_id), now),
            self._signed_power_kw(dev, now),
        )
        target = decide_discharge(run, d_kw, now)
        if target is None or now < run.retry_at:
            return
        d_txt = f"{d_kw:.2f}" if d_kw is not None else "—"
        log = (
            _LOGGER.warning
            if (target == 0.0) != (run.setpoint_kw == 0.0) else _LOGGER.debug
        )
        log(
            "battery load_follow %s: Entladung %.2f → %.2f kW (Defizit %s kW)",
            device_id, run.setpoint_kw, target, d_txt,
        )
        mode = "discharge" if target > 0.0 else "freeze"
        ok = await self._apply_battery_setpoint(device_id, mode, -target)
        if not ok:
            run.retry_at = now + BATTERY_LOAD_FOLLOW_WRITE_RETRY_S
            return
        run.setpoint_kw = target
