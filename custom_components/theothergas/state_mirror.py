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

    # Letzter Wallbox-Ladestrom (ganze Ampere) pro Device, den der
    # Solver im „An"/Power-Modus gewählt hat. Wird vom charge_mode_hold_
    # loop zusammen mit dem Modus re-geschrieben (manche Boxen resetten
    # den Strom). Nur gesetzt für Boxen mit gemappter Ladestrom-Entity
    # im Power-Modus; sonst leer (= volle Leistung / kein Strom-Write).
    held_charge_current: dict[str, int] = field(default_factory=dict)

    # Letzte Wallbox-Phasenzahl (1|3) pro Device (2026-07-19). Vom
    # Solver im Power-Modus explizit kommandiert (nie „Auto"); der
    # charge_mode_hold_loop schreibt sie zusammen mit Strom + Modus
    # re. Nur gesetzt für Boxen mit gemappter Phasen-Entity.
    held_charge_phases: dict[str, int] = field(default_factory=dict)

    # Lease-Expiry-Tracker (#B2, 2026-08-12) — pro Device ein One-shot-
    # Task, gestartet vom SSE-Stale-Bail des charge_mode_hold_loop:
    # nach COMMAND_LEASE_TTL_S ohne SSE-Event schreibt er EINMAL den
    # per-Typ-Safe-Default (Wallbox → Solar wenn gemappt, Batterie →
    # Passiv), damit ein stickiger Mode-Select nicht die ganze Outage
    # auf dem letzten Cloud-Kommando latcht. Frisches Kommando /
    # AI-off / Removal canceln ihn.
    charge_mode_lease_tasks: dict[str, asyncio.Task] = field(default_factory=dict)

    # Wall-Clock des letzten SSE-Events (any Type). Hold-Loops gaten
    # darauf via SSE_STALE_THRESHOLD_S damit ein Backend-Outage die
    # periodische Re-Write-Logik pausiert.
    last_sse_event_at: float = 0.0

    # (#136, 2026-08-25) Schreib-Circuit-Breaker: je Entity das
    # laufende Stunden-Fenster als (window_start, count). Über
    # WRITE_BREAKER_MAX_PER_HOUR blockt `_write_allowed` jeden
    # weiteren Write bis zur nächsten Stunde.
    entity_write_counts: dict[str, tuple[float, int]] = field(
        default_factory=dict
    )

    # (#136) Geräte, deren Breaker gerade getrippt ist (device_id →
    # Trip-Zeit). Speist das `write_breaker`-Telemetrie-Flag; gecleart,
    # sobald ein Write nach dem Fenster-Rollover wieder durchgeht.
    write_breaker_devices: dict[str, float] = field(default_factory=dict)

    # (#140) Manuelle Übersteuerung: device_id → Wall-Clock, bis zu der
    # die Steuerung dieses Geräts pausiert ist. Speist das
    # `local_override`-Telemetrie-Flag; nach Ablauf übernimmt der
    # Self-Heal-Loop / der nächste MPC-Tick automatisch wieder.
    local_override_until: dict[str, float] = field(default_factory=dict)

    # (#140) Wall-Clock des letzten EIGENEN Service-Calls je Entity —
    # die Referenz, gegen die der AUTO-Hold „Drift von uns" von
    # „Nutzer-Eingriff" trennt (LOCAL_OVERRIDE_GRACE_S).
    last_own_write_at: dict[str, float] = field(default_factory=dict)

    # (#152) Der Wert, den wir zuletzt WIRKLICH auf die Steuer-Entity
    # eines Geräts geschrieben haben — nach dem #135-Clamp, also das,
    # was am Gerät ankommen konnte, nicht was konfiguriert ist. Quelle
    # für den Wirkungs-Vergleich gegen den gefahrenen Sollwert.
    last_written_value: dict[str, object] = field(default_factory=dict)

    # (#152) Geräte, deren konfigurierter Schaltwert ausserhalb des von
    # der Steuer-Entity akzeptierten Bereichs liegt (device_id →
    # Zeitpunkt der Feststellung). Der Wert kann das Gerät damit nie
    # erreichen; speist das `control_value_rejected`-Telemetrie-Flag
    # und wird gecleart, sobald ein Write ungeklemmt durchgeht.
    value_rejected_devices: dict[str, float] = field(default_factory=dict)

    # (#152) Beginn einer anhaltenden Abweichung zwischen geschriebenem
    # und gefahrenem Sollwert (device_id → Wall-Clock). Erst nach
    # CONTROL_EFFECT_MIN_MISMATCH_S wird daraus ein Befund — ein Gerät
    # darf rampen und verzögert übernehmen.
    effective_mismatch_since: dict[str, float] = field(default_factory=dict)
