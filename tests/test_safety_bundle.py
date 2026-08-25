"""Sicherheitsbündel #135/#136/#140 — Connector-Hälften (2026-08-25).

* #135 `_clamp_write_value`: JEDER numerische Write wird gegen die
  Grenzen der ZIEL-Entity geklemmt (climate/water_heater min_temp/
  max_temp, number/input_number min/max) — der Config-Flow begrenzt
  nur, was der Nutzer EINTRÄGT, nicht was berechnet ankommt.
* #136 `_write_allowed`: Schreib-Circuit-Breaker je (Entity, Stunde) —
  über WRITE_BREAKER_MAX_PER_HOUR wird nichts mehr geschrieben, das
  Gerät meldet `write_breaker=True` in der Telemetrie.
* #140 AUTO-Hold-Übersteuerung: Fremd-Drift (kein eigener Write in
  LOCAL_OVERRIDE_GRACE_S) → Gerät für LOCAL_OVERRIDE_HOLD_S pausieren
  statt den Menschen binnen 30 s zu überstimmen; `local_override=True`
  in der Telemetrie; NUR im AUTO-Modus (ALWAYS = Auto-Reset-Register).

Test-Mechanik (Sleep-Patch/_StopHold) gespiegelt aus
`test_hold_loops_and_eviction.py`.
"""
from __future__ import annotations

import time
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.theothergas.const import (
    CONF_DEVICE_ID,
    CONF_DEVICE_TYPE,
    CONF_ENTITY_CONTROL,
    CONF_ENTITY_CONTROL_HOLD,
    CONF_ENTITY_VORLAUF_SETPOINT,
    DOMAIN,
    ENTITY_CONTROL_HOLD_ALWAYS,
    ENTITY_CONTROL_HOLD_AUTO,
    LOCAL_OVERRIDE_GRACE_S,
    LOCAL_OVERRIDE_HOLD_S,
    WRITE_BREAKER_MAX_PER_HOUR,
)
from custom_components.theothergas.coordinator import CrowdergyCoordinator
from custom_components.theothergas.state_mirror import DeviceStateMirror

_SLEEP = "custom_components.theothergas.coordinator.asyncio.sleep"


def make_coordinator(
    hass: HomeAssistant, devices: list[dict], *, options: dict | None = None,
) -> CrowdergyCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=dict(options or {}))
    entry.add_to_hass(hass)
    coord = CrowdergyCoordinator.__new__(CrowdergyCoordinator)
    coord.hass = hass
    coord.entry = entry
    coord.devices = devices
    coord.data = None
    coord.state = DeviceStateMirror()
    coord._consent_denied_logged = set()
    coord._backend_gone_device_ids = set()
    coord._last_sent_payload = {}
    coord._last_send_at = {}
    coord._last_mirror_at = {}
    coord._last_sent_hash = {}
    coord._prev_energy_kwh = {}
    coord._prev_energy_kwh_discharged = {}
    return coord


class _StopHold(BaseException):
    pass


def _breaking_sleep(max_calls: int):
    state = {"n": 0}

    async def _sleep(_delay):
        state["n"] += 1
        if state["n"] >= max_calls:
            raise _StopHold

    return _sleep


async def _run_until_stopped(coro) -> None:
    try:
        await coro
    except _StopHold:
        pass


def _heating_device(hold: str = ENTITY_CONTROL_HOLD_AUTO) -> dict:
    return {
        CONF_DEVICE_ID: "d1",
        CONF_DEVICE_TYPE: "heating",
        CONF_ENTITY_CONTROL: "switch.kessel",
        CONF_ENTITY_CONTROL_HOLD: hold,
    }


# ════════════════════════════════════════════════════════════════════
# #135 — Clamp gegen Entity-Grenzen
# ════════════════════════════════════════════════════════════════════


async def test_clamp_uses_number_min_max(hass: HomeAssistant):
    coord = make_coordinator(hass, [])
    hass.states.async_set("number.strom", "8", {"min": 6, "max": 16})
    assert coord._clamp_write_value("number.strom", "number", 20.0) == 16.0
    assert coord._clamp_write_value("number.strom", "number", 3.0) == 6.0
    assert coord._clamp_write_value("number.strom", "number", 10.0) == 10.0


async def test_clamp_uses_climate_min_max_temp(hass: HomeAssistant):
    """Fußbodenheizungs-Fall: die globale Solver-Obergrenze (55 °C)
    kennt die Auslegung nicht — max_temp der Entity gewinnt."""
    coord = make_coordinator(hass, [])
    hass.states.async_set(
        "climate.wp", "heat", {"min_temp": 20, "max_temp": 35},
    )
    assert coord._clamp_write_value("climate.wp", "climate", 52.0) == 35.0


async def test_clamp_without_limits_is_identity(hass: HomeAssistant):
    coord = make_coordinator(hass, [])
    hass.states.async_set("number.strom", "8")  # keine min/max-Attribute
    assert coord._clamp_write_value("number.strom", "number", 42.0) == 42.0
    # Unbekannte Entity → verbatim (nie einen Write daran scheitern lassen)
    assert coord._clamp_write_value("number.gibtsnicht", "number", 42.0) == 42.0


async def test_vorlauf_setpoint_clamped_to_entity_limits(hass: HomeAssistant):
    """Der Solver-berechnete Vorlauf-Setpoint läuft durch den Clamp —
    genau der Weg, den der Config-Flow NICHT begrenzen kann."""
    dev = {
        CONF_DEVICE_ID: "d1",
        CONF_DEVICE_TYPE: "heating",
        CONF_ENTITY_VORLAUF_SETPOINT: "climate.wp",
    }
    coord = make_coordinator(hass, [dev])
    hass.states.async_set(
        "climate.wp", "heat",
        {"min_temp": 20, "max_temp": 35, "temperature": 28},
    )
    calls = async_mock_service(hass, "climate", "set_temperature")

    await coord._apply_vorlauf_setpoint("d1", 52.0)

    assert len(calls) == 1
    assert calls[0].data["temperature"] == 35.0


# ════════════════════════════════════════════════════════════════════
# #136 — Schreib-Circuit-Breaker
# ════════════════════════════════════════════════════════════════════


async def test_write_breaker_trips_over_threshold(hass: HomeAssistant):
    coord = make_coordinator(hass, [])
    for _ in range(WRITE_BREAKER_MAX_PER_HOUR):
        assert coord._write_allowed("d1", "switch.kessel") is True
    # Der (N+1)-te Write in derselben Stunde trippt.
    assert coord._write_allowed("d1", "switch.kessel") is False
    assert "d1" in coord.state.write_breaker_devices
    # Und bleibt getrippt für den Rest des Fensters.
    assert coord._write_allowed("d1", "switch.kessel") is False


async def test_write_breaker_recovers_after_window_rollover(hass: HomeAssistant):
    coord = make_coordinator(hass, [])
    # Getrippter Zustand mit einem Fenster, das vor > 1 h begann.
    coord.state.entity_write_counts["switch.kessel"] = (
        time.time() - 3601.0, WRITE_BREAKER_MAX_PER_HOUR + 50,
    )
    coord.state.write_breaker_devices["d1"] = time.time() - 3601.0

    assert coord._write_allowed("d1", "switch.kessel") is True
    # Erholt → Zustands-Flag gecleart (Backend cleart *_since via Telemetrie).
    assert "d1" not in coord.state.write_breaker_devices


async def test_tripped_breaker_blocks_entity_control_write(hass: HomeAssistant):
    """E2E: getrippter Breaker → `_apply_device_state` erreicht den
    HA-Service-Call nicht mehr."""
    coord = make_coordinator(hass, [_heating_device()])
    coord.state.entity_write_counts["switch.kessel"] = (
        time.time(), WRITE_BREAKER_MAX_PER_HOUR + 1,
    )
    hass.states.async_set("switch.kessel", "off")
    calls = async_mock_service(hass, "switch", "turn_on")

    await coord._apply_device_state("d1", True)

    assert calls == []
    assert "d1" in coord.state.write_breaker_devices


async def test_write_allowed_stamps_own_write_clock(hass: HomeAssistant):
    """#140-Kopplung: jeder erlaubte Write stempelt die Eigen-Write-Uhr,
    gegen die der AUTO-Hold Fremd-Drift erkennt."""
    coord = make_coordinator(hass, [])
    before = time.time()
    assert coord._write_allowed("d1", "switch.kessel") is True
    assert coord.state.last_own_write_at["switch.kessel"] >= before


# ════════════════════════════════════════════════════════════════════
# #140 — Manuelle Übersteuerung
# ════════════════════════════════════════════════════════════════════


async def test_auto_hold_foreign_drift_pauses_instead_of_rewriting(
    hass: HomeAssistant,
):
    """DER #140-Kern: AUTO-Drift OHNE eigenen Write in den letzten
    LOCAL_OVERRIDE_GRACE_S = Nutzer-Eingriff → kein Rewrite, Gerät
    pausiert, Hold beendet."""
    coord = make_coordinator(hass, [_heating_device(ENTITY_CONTROL_HOLD_AUTO)])
    coord.state.active_state["d1"] = True
    coord.state.last_sse_event_at = time.time()
    # Kein last_own_write_at-Eintrag → letzter eigener Write "nie".
    hass.states.async_set("switch.kessel", "off")  # User hat abgeschaltet
    calls = async_mock_service(hass, "switch", "turn_on")

    with patch(_SLEEP, _breaking_sleep(8)):
        await _run_until_stopped(
            coord._hold_loop("d1", "switch.kessel", "on", "switch", True,
                             ENTITY_CONTROL_HOLD_AUTO)
        )

    assert calls == []
    until = coord.state.local_override_until.get("d1", 0.0)
    assert until > time.time() + LOCAL_OVERRIDE_HOLD_S - 60


async def test_auto_hold_recent_own_write_still_repairs_drift(
    hass: HomeAssistant,
):
    """Drift kurz nach EIGENEM Write (Echo/Register-Revert im
    Grace-Fenster) bleibt Drift-Repair — kein False-Positive-Override."""
    coord = make_coordinator(hass, [_heating_device(ENTITY_CONTROL_HOLD_AUTO)])
    coord.state.active_state["d1"] = True
    coord.state.last_sse_event_at = time.time()
    coord.state.last_own_write_at["switch.kessel"] = time.time()
    hass.states.async_set("switch.kessel", "off")
    calls = async_mock_service(hass, "switch", "turn_on")

    with patch(_SLEEP, _breaking_sleep(2)):
        await _run_until_stopped(
            coord._hold_loop("d1", "switch.kessel", "on", "switch", True,
                             ENTITY_CONTROL_HOLD_AUTO)
        )

    assert len(calls) == 1
    assert "d1" not in coord.state.local_override_until


async def test_always_hold_never_flags_override(hass: HomeAssistant):
    """ALWAYS existiert für Geräte, deren HA-State die Realität nicht
    abbildet (Auto-Reset-Register) — dort ist Drift NIE ein
    Nutzer-Eingriff. Blind-Rewrite bleibt unverändert."""
    coord = make_coordinator(
        hass, [_heating_device(ENTITY_CONTROL_HOLD_ALWAYS)]
    )
    coord.state.active_state["d1"] = True
    coord.state.last_sse_event_at = time.time()
    hass.states.async_set("switch.kessel", "off")
    calls = async_mock_service(hass, "switch", "turn_on")

    with patch(_SLEEP, _breaking_sleep(2)):
        await _run_until_stopped(
            coord._hold_loop("d1", "switch.kessel", "on", "switch", True,
                             ENTITY_CONTROL_HOLD_ALWAYS)
        )

    assert len(calls) == 1
    assert "d1" not in coord.state.local_override_until


async def test_local_override_gates_apply_device_state(hass: HomeAssistant):
    """Solange die Pause gilt, schreibt kein Apply-Pfad — auch nicht der
    Self-Heal-Loop, der `_apply_device_state` alle 30 s ruft."""
    coord = make_coordinator(hass, [_heating_device()])
    coord.state.local_override_until["d1"] = time.time() + 600
    hass.states.async_set("switch.kessel", "off")
    calls = async_mock_service(hass, "switch", "turn_on")

    await coord._apply_device_state("d1", True)

    assert calls == []


async def test_local_override_expires_and_control_resumes(hass: HomeAssistant):
    coord = make_coordinator(hass, [_heating_device()])
    coord.state.local_override_until["d1"] = time.time() - 1  # abgelaufen
    hass.states.async_set("switch.kessel", "off")
    calls = async_mock_service(hass, "switch", "turn_on")

    await coord._apply_device_state("d1", True)

    assert len(calls) == 1


async def test_grace_constant_covers_hold_cadence_note():
    """Dokumentations-Pin: Grace == HOLD_POLL_INTERVAL heißt, ein
    AUTO-Gerät, das WIEDERHOLT gegen unseren Write zurückspringt, wird
    nach dem zweiten Zyklus als Übersteuerung behandelt —
    Dauer-Gegenschreiben ist genau das Verhalten, das #140 abschafft.
    Legitime Auto-Reset-Register gehören in den ALWAYS-Modus."""
    from custom_components.theothergas.const import HOLD_POLL_INTERVAL
    assert LOCAL_OVERRIDE_GRACE_S >= HOLD_POLL_INTERVAL
