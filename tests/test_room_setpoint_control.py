"""Raum-Sollwert der modulierenden Heizung auf der Steuer-Entity (#341).

Die WP regelt ihren Vorlauf selbst nach der Heizkurve; Crowdergy schreibt
nur den Raum-Sollwert des Heizkreises (Stiebel ISG: climate-Entity,
5–30 °C). AN = der vom Backend gelieferte `room_setpoint_c` (Fallback
value_on), AUS = value_off. Diese Datei pinnt:

* Steht der Sollwert, ist er der kommandierte AN-Wert: kein Override,
  kein Rückschreiben auf value_on; Drift-Repair gegen ihn.
* is_on + Sollwert im selben Frame: ein Write. is_on ohne Sollwert:
  value_on, alter Sollwert verfällt. Sollwert bei AUS: ignoriert.
* Steuer-Entity ohne Temperatur-Modus (Schalter): kein Sollwert.
* Neuer Sollwert, Entity zeigt noch unseren alten Wert (ECO-Spiegel,
  verschluckter Write): ausstehender Write, kein Override.
* Träge Entity nach HA-Neustart: zeigt noch den Wert von vor unserem
  Write → kein Override; ein neuer Fremdwert pausiert weiter.
* Tagesbudget: höchstens 24 Writes je Sollwert-Register und Tag.
* Ein Vorlauf-Feld vom Backend wird nie geschrieben.
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
    CONF_VALUE_OFF,
    CONF_VALUE_ON,
    DOMAIN,
    ENTITY_CONTROL_HOLD_AUTO,
    SETPOINT_WRITES_MAX_PER_DAY,
)
from custom_components.theothergas.coordinator import CrowdergyCoordinator
from custom_components.theothergas.state_mirror import DeviceStateMirror

_SLEEP = "custom_components.theothergas.coordinator.asyncio.sleep"
WP = "climate.wp_heizkreis"


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


def _heating() -> dict:
    dev = {
        CONF_DEVICE_ID: "d1",
        CONF_DEVICE_TYPE: "heating",
        CONF_ENTITY_CONTROL: WP,
        CONF_VALUE_ON: "30",
        CONF_VALUE_OFF: "10",
        CONF_ENTITY_CONTROL_HOLD: ENTITY_CONTROL_HOLD_AUTO,
    }
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


async def _apply_sp(coord, value: float) -> None:
    """Raum-Sollwert anwenden, ohne einen echten Hold-Task zu starten."""
    with patch.object(coord, "_start_hold"):
        await coord._apply_room_setpoint("d1", value)


async def test_own_setpoint_write_is_not_a_manual_override(hass: HomeAssistant):
    """AN, Solver-Sollwert 22,5 °C steht in der Entity, eigener Write
    > 30 s her → weder Pause noch Rückschreiben auf value_on (30)."""
    coord = make_coordinator(hass, [_heating()])
    coord.state.on_state["d1"] = True
    _wp(hass, 21.0)
    calls = async_mock_service(hass, "climate", "set_temperature")

    await _apply_sp(coord, 22.5)
    assert [c.data for c in calls] == [{"entity_id": WP, "temperature": 22.5}]
    _wp(hass, 22.5)
    coord.state.last_own_write_at[WP] = time.time() - 120

    await _run_hold(coord, True, "30")

    assert "d1" not in coord.state.local_override_until
    assert len(calls) == 1
    assert coord.state.last_written_value["d1"] == "22.5"


async def test_hold_repairs_drift_back_to_setpoint(hass: HomeAssistant):
    """Echo nach eigenem Write (Entity springt auf 30) → Repair auf den
    Sollwert, nicht auf value_on."""
    coord = make_coordinator(hass, [_heating()])
    coord.state.on_state["d1"] = True
    _wp(hass, 23.0)
    calls = async_mock_service(hass, "climate", "set_temperature")
    await _apply_sp(coord, 23.0)  # steht schon → kein Write
    assert calls == []
    _wp(hass, 30.0)
    coord.state.last_own_write_at[WP] = time.time()

    await _run_hold(coord, True, "30", ticks=2)

    assert [c.data for c in calls] == [{"entity_id": WP, "temperature": 23.0}]
    assert "d1" not in coord.state.local_override_until


async def test_foreign_drift_from_setpoint_still_pauses(hass: HomeAssistant):
    """Ein echter Eingriff (19 °C, kein eigener Write) bleibt erkannt."""
    coord = make_coordinator(hass, [_heating()])
    coord.state.on_state["d1"] = True
    _wp(hass, 22.5)
    calls = async_mock_service(hass, "climate", "set_temperature")
    await _apply_sp(coord, 22.5)
    _wp(hass, 19.0)
    coord.state.last_own_write_at[WP] = time.time() - 120

    await _run_hold(coord, True, "30")

    assert calls == []
    assert coord.state.local_override_until.get("d1", 0) > time.time()


async def test_is_on_flip_with_setpoint_writes_once(hass: HomeAssistant):
    """is_on + Sollwert im selben Frame → genau ein Write mit dem Sollwert
    (nicht erst value_on, dann der Sollwert)."""
    coord = make_coordinator(hass, [_heating()])
    coord.state.on_state["d1"] = False
    _wp(hass, 10.0)
    calls = async_mock_service(hass, "climate", "set_temperature")

    with patch.object(coord, "_start_hold"):
        await coord._handle_ws_message({
            "type": "telemetry", "device_id": "d1",
            "data": {"is_on": True, "room_setpoint_c": 23.5},
        })
        _wp(hass, 23.5)
        # Folge-Tick mit gleichem Sollwert: steht schon, kein Write.
        await coord._handle_ws_message({
            "type": "telemetry", "device_id": "d1",
            "data": {"room_setpoint_c": 23.5},
        })

    assert [c.data for c in calls] == [{"entity_id": WP, "temperature": 23.5}]
    assert coord.state.last_written_value["d1"] == "23.5"


async def test_new_setpoint_mid_run_is_written(hass: HomeAssistant):
    """Solver hebt den Sollwert im laufenden Heizlauf (günstig jetzt) →
    ein Write mit dem neuen Wert."""
    coord = make_coordinator(hass, [_heating()])
    coord.state.on_state["d1"] = True
    _wp(hass, 22.0)
    calls = async_mock_service(hass, "climate", "set_temperature")
    with patch.object(coord, "_start_hold"):
        await coord._handle_ws_message({
            "type": "telemetry", "device_id": "d1",
            "data": {"room_setpoint_c": 25.5},
        })
    assert [c.data["temperature"] for c in calls] == [25.5]


async def test_is_on_flip_without_setpoint_uses_value_on(hass: HomeAssistant):
    """Hand-Befehl / altes Backend ohne Sollwert: value_on, ein alter
    Sollwert aus einem früheren Lauf gilt nicht."""
    coord = make_coordinator(hass, [_heating()])
    coord.state.on_state["d1"] = True
    _wp(hass, 21.0)
    calls = async_mock_service(hass, "climate", "set_temperature")
    await _apply_sp(coord, 22.0)
    _wp(hass, 22.0)
    with patch.object(coord, "_start_hold"):
        await coord._handle_ws_message({
            "type": "telemetry", "device_id": "d1", "data": {"is_on": False},
        })
        _wp(hass, 10.0)
        await coord._handle_ws_message({
            "type": "telemetry", "device_id": "d1", "data": {"is_on": True},
        })

    assert [c.data["temperature"] for c in calls] == [22.0, 10.0, 30.0]
    assert "d1" not in coord.state.room_setpoint_control


async def test_setpoint_while_off_is_ignored(hass: HomeAssistant):
    """AUS ist immer value_off — ein Sollwert ohne Heizlauf schreibt nichts."""
    coord = make_coordinator(hass, [_heating()])
    coord.state.on_state["d1"] = False
    _wp(hass, 10.0)
    calls = async_mock_service(hass, "climate", "set_temperature")
    await _apply_sp(coord, 23.0)
    assert calls == []
    assert "d1" not in coord.state.room_setpoint_control


async def test_switch_control_takes_no_setpoint(hass: HomeAssistant):
    """Steuer-Entity ohne Temperatur-Modus (Schalter): kein Sollwert."""
    dev = _heating()
    dev[CONF_ENTITY_CONTROL] = "switch.wp_boost"
    dev[CONF_VALUE_ON] = ""
    dev[CONF_VALUE_OFF] = ""
    coord = make_coordinator(hass, [dev])
    coord.state.on_state["d1"] = True
    hass.states.async_set("switch.wp_boost", "on")
    switch_calls = async_mock_service(hass, "switch", "turn_on")
    climate_calls = async_mock_service(hass, "climate", "set_temperature")
    await _apply_sp(coord, 23.0)
    assert switch_calls == [] and climate_calls == []
    assert "d1" not in coord.state.room_setpoint_control


async def test_legacy_vorlauf_frame_never_writes(hass: HomeAssistant):
    """Ein altes Backend schickt `vorlauf_setpoint_c` — kein Write."""
    coord = make_coordinator(hass, [_heating()])
    coord.state.on_state["d1"] = True
    _wp(hass, 22.0)
    calls = async_mock_service(hass, "climate", "set_temperature")
    with patch.object(coord, "_start_hold"):
        await coord._handle_ws_message({
            "type": "telemetry", "device_id": "d1",
            "data": {"vorlauf_setpoint_c": 31.6},
        })
    assert calls == []


ECO = "number.wp_eco_soll"


def _eco(hass, value: float) -> None:
    hass.states.async_set(ECO, str(value), {"min": 5, "max": 30})


async def test_new_setpoint_on_eco_mirror_is_not_an_override(hass: HomeAssistant):
    """Der ECO-Spiegel hält unseren alten Sollwert (22 °C), der Solver
    schickt 24 °C. Die ECO-Entity zeigt noch unseren eigenen Wert →
    nachschreiben, nicht pausieren."""
    dev = _heating()
    dev[CONF_ENTITY_CONTROL_ECO] = ECO
    coord = make_coordinator(hass, [dev])
    coord.state.on_state["d1"] = True
    _wp(hass, 22.0)
    _eco(hass, 22.0)
    async_mock_service(hass, "climate", "set_temperature")
    eco_calls = async_mock_service(hass, "number", "set_value")
    await _apply_sp(coord, 22.0)
    await _run_hold(coord, True, "30", ticks=2)  # ECO steht → gemerkt
    assert eco_calls == []

    coord.state.last_own_write_at[ECO] = time.time() - 600
    coord.state.room_setpoint_control["d1"] = (True, WP, 24.0)
    _wp(hass, 24.0)
    await _run_hold(coord, True, "30", ticks=2)

    assert "d1" not in coord.state.local_override_until
    assert [c.data for c in eco_calls] == [{"entity_id": ECO, "value": 24.0}]


async def test_foreign_change_on_eco_mirror_still_pauses(hass: HomeAssistant):
    """Gegenprobe: zeigt die ECO-Entity einen Wert, den wir nie
    geschrieben haben, bleibt das ein Eingriff."""
    dev = _heating()
    dev[CONF_ENTITY_CONTROL_ECO] = ECO
    coord = make_coordinator(hass, [dev])
    coord.state.on_state["d1"] = True
    _wp(hass, 22.0)
    _eco(hass, 22.0)
    async_mock_service(hass, "climate", "set_temperature")
    eco_calls = async_mock_service(hass, "number", "set_value")
    await _apply_sp(coord, 22.0)
    await _run_hold(coord, True, "30", ticks=2)

    _eco(hass, 17.0)
    coord.state.last_own_write_at[ECO] = time.time() - 600
    await _run_hold(coord, True, "30", ticks=2)

    assert eco_calls == []
    assert coord.state.local_override_until.get("d1", 0) > time.time()


async def test_new_setpoint_skipped_by_breaker_is_not_an_override(
    hass: HomeAssistant,
):
    """Neuer Sollwert kam an, der Write ging (noch) nicht raus — die
    Entity zeigt unseren alten Sollwert. Kein Override."""
    coord = make_coordinator(hass, [_heating()])
    coord.state.on_state["d1"] = True
    _wp(hass, 22.0)
    calls = async_mock_service(hass, "climate", "set_temperature")
    await _apply_sp(coord, 22.0)  # steht → gemerkt
    coord.state.last_own_write_at[WP] = time.time() - 600
    with patch.object(
        CrowdergyCoordinator, "_write_allowed", lambda *_a, **_k: False,
    ):
        await _apply_sp(coord, 24.0)
    await _run_hold(coord, True, "30", ticks=2)

    assert "d1" not in coord.state.local_override_until
    assert [c.data for c in calls] == [{"entity_id": WP, "temperature": 24.0}]


async def test_slow_entity_after_restart_is_not_an_override(hass: HomeAssistant):
    """Feld-Repro 17:45 nach dem Update: HA-Neustart, die Entity hält noch
    unseren alten Sollwert 25,7 °C, der Connector kennt ihn nicht mehr und
    schreibt AUS = 10 °C. Die Integration übernimmt den Write verzögert —
    die Entity zeigt > 30 s weiter 25,7. Das ist kein Nutzer-Eingriff."""
    coord = make_coordinator(hass, [_heating()])
    coord.state.on_state["d1"] = False
    _wp(hass, 25.7)
    calls = async_mock_service(hass, "climate", "set_temperature")

    await coord._apply_device_state("d1", False)  # schreibt 10, Entity träge
    assert [c.data for c in calls] == [{"entity_id": WP, "temperature": 10.0}]
    coord._cancel_hold("d1")
    coord.state.last_own_write_at[WP] = time.time() - 60

    await _run_hold(coord, False, "10", ticks=2)

    assert "d1" not in coord.state.local_override_until
    assert len(calls) == 2  # nachgeschrieben statt pausiert


async def test_slow_entity_then_foreign_value_still_pauses(hass: HomeAssistant):
    """Gegenprobe: ändert jemand den Wert auf etwas, das weder unser
    Kommando noch der Stand vor dem Write ist, bleibt es ein Eingriff."""
    coord = make_coordinator(hass, [_heating()])
    coord.state.on_state["d1"] = False
    _wp(hass, 25.7)
    calls = async_mock_service(hass, "climate", "set_temperature")
    await coord._apply_device_state("d1", False)
    coord._cancel_hold("d1")
    _wp(hass, 21.0)
    coord.state.last_own_write_at[WP] = time.time() - 60

    await _run_hold(coord, False, "10", ticks=2)

    assert len(calls) == 1
    assert coord.state.local_override_until.get("d1", 0) > time.time()


async def test_daily_setpoint_budget_blocks_after_24_writes(hass: HomeAssistant):
    """Sollwert-Register der WP: höchstens SETPOINT_WRITES_MAX_PER_DAY
    Writes je Entity und Tag; danach bleibt der Wert stehen und das
    Gerät meldet write_breaker. Am nächsten Tag geht es weiter."""
    coord = make_coordinator(hass, [_heating()])
    for _ in range(SETPOINT_WRITES_MAX_PER_DAY):
        assert coord._write_allowed("d1", WP) is True
    assert coord._write_allowed("d1", WP) is False
    assert "d1" in coord.state.write_breaker_devices
    day, count = coord.state.entity_day_write_counts[WP]
    assert count == SETPOINT_WRITES_MAX_PER_DAY
    coord.state.entity_day_write_counts[WP] = ("2000-01-01", count)
    assert coord._write_allowed("d1", WP) is True
    assert "d1" not in coord.state.write_breaker_devices


async def test_daily_budget_ignores_non_setpoint_entities(hass: HomeAssistant):
    """Schalter, Wallbox, Akku zählen nicht gegen das Tagesbudget."""
    dev = _heating()
    dev[CONF_ENTITY_CONTROL] = "switch.wp_boost"
    dev[CONF_VALUE_ON] = ""
    dev[CONF_VALUE_OFF] = ""
    coord = make_coordinator(hass, [dev])
    for _ in range(SETPOINT_WRITES_MAX_PER_DAY + 5):
        assert coord._write_allowed("d1", "switch.wp_boost") is True
    assert "switch.wp_boost" not in coord.state.entity_day_write_counts
