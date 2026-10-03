"""Lokaler PV-Überschuss-Regler für Wallboxen (#292).

Der Solver-Tick (15 min) ist für Wolken und Lastwechsel zu langsam. Für
Boxen mit variablem Ladestrom regelt der Connector den Ladestrom hier im
`PV_FOLLOW_INTERVAL_S`-Takt auf den gemessenen Überschuss. Das Backend
gibt den Regler je Box frei (`pv_follow`-Frame: Netzanschluss, passive
Akkus, Grenzen) und erneuert die Freigabe je Solver-Tick.

Vertrag: crowdergy-backend docs/wallbox-charge-strategies.md, Abschnitt
„Lokaler PV-Überschuss-Regler".

Regeln:
  * Überschuss ``S = −P_grid + P_box + Σ P_akku_lädt`` — invariant gegen
    die eigene Wirkung (was die Box zieht, fehlt Export oder Akku).
    Akku-Ladung zählt nur, wenn jeder genannte Akku trotzdem voll wird
    (SoC ≥ ``soc_start_pct``, läuft die Box: ≥ ``soc_hold_pct``).
  * Start nach `PV_FOLLOW_START_DELAY_S` mit ``S ≥ min_kw + Puffer``,
    Stopp nach `PV_FOLLOW_STOP_DELAY_S` mit ``S < min_kw``;
    Mindestlaufzeit / Mindestpause gegen Takten.
  * Fehlt eine Messung oder ist das Auto nicht gesteckt: kein Start, eine
    laufende Box stoppt sofort.
  * Schreibt über `_apply_charge_mode(schedule_hold=False)` — damit gelten
    Consent, Write-Breaker, Clamp und die Reihenfolge Phase → Strom → Modus.
  * Läuft bei toter Cloud weiter (lädt nur aus gemessenem Überschuss); bei
    lebender Cloud ohne Erneuerung endet er nach
    `PV_FOLLOW_REFRESH_TIMEOUT_S`.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from typing import Any

from .const import (
    CONF_CHARGE_MODE_VALUE_LOCK,
    CONF_CHARGE_MODE_VALUE_POWER,
    CONF_CHARGE_MODE_VALUE_SOLAR,
    CONF_DEVICE_ID,
    CONF_DEVICE_TYPE,
    CONF_ENTITY_POWER,
    CONF_ENTITY_POWER_2,
    CONF_ENTITY_SOC,
    CONF_ENTITY_VEHICLE_STATUS,
    CONF_ENTITY_WALLBOX_CHARGE_CURRENT,
    CONF_INVERT_POWER_SIGN,
    SSE_STALE_THRESHOLD_S,
)

_LOGGER = logging.getLogger(__name__)

PV_FOLLOW_INTERVAL_S = 15.0
PV_FOLLOW_START_DELAY_S = 60.0
PV_FOLLOW_STOP_DELAY_S = 180.0
PV_FOLLOW_MIN_ON_S = 300.0
PV_FOLLOW_MIN_OFF_S = 120.0
# > Stale-Failsafe des Backends (45 min), damit der Regler einen
# Solver-Ausfall überbrückt, bis der Failsafe die Freigabe erneuert.
PV_FOLLOW_REFRESH_TIMEOUT_S = 3600.0
# Eigene Boxleistung, ab der die Box als „lädt" gilt (Übernahme eines
# laufenden Ladevorgangs beim Start des Reglers).
PV_FOLLOW_RUNNING_KW = 0.5


@dataclass
class PvFollowParams:
    """Die Freigabe aus dem `pv_follow`-Frame."""

    grid_device_id: str
    battery_device_ids: tuple[str, ...]
    min_current_a: int
    max_current_a: int
    kw_per_amp: float
    phases: int | None
    soc_start_pct: float
    soc_hold_pct: float
    start_buffer_kw: float

    @property
    def min_kw(self) -> float:
        return self.min_current_a * self.kw_per_amp

    @classmethod
    def from_frame(cls, data: dict[str, Any]) -> "PvFollowParams | None":
        try:
            phases = data.get("phases")
            return cls(
                grid_device_id=str(data["grid_device_id"]),
                battery_device_ids=tuple(
                    str(b) for b in data.get("battery_device_ids") or ()
                ),
                min_current_a=int(data.get("min_current_a", 6)),
                max_current_a=int(data.get("max_current_a", 16)),
                kw_per_amp=float(data.get("kw_per_amp", 0.69)),
                phases=int(phases) if phases is not None else None,
                soc_start_pct=float(data.get("soc_start_pct", 80.0)),
                soc_hold_pct=float(data.get("soc_hold_pct", 75.0)),
                start_buffer_kw=float(data.get("start_buffer_kw", 0.5)),
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass
class PvFollowRun:
    """Laufzeit-Zustand des Reglers einer Box."""

    params: PvFollowParams
    refreshed_at: float
    charging: bool | None = None  # None = noch nicht übernommen
    amps: int | None = None
    above_since: float | None = None
    below_since: float | None = None
    switched_at: float = 0.0


def surplus_kw(
    params: PvFollowParams,
    *,
    grid_kw: float | None,
    box_kw: float | None,
    batteries: list[tuple[float | None, float | None]],
    running: bool,
) -> float | None:
    """Pure: nutzbarer Überschuss oder None, wenn Grid/Box nicht messbar.

    ``batteries`` = ``(power_kw, soc_pct)`` je genanntem Akku
    (home-zentrisch: − = lädt)."""
    if grid_kw is None or box_kw is None:
        return None
    s = -grid_kw + max(0.0, box_kw)
    if not batteries:
        return s
    soc_min = params.soc_hold_pct if running else params.soc_start_pct
    fills = all(soc is not None and soc >= soc_min for _p, soc in batteries)
    if fills:
        s += sum(-p for p, _soc in batteries if p is not None and p < 0.0)
    return s


def decide(
    run: PvFollowRun, s_kw: float | None, plugged: bool, now: float
) -> tuple[str, int | None]:
    """Pure Regler-Entscheidung. Returns ``(action, amps)`` mit action ∈
    ``start`` / ``adjust`` / ``stop`` / ``hold``. Mutiert die Timer im
    ``run`` (nicht ``charging``/``amps`` — das macht der Aufrufer nach
    dem Write)."""
    p = run.params
    charging = bool(run.charging)
    if s_kw is None or not plugged:
        run.above_since = run.below_since = None
        return ("stop", None) if charging else ("hold", None)
    amps = int(math.floor(s_kw / p.kw_per_amp + 1e-9))
    amps = max(p.min_current_a, min(p.max_current_a, amps))
    if not charging:
        run.below_since = None
        if s_kw >= p.min_kw + p.start_buffer_kw:
            if run.above_since is None:
                run.above_since = now
            if (
                now - run.above_since >= PV_FOLLOW_START_DELAY_S
                and now - run.switched_at >= PV_FOLLOW_MIN_OFF_S
            ):
                return "start", amps
        else:
            run.above_since = None
        return "hold", None
    run.above_since = None
    if s_kw < p.min_kw:
        if run.below_since is None:
            run.below_since = now
        if (
            now - run.below_since >= PV_FOLLOW_STOP_DELAY_S
            and now - run.switched_at >= PV_FOLLOW_MIN_ON_S
        ):
            return "stop", None
        return "hold", None
    run.below_since = None
    if amps != run.amps:
        return "adjust", amps
    return "hold", None


class PvFollowMixin:
    """Regler-Lebenszyklus am Coordinator (Mixin wie CommandDispatcher)."""

    def _pv_follow_runs(self) -> dict[str, PvFollowRun]:
        runs = getattr(self, "_pv_follow_run_state", None)
        if runs is None:
            runs = self._pv_follow_run_state = {}
        return runs

    def _pv_follow_tasks(self) -> dict[str, asyncio.Task]:
        tasks = getattr(self, "_pv_follow_task_state", None)
        if tasks is None:
            tasks = self._pv_follow_task_state = {}
        return tasks

    def _dev(self, device_id: str) -> dict[str, Any] | None:
        return next(
            (d for d in self.devices if d.get(CONF_DEVICE_ID) == device_id),
            None,
        )

    async def _start_pv_follow(self, device_id: str, data: dict[str, Any]) -> None:
        """`pv_follow`-Frame: Regler starten oder Freigabe erneuern."""
        dev = self._dev(device_id)
        if dev is None or dev.get(CONF_DEVICE_TYPE) != "wallbox":
            _LOGGER.debug("pv_follow: unknown wallbox %s — ignoring", device_id)
            return
        if not dev.get(CONF_ENTITY_WALLBOX_CHARGE_CURRENT):
            _LOGGER.warning(
                "pv_follow: %s has no charge-current entity — ignoring",
                device_id,
            )
            return
        params = PvFollowParams.from_frame(data)
        if params is None:
            _LOGGER.warning("pv_follow: malformed frame for %s", device_id)
            return
        runs = self._pv_follow_runs()
        run = runs.get(device_id)
        if run is not None:
            run.params = params
            run.refreshed_at = time.time()
            return
        # Der Regler übernimmt die Box: Hold + Lease des Dispatch abräumen.
        self._cancel_charge_mode_hold(device_id)
        runs[device_id] = PvFollowRun(params=params, refreshed_at=time.time())
        _LOGGER.warning("pv_follow: lokaler PV-Regler für %s gestartet", device_id)
        self._pv_follow_tasks()[device_id] = self.hass.async_create_background_task(
            self._pv_follow_loop(device_id),
            name=f"theothergas_pv_follow_{device_id}",
        )

    async def _stop_pv_follow(
        self, device_id: str, *, write_stop: bool, reason: str
    ) -> None:
        """Regler beenden. ``write_stop`` hinterlässt den Stopp-Zustand
        (solar, sonst lock), falls die Box gerade lädt."""
        run = self._pv_follow_runs().pop(device_id, None)
        task = self._pv_follow_tasks().pop(device_id, None)
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
        if run is None:
            return
        _LOGGER.warning("pv_follow: Regler für %s beendet (%s)", device_id, reason)
        if write_stop and run.charging:
            await self._pv_follow_write_stop(device_id)

    def _cancel_pv_follow_all(self) -> None:
        for task in list(self._pv_follow_tasks().values()):
            task.cancel()
        self._pv_follow_tasks().clear()
        self._pv_follow_runs().clear()

    async def _pv_follow_loop(self, device_id: str) -> None:
        while True:
            await asyncio.sleep(PV_FOLLOW_INTERVAL_S)
            run = self._pv_follow_runs().get(device_id)
            if run is None:
                return
            now = time.time()
            sse_fresh = (
                now - float(getattr(self.state, "last_sse_event_at", 0.0) or 0.0)
                < SSE_STALE_THRESHOLD_S
            )
            if sse_fresh and now - run.refreshed_at > PV_FOLLOW_REFRESH_TIMEOUT_S:
                await self._stop_pv_follow(
                    device_id, write_stop=True, reason="keine Erneuerung"
                )
                return
            try:
                await self._pv_follow_tick(device_id, run, now)
            except Exception:  # noqa: BLE001 — der Regler darf nie sterben
                _LOGGER.exception("pv_follow tick failed for %s", device_id)

    def _signed_power_kw(self, dev: dict[str, Any] | None) -> float | None:
        """Leistung eines Geräts wie im Telemetrie-Tick (Differenzpaar
        oder Vorzeichen-Flip), home-zentrisch."""
        if dev is None:
            return None
        power = self._read_power_kw(dev.get(CONF_ENTITY_POWER, "") or "")
        second = dev.get(CONF_ENTITY_POWER_2, "") or ""
        if second:
            p2 = self._read_power_kw(second)
            if power is not None and p2 is not None:
                return power - p2
            if power is None and p2 is not None:
                return -p2
            return power
        if power is not None and dev.get(CONF_INVERT_POWER_SIGN):
            return -power
        return power

    def _read_soc(self, dev: dict[str, Any] | None) -> float | None:
        if dev is None:
            return None
        value = self._read_entity_state(dev.get(CONF_ENTITY_SOC, "") or "")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)

    async def _pv_follow_tick(
        self, device_id: str, run: PvFollowRun, now: float
    ) -> None:
        dev = self._dev(device_id)
        if dev is None:
            await self._stop_pv_follow(device_id, write_stop=False, reason="Gerät weg")
            return
        p = run.params
        box_kw = self._signed_power_kw(dev)
        if run.charging is None:
            # Übernahme: lädt die Box schon (letzter Solver-Befehl), regelt
            # der Regler sie ab hier, ohne Start-Verzögerung.
            run.charging = box_kw is not None and box_kw > PV_FOLLOW_RUNNING_KW
            run.switched_at = now - PV_FOLLOW_MIN_ON_S if run.charging else 0.0
        status_entity = dev.get(CONF_ENTITY_VEHICLE_STATUS, "") or ""
        plugged = True
        if status_entity:
            status = self._normalised_vehicle_status(
                dev, self._read_string(status_entity)
            )
            plugged = status != "unplugged"
        s_kw = surplus_kw(
            p,
            grid_kw=self._signed_power_kw(self._dev(p.grid_device_id)),
            box_kw=box_kw,
            batteries=[
                (self._signed_power_kw(b), self._read_soc(b))
                for b in (self._dev(i) for i in p.battery_device_ids)
                if b is not None
            ],
            running=bool(run.charging),
        )
        action, amps = decide(run, s_kw, plugged, now)
        if action in ("start", "adjust") and amps is not None:
            power_value = dev.get(CONF_CHARGE_MODE_VALUE_POWER, "") or ""
            if not power_value:
                _LOGGER.warning(
                    "pv_follow: %s has no power mode mapped — cannot charge",
                    device_id,
                )
                return
            log = _LOGGER.warning if action == "start" else _LOGGER.debug
            log(
                "pv_follow %s: %s %d A (Überschuss %.2f kW)",
                device_id, action, amps, s_kw if s_kw is not None else 0.0,
            )
            await self._apply_charge_mode(
                device_id, power_value, schedule_hold=False,
                charge_current_a=amps, charge_phases=p.phases,
            )
            if action == "start":
                run.charging = True
                run.switched_at = now
            run.amps = amps
        elif action == "stop":
            _LOGGER.warning(
                "pv_follow %s: stop (Überschuss %s kW, gesteckt=%s)",
                device_id,
                f"{s_kw:.2f}" if s_kw is not None else "—", plugged,
            )
            await self._pv_follow_write_stop(device_id)
            run.charging = False
            run.amps = None
            run.switched_at = now

    async def _pv_follow_write_stop(self, device_id: str) -> None:
        dev = self._dev(device_id) or {}
        mode = (
            dev.get(CONF_CHARGE_MODE_VALUE_SOLAR, "")
            or dev.get(CONF_CHARGE_MODE_VALUE_LOCK, "")
            or ""
        )
        if not mode:
            _LOGGER.warning(
                "pv_follow: %s has neither solar nor lock mapped — "
                "cannot stop charging", device_id,
            )
            return
        await self._apply_charge_mode(device_id, mode, schedule_hold=False)
