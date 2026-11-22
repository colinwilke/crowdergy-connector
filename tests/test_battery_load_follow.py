"""Lokale Lastfolge des Akku-Entlade-Setpoints (backend#495)."""
from __future__ import annotations

import time

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.theothergas.battery_load_follow import (
    BATTERY_LOAD_FOLLOW_AVG_S,
    BATTERY_LOAD_FOLLOW_INTERVAL_S,
    BATTERY_LOAD_FOLLOW_REFRESH_TIMEOUT_S,
    BatteryLoadFollowParams,
    BatteryLoadFollowRun,
    battery_deficit_kw,
    decide_discharge,
)
from custom_components.theothergas.const import OPT_CONSENT_REMOTE_CONTROL
from tests.test_battery_pv_follow import _field, make_coordinator

LF = {"grid_device_id": "sub", "max_kw": 0.87, "min_kw": 0.5}
PARAMS = BatteryLoadFollowParams.from_frame(LF)
FRAME = {
    "type": "command", "device_id": "bat", "action": "set_charge_mode",
    "mode": "discharge", "setpoint_kw": -0.87, "load_follow": dict(LF),
}
STEP = BATTERY_LOAD_FOLLOW_INTERVAL_S


def _run(setpoint_kw: float = 0.85, params=PARAMS) -> BatteryLoadFollowRun:
    return BatteryLoadFollowRun(
        params=params, refreshed_at=0.0, setpoint_kw=setpoint_kw,
    )


def _feed(run, values, t0=1000.0):
    """Werte im Regler-Takt einspeisen; liefert die Entscheidungen."""
    return [
        decide_discharge(run, v, t0 + i * STEP) for i, v in enumerate(values)
    ]


# ── pure ─────────────────────────────────────────────────────────────────


def test_params_reject_malformed_frames():
    assert BatteryLoadFollowParams("sub", 0.87, 0.5, 0.5) == PARAMS
    assert BatteryLoadFollowParams.from_frame(None) is None
    assert BatteryLoadFollowParams.from_frame({"max_kw": 1.0}) is None
    assert BatteryLoadFollowParams.from_frame({**LF, "max_kw": 0}) is None
    assert BatteryLoadFollowParams.from_frame({**LF, "max_kw": "x"}) is None
    # Mindestleistung über dem Deckel bzw. Stopp-Schwelle über der
    # Mindestleistung: kein Regler.
    assert BatteryLoadFollowParams.from_frame({**LF, "min_kw": 1.0}) is None
    assert BatteryLoadFollowParams.from_frame(
        {**LF, "stop_below_kw": 0.6}
    ) is None
    assert BatteryLoadFollowParams.from_frame(
        {**LF, "stop_below_kw": 0.3}
    ).stop_below_kw == 0.3


def test_deficit_is_invariant_against_own_discharge():
    # Feld 17.11. 14:15: Sub bezieht 1,81 kW, Akku still.
    before = battery_deficit_kw(1.81, 0.01)
    # Akku entlädt 0,86 kW → der Bezug fällt um genau das.
    after = battery_deficit_kw(0.96, 0.86)
    assert before == pytest.approx(1.82) and after == pytest.approx(1.82)
    assert battery_deficit_kw(None, 0.0) is None
    assert battery_deficit_kw(0.0, None) is None


def test_field_case_load_gone_stops_the_discharge():
    """Feld 17.11. ab 14:18: Sub −1,0 kW, Akku 0,86 kW ⇒ Defizit −0,14 kW.
    Erst das gefüllte Fenster entscheidet, dann Setpoint 0."""
    run = _run()
    out = _feed(run, [-0.14] * 5)
    assert out == [None, None, None, None, 0.0]


def test_single_sample_never_moves_the_setpoint():
    """Ein Ausreißer im sonst gedeckten Fenster ändert nichts."""
    run = _run()
    out = _feed(run, [1.5, 1.5, 1.5, 1.5, 1.5, -2.0, 1.5])
    assert out == [None] * 7


def test_follows_a_lasting_lower_load_down():
    run = _run()
    out = _feed(run, [0.7] * 5)
    assert out[-1] == 0.7


def test_never_above_the_commanded_setpoint():
    run = _run(setpoint_kw=0.6)
    out = _feed(run, [3.0] * 5)
    assert out[-1] == 0.85  # max_kw 0,87 auf dem 0,05-Raster


def test_between_stop_threshold_and_minimum_holds_the_minimum():
    params = BatteryLoadFollowParams.from_frame({**LF, "stop_below_kw": 0.3})
    run = _run(setpoint_kw=0.85, params=params)
    assert _feed(run, [0.4] * 5)[-1] == 0.5
    run.setpoint_kw = 0.5
    assert _feed(run, [0.2] * 5, t0=2000.0)[-1] == 0.0


def test_restart_needs_buffer_over_the_window():
    run = _run(setpoint_kw=0.0)
    assert _feed(run, [0.55] * 6) == [None] * 6  # unter Stopp + Puffer
    assert _feed(run, [0.8] * 5, t0=2000.0)[-1] == 0.8


def test_restart_between_stop_threshold_and_minimum_power():
    """Feld 22.11.: Stopp-Schwelle 0,23 kW, Hauslast 0,45 kW, Mindest-
    leistung 0,5 kW. Nach einem Stopp muss der Regler wieder anlaufen —
    mit der Mindestleistung."""
    params = BatteryLoadFollowParams.from_frame({**LF, "stop_below_kw": 0.23})
    run = _run(setpoint_kw=0.0, params=params)
    assert _feed(run, [0.45] * 5)[-1] == 0.5
    run = _run(setpoint_kw=0.0, params=params)
    assert _feed(run, [0.30] * 6) == [None] * 6  # in der Hysterese


def test_missing_measurement_stops_at_once_and_restarts_the_window():
    run = _run()
    assert decide_discharge(run, None, 1000.0) == 0.0
    run.setpoint_kw = 0.0
    assert decide_discharge(run, None, 1015.0) is None
    # Danach braucht es wieder ein volles Fenster.
    out = _feed(run, [0.8] * 5, t0=1030.0)
    assert out[:4] == [None] * 4 and out[4] == 0.8
    assert BATTERY_LOAD_FOLLOW_AVG_S == 4 * STEP


# ── Coordinator-Verdrahtung ──────────────────────────────────────────────


async def _start(coord) -> BatteryLoadFollowRun:
    await coord._handle_ws_message(dict(FRAME))
    run = coord._battery_load_follow_runs()["bat"]
    coord._battery_load_follow_tasks()["bat"].cancel()
    return run


async def test_frame_with_load_follow_starts_and_follows_the_load(
    hass: HomeAssistant,
):
    coord = make_coordinator(hass)
    _field(hass, sub=1.81, bat=0.0)
    setpoints = async_mock_service(hass, "number", "set_value")
    modes = async_mock_service(hass, "select", "select_option")

    run = await _start(coord)
    assert [c.data["value"] for c in setpoints] == [-850.0]
    assert [c.data["option"] for c in modes] == ["Aktiv"]
    assert run.setpoint_kw == 0.85

    # Last weg: Sub speist 1,0 kW ein, Akku entlädt 0,86 kW.
    _field(hass, sub=-1.0, bat=0.86, setpoint_w=-850.0)
    hass.states.async_set("select.bat_mode", "Aktiv")
    t0 = time.time()
    for i in range(5):
        await coord._battery_load_follow_tick("bat", run, t0 + i * STEP)
    assert setpoints[-1].data["value"] == 0.0
    assert run.setpoint_kw == 0.0
    assert all(c.data["option"] == "Aktiv" for c in modes)


async def test_renewal_keeps_the_setpoint_and_lowers_to_a_new_cap(
    hass: HomeAssistant,
):
    coord = make_coordinator(hass)
    _field(hass, sub=1.81, bat=0.0)
    setpoints = async_mock_service(hass, "number", "set_value")
    async_mock_service(hass, "select", "select_option")
    run = await _start(coord)
    run.refreshed_at = 0.0
    n = len(setpoints)

    await coord._handle_ws_message(dict(FRAME))
    assert len(setpoints) == n  # Erneuerung schreibt nichts
    assert run.setpoint_kw == 0.85 and run.refreshed_at > 0.0

    lower = {**FRAME, "setpoint_kw": -0.6,
             "load_follow": {**LF, "max_kw": 0.6}}
    await coord._handle_ws_message(lower)
    assert setpoints[-1].data["value"] == -600.0
    assert run.setpoint_kw == 0.6


async def test_command_without_load_follow_ends_the_regulator(
    hass: HomeAssistant,
):
    coord = make_coordinator(hass)
    _field(hass, sub=1.81, bat=0.0)
    async_mock_service(hass, "number", "set_value")
    modes = async_mock_service(hass, "select", "select_option")
    await _start(coord)
    hass.states.async_set("select.bat_mode", "Aktiv")

    await coord._handle_ws_message({
        "type": "command", "device_id": "bat", "action": "set_charge_mode",
        "mode": "passive", "setpoint_kw": 0.0,
    })

    assert "bat" not in coord._battery_load_follow_runs()
    assert modes[-1].data["option"] == "Passiv"


async def test_load_follow_only_on_discharge(hass: HomeAssistant):
    coord = make_coordinator(hass)
    _field(hass, sub=1.81, bat=0.0)
    async_mock_service(hass, "number", "set_value")
    async_mock_service(hass, "select", "select_option")
    await coord._handle_ws_message(
        {**FRAME, "mode": "charge", "setpoint_kw": 1.0}
    )
    assert coord._battery_load_follow_runs() == {}


async def test_refresh_timeout_falls_back_to_passive(hass: HomeAssistant):
    coord = make_coordinator(hass)
    _field(hass, sub=1.81, bat=0.0)
    async_mock_service(hass, "number", "set_value")
    modes = async_mock_service(hass, "select", "select_option")
    run = await _start(coord)
    hass.states.async_set("select.bat_mode", "Aktiv")
    run.refreshed_at = time.time() - BATTERY_LOAD_FOLLOW_REFRESH_TIMEOUT_S - 1

    import custom_components.theothergas.battery_load_follow as mod

    real_sleep = mod.asyncio.sleep

    async def no_sleep(_s):
        await real_sleep(0)

    mod.asyncio.sleep = no_sleep
    try:
        await coord._battery_load_follow_loop("bat")
    finally:
        mod.asyncio.sleep = real_sleep

    assert "bat" not in coord._battery_load_follow_runs()
    assert modes[-1].data["option"] == "Passiv"


async def test_no_consent_no_write_and_no_regulator(hass: HomeAssistant):
    coord = make_coordinator(hass, options={OPT_CONSENT_REMOTE_CONTROL: False})
    _field(hass, sub=1.81, bat=0.0)
    setpoints = async_mock_service(hass, "number", "set_value")
    await coord._handle_ws_message(dict(FRAME))
    assert not setpoints
    assert coord._battery_load_follow_runs() == {}


async def test_old_style_discharge_still_holds_the_setpoint(hass: HomeAssistant):
    coord = make_coordinator(hass)
    _field(hass, sub=1.81, bat=0.0)
    setpoints = async_mock_service(hass, "number", "set_value")
    async_mock_service(hass, "select", "select_option")
    frame = {k: v for k, v in FRAME.items() if k != "load_follow"}
    await coord._handle_ws_message(frame)
    assert coord._battery_load_follow_runs() == {}
    assert [c.data["value"] for c in setpoints] == [-870.0]
