"""Vorlauf-Sollwert und An/Aus-Paar auf derselben climate-Entity.

Modulierende Heizung im Temperatur-Modus: value_on/value_off = Max/Min
(30/10 °C), der Solver schickt zusätzlich je Tick `vorlauf_setpoint_c`.
Ohne eigenen `entity_vorlauf_setpoint` geht der Vorlauf auf dieselbe
climate-Entity. Vorher stritten die Kanäle: der AUTO-Hold erwartete
Min/Max, sah den eigenen Vorlauf-Write (z. B. 25,6 °C) als Fremd-Drift
und pausierte das Gerät 2 h („Manuelle Bedienung erkannt", Feld colin
2026-10-24). Diese Datei pinnt:

* Steht der Vorlauf, ist er der kommandierte Wert: kein Override, kein
  Rückschreiben auf Min/Max.
* Drift-Repair und Fremd-Drift-Erkennung laufen gegen den Vorlauf.
* is_on-Wechsel mit Vorlauf im selben Frame: ein Write (der Vorlauf).
* is_on-Wechsel ohne Vorlauf: Min/Max wie bisher, alter Vorlauf verfällt.
* Eigene Vorlauf-Entity: An/Aus-Hold unverändert.
* Wirkungs-Kontrolle vergleicht gegen den Vorlauf.
* Neuer Vorlauf, Entity zeigt noch unseren alten Wert (ECO-Spiegel,
  Steuer-Entity): ausstehender Write, kein Override.
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
    CONF_ENTITY_CONTROL_ECO,
    CONF_ENTITY_CONTROL_HOLD,
    CONF_ENTITY_VORLAUF_SETPOINT,
    CONF_VALUE_OFF,
    CONF_VALUE_ON,
    DOMAIN,
    ENTITY_CONTROL_HOLD_AUTO,
)
from custom_components.theothergas.coordinator import CrowdergyCoordinator
from custom_components.theothergas.state_mirror import DeviceStateMirror

_SLEEP = "custom_components.theothergas.coordinator.asyncio.sleep"
WP = "climate.wp_heizkreis"
VL_NUMBER = "number.wp_vorlauf_soll"


def make_coordinator(hass: HomeAssistant, devices: list[dict]) -> CrowdergyCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
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
    coord.state.active_state["d1"] = True
    coord.state.last_sse_event_at = time.time()
    return coord


def _heating(*, vorlauf_entity: str = "") -> dict:
    dev = {
        CONF_DEVICE_ID: "d1",
        CONF_DEVICE_TYPE: "heating",
        CONF_ENTITY_CONTROL: WP,
        CONF_VALUE_ON: "30",
        CONF_VALUE_OFF: "10",
        CONF_ENTITY_CONTROL_HOLD: ENTITY_CONTROL_HOLD_AUTO,
    }
    if vorlauf_entity:
        dev[CONF_ENTITY_VORLAUF_SETPOINT] = vorlauf_entity
    return dev


def _wp(hass, temperature: float) -> None:
    hass.states.async_set(
        WP, "heat", {"temperature": temperature, "min_temp": 10, "max_temp": 30},
    )


class _StopHold(BaseException):
    pass


def _breaking_sleep(max_calls: int):
    state = {"n": 0}

    async def _sleep(_delay):
        state["n"] += 1
        if state["n"] >= max_calls:
            raise _StopHold

    return _sleep


async def _run_hold(coord, on: bool, raw: str, ticks: int = 4) -> None:
    with patch(_SLEEP, _breaking_sleep(ticks)):
        try:
            await coord._hold_loop("d1", WP, raw, "climate", on,
                                   ENTITY_CONTROL_HOLD_AUTO)
        except _StopHold:
            pass


async def test_own_vorlauf_write_is_not_a_manual_override(hass: HomeAssistant):
    """Feld-Repro: AUS (10 °C), Solver-Vorlauf 25,6 °C steht in der Entity,
    eigener Write > 30 s her → weder Pause noch Rückschreiben auf 10."""
    coord = make_coordinator(hass, [_heating()])
    coord.state.on_state["d1"] = False
    _wp(hass, 10.0)
    calls = async_mock_service(hass, "climate", "set_temperature")

    await coord._apply_vorlauf_setpoint("d1", 25.6)
    assert [c.data for c in calls] == [{"entity_id": WP, "temperature": 25.6}]
    _wp(hass, 25.6)
    coord.state.last_own_write_at[WP] = time.time() - 120

    await _run_hold(coord, False, "10")

    assert "d1" not in coord.state.local_override_until
    assert len(calls) == 1  # kein Rückschreiben auf 10 °C


async def test_hold_repairs_drift_back_to_vorlauf(hass: HomeAssistant):
    """Echo nach eigenem Write (Entity springt auf 30) → Repair auf den
    Vorlauf, nicht auf Max."""
    coord = make_coordinator(hass, [_heating()])
    coord.state.on_state["d1"] = True
    _wp(hass, 27.7)
    calls = async_mock_service(hass, "climate", "set_temperature")
    await coord._apply_vorlauf_setpoint("d1", 27.7)  # steht schon → kein Write
    assert calls == []
    _wp(hass, 30.0)
    coord.state.last_own_write_at[WP] = time.time()

    await _run_hold(coord, True, "30", ticks=2)

    assert [c.data for c in calls] == [{"entity_id": WP, "temperature": 27.7}]
    assert "d1" not in coord.state.local_override_until


async def test_foreign_drift_from_vorlauf_still_pauses(hass: HomeAssistant):
    """Ein echter Eingriff (22 °C, kein eigener Write) bleibt erkannt."""
    coord = make_coordinator(hass, [_heating()])
    coord.state.on_state["d1"] = True
    _wp(hass, 25.6)
    calls = async_mock_service(hass, "climate", "set_temperature")
    await coord._apply_vorlauf_setpoint("d1", 25.6)
    _wp(hass, 22.0)
    coord.state.last_own_write_at[WP] = time.time() - 120

    await _run_hold(coord, True, "30")

    assert calls == []
    assert coord.state.local_override_until.get("d1", 0) > time.time()


async def test_is_on_flip_with_vorlauf_writes_once(hass: HomeAssistant):
    """is_on + Vorlauf im selben Frame → genau ein Write mit dem Vorlauf
    (vorher: erst 30 °C, dann 25,6 °C)."""
    coord = make_coordinator(hass, [_heating()])
    coord.state.on_state["d1"] = False
    _wp(hass, 10.0)
    calls = async_mock_service(hass, "climate", "set_temperature")

    with patch.object(coord, "_start_hold"):
        await coord._handle_ws_message({
            "type": "telemetry", "device_id": "d1",
            "data": {"is_on": True, "vorlauf_setpoint_c": 25.6},
        })
        _wp(hass, 25.6)
        # Folge-Tick mit gleichem Vorlauf: steht schon, kein Write.
        await coord._handle_ws_message({
            "type": "telemetry", "device_id": "d1",
            "data": {"vorlauf_setpoint_c": 25.6},
        })

    assert [c.data for c in calls] == [{"entity_id": WP, "temperature": 25.6}]
    assert coord.state.last_written_value["d1"] == "25.6"


async def test_is_on_flip_without_vorlauf_uses_min_max(hass: HomeAssistant):
    """Hand-Befehl / Resync ohne Vorlauf: value_on wie bisher, der alte
    Vorlauf (aus dem AUS-Zustand) gilt nicht für AN."""
    coord = make_coordinator(hass, [_heating()])
    coord.state.on_state["d1"] = False
    _wp(hass, 10.0)
    calls = async_mock_service(hass, "climate", "set_temperature")
    await coord._apply_vorlauf_setpoint("d1", 25.0)
    _wp(hass, 25.0)

    with patch.object(coord, "_start_hold"):
        await coord._handle_ws_message({
            "type": "telemetry", "device_id": "d1", "data": {"is_on": True},
        })

    assert [c.data["temperature"] for c in calls] == [25.0, 30.0]
    assert "d1" not in coord.state.vorlauf_on_control


async def test_separate_vorlauf_entity_leaves_hold_unchanged(hass: HomeAssistant):
    """Eigener Vorlauf-Slot (number): kein Konflikt, der Hold hält Max."""
    coord = make_coordinator(hass, [_heating(vorlauf_entity=VL_NUMBER)])
    coord.state.on_state["d1"] = True
    hass.states.async_set(VL_NUMBER, "20", {"min": 20, "max": 55})
    _wp(hass, 30.0)
    number_calls = async_mock_service(hass, "number", "set_value")
    climate_calls = async_mock_service(hass, "climate", "set_temperature")

    await coord._apply_vorlauf_setpoint("d1", 35.0)
    assert [c.data for c in number_calls] == [{"entity_id": VL_NUMBER, "value": 35.0}]
    assert "d1" not in coord.state.vorlauf_on_control
    coord.state.last_own_write_at[WP] = time.time() - 120

    await _run_hold(coord, True, "30")

    assert climate_calls == []
    assert "d1" not in coord.state.local_override_until


ECO = "number.wp_eco_soll"


def _eco(hass, value: float) -> None:
    hass.states.async_set(ECO, str(value), {"min": 10, "max": 30})


async def test_new_vorlauf_on_eco_mirror_is_not_an_override(hass: HomeAssistant):
    """Feld-Repro nach v3.50.5: der ECO-Spiegel hält unseren alten
    Vorlauf (25,6 °C), der Solver schickt 27 °C. Die ECO-Entity zeigt
    noch unseren eigenen Wert → nachschreiben, nicht pausieren."""
    dev = _heating()
    dev[CONF_ENTITY_CONTROL_ECO] = ECO
    coord = make_coordinator(hass, [dev])
    coord.state.on_state["d1"] = True
    _wp(hass, 25.6)
    _eco(hass, 25.6)
    async_mock_service(hass, "climate", "set_temperature")
    eco_calls = async_mock_service(hass, "number", "set_value")
    await coord._apply_vorlauf_setpoint("d1", 25.6)
    await _run_hold(coord, True, "30", ticks=2)  # ECO steht → gemerkt
    assert eco_calls == []

    coord.state.last_own_write_at[ECO] = time.time() - 600
    await coord._apply_vorlauf_setpoint("d1", 27.0)
    _wp(hass, 27.0)
    await _run_hold(coord, True, "30", ticks=2)

    assert "d1" not in coord.state.local_override_until
    assert [c.data for c in eco_calls] == [{"entity_id": ECO, "value": 27.0}]


async def test_foreign_change_on_eco_mirror_still_pauses(hass: HomeAssistant):
    """Gegenprobe: zeigt die ECO-Entity einen Wert, den wir nie
    geschrieben haben, bleibt das ein Eingriff."""
    dev = _heating()
    dev[CONF_ENTITY_CONTROL_ECO] = ECO
    coord = make_coordinator(hass, [dev])
    coord.state.on_state["d1"] = True
    _wp(hass, 25.6)
    _eco(hass, 25.6)
    async_mock_service(hass, "climate", "set_temperature")
    eco_calls = async_mock_service(hass, "number", "set_value")
    await coord._apply_vorlauf_setpoint("d1", 25.6)
    await _run_hold(coord, True, "30", ticks=2)

    _eco(hass, 21.0)
    coord.state.last_own_write_at[ECO] = time.time() - 600
    await _run_hold(coord, True, "30", ticks=2)

    assert eco_calls == []
    assert coord.state.local_override_until.get("d1", 0) > time.time()


async def test_new_vorlauf_skipped_by_breaker_is_not_an_override(
    hass: HomeAssistant,
):
    """Steuer-Entity: neuer Vorlauf kam an, der Write ging (noch) nicht
    raus — die Entity zeigt unseren alten Vorlauf. Kein Override."""
    coord = make_coordinator(hass, [_heating()])
    coord.state.on_state["d1"] = True
    _wp(hass, 25.6)
    calls = async_mock_service(hass, "climate", "set_temperature")
    await coord._apply_vorlauf_setpoint("d1", 25.6)  # steht → gemerkt
    coord.state.last_own_write_at[WP] = time.time() - 600
    with patch.object(
        CrowdergyCoordinator, "_write_allowed", lambda *_a, **_k: False,
    ):
        await coord._apply_vorlauf_setpoint("d1", 27.0)
    await _run_hold(coord, True, "30", ticks=2)

    assert "d1" not in coord.state.local_override_until
    assert [c.data for c in calls] == [{"entity_id": WP, "temperature": 27.0}]
