"""DeviceStateMirror — per-device state-Cache extrahiert aus
coordinator.py (FEAT-5 Phase A, 2026-06-09).

Phase A des Coordinator-Splits aus dem Multi-Repo-Review 2026-06-09
Finding #1. Ziel: die acht über die Coordinator-Klasse verstreuten
State-Dicts in einem testbaren Wrapper konsolidieren, OHNE Verhalten
zu ändern. Die alten Attribut-Namen bleiben über @property-Shims in
der Coordinator-Klasse verfügbar, sodass die ~250 bestehenden Read-/
Write-Sites unverändert weiterlaufen.

Phase B (separater Sprint): pro Cluster (active_state / on_state /
cool_state / hold-tasks / charge-mode) die Direct-Attribute-Accesses
durch typed Mirror-Methoden ersetzen (`set_active(id, value)` statt
`self._active_state[id] = value`).

Memo: `project_connector_coordinator_split_plan_2026_06_09.md`.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


@dataclass
class DeviceStateMirror:
    """Single-Source-of-Truth für den Per-Device-Live-State im
    Connector. Authoritative-Quelle bleibt das Backend; der Mirror
    cached die letzten bekannten Werte damit HA-Entities lokal lesen
    können ohne Backend-Roundtrip pro Tick.

    Bewusst KEINE Methods außer den Helper-Konstruktor — Phase A ist
    reine Storage-Extraktion. Phase B baut die typed-Accessor-API
    drauf (`set_active`, `get_cool`, …) und migriert Call-Sites.
    """

    # Crowdergize-State (= Backend devices.is_active gespiegelt). Mit
    # Bootstrap-Flag damit `_async_update_data` vor erstem Refresh-Tick
    # nichts schreibt was vom Backend überschrieben wird.
    active_state: dict[str, bool] = field(default_factory=dict)
    active_state_bootstrapped: bool = False

    # Pro-Device Soll-Zustand (Heating/WW/Aircon-Heat — Boolean ON/OFF).
    on_state: dict[str, bool] = field(default_factory=dict)

    # Pro-Device Cooling-State (Aircon/SG-Ready). Mutex mit on_state
    # wird Solver-side erzwungen, hier bewusst separat damit Dispatch
    # auf getrennte HA-Entities möglich ist.
    cool_state: dict[str, bool] = field(default_factory=dict)

    # Hold-Loop-Tracker — pro Device ein Task der das entity_control
    # gegen User-/HA-Drift hält. Neue Apply ersetzt den alten Task.
    hold_tasks: dict[str, asyncio.Task] = field(default_factory=dict)

    # Hold-Loop-Tracker für Charge-Mode (Battery + Wallbox) — separater
    # Loop weil manche Inverter den Mode autonom resetten.
    charge_mode_hold_tasks: dict[str, asyncio.Task] = field(default_factory=dict)

    # Letzter charge_mode-Wert pro Device, dient als Re-Write-Quelle
    # für den charge_mode_hold_loop.
    held_charge_mode: dict[str, str] = field(default_factory=dict)

    # Wall-Clock des letzten SSE-Events (any Type). Hold-Loops gaten
    # darauf via SSE_STALE_THRESHOLD_S damit ein Backend-Outage die
    # periodische Re-Write-Logik pausiert.
    last_sse_event_at: float = 0.0
