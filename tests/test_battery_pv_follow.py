"""Lokale PV-Nachregelung des Akku-Lade-Setpoints (backend#401)."""
from __future__ import annotations

import time

import pytest

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.theothergas.battery_pv_follow import (
    BATTERY_PV_FOLLOW_REFRESH_TIMEOUT_S,
    BATTERY_PV_FOLLOW_START_DELAY_S,
    BATTERY_PV_FOLLOW_STOP_DELAY_S,
    BatteryPvFollowParams,
    BatteryPvFollowRun,
    battery_surplus_kw,
    decide_setpoint,
)
from custom_components.theothergas.const import (
    CONF_DEVICE_ID,
    CONF_DEVICE_TYPE,
    CONF_ENTITY_BATTERY_MODE,
    CONF_ENTITY_BATTERY_POWER_SETPOINT,
    CONF_ENTITY_POWER,
    CONF_VALUE_BATTERY_MODE_ACTIVE,
    CONF_VALUE_BATTERY_MODE_PASSIVE,
    DOMAIN,
    OPT_CONSENT_REMOTE_CONTROL,
)
from custom_components.theothergas.coordinator import CrowdergyCoordinator
from custom_components.theothergas.state_mirror import DeviceStateMirror

PVF = {"grid_device_id": "sub", "max_kw": 5.0, "min_kw": 1.0}
PARAMS = BatteryPvFollowParams.from_frame(PVF)
FRAME = {
    "type": "command", "device_id": "bat", "action": "set_charge_mode",
    "mode": "charge", "setpoint_kw": 1.92, "pv_follow": dict(PVF),
}


# ── pure ─────────────────────────────────────────────────────────────────


def test_params_reject_malformed_frames():
    assert PARAMS == BatteryPvFollowParams("sub", 5.0, 1.0)
    assert BatteryPvFollowParams.from_frame(None) is None
    assert BatteryPvFollowParams.from_frame({"max_kw": 5.0}) is None
    assert BatteryPvFollowParams.from_frame({**PVF, "max_kw": 0}) is None
    assert BatteryPvFollowParams.from_frame({**PVF, "max_kw": "x"}) is None


def test_surplus_is_invariant_against_own_charge():
    # Feld 25.10. 10:10: Sub exportiert 1,83 kW, Akku fast still.
    before = battery_surplus_kw(-1.83, -0.05)
    # Akku lädt 1,85 kW → Sub-Export fällt um genau das.
    after = battery_surplus_kw(0.02, -1.90)
    assert before == pytest.approx(1.88) and after == pytest.approx(1.88)
    assert battery_surplus_kw(None, 0.0) is None
    assert battery_surplus_kw(0.0, None) is None


def test_running_setpoint_never_exceeds_the_surplus():
    run = BatteryPvFollowRun(params=PARAMS, refreshed_at=0.0, setpoint_kw=2.5)
    # Feld 25.10. 11:25: Wolke, Überschuss nur noch ~0,9 kW über Mindest
    assert decide_setpoint(run, 1.9, 0.0) == 1.9
    run.setpoint_kw = 1.9
    # kleine Aufwärts-Änderung: kein Write
    assert decide_setpoint(run, 2.0, 15.0) is None
    assert decide_setpoint(run, 2.1, 30.0) == 2.1
    # gedeckelt auf max_kw
    run.setpoint_kw = 2.1
    assert decide_setpoint(run, 9.0, 45.0) == 5.0


def test_below_min_holds_min_then_stops():
    run = BatteryPvFollowRun(params=PARAMS, refreshed_at=0.0, setpoint_kw=2.0)
    t0 = 1000.0
    assert decide_setpoint(run, 0.4, t0) == 1.0
    run.setpoint_kw = 1.0
    assert decide_setpoint(run, 0.4, t0 + BATTERY_PV_FOLLOW_STOP_DELAY_S - 1) is None
    assert decide_setpoint(run, 0.4, t0 + BATTERY_PV_FOLLOW_STOP_DELAY_S) == 0.0


def test_restart_needs_buffer_and_delay():
    run = BatteryPvFollowRun(params=PARAMS, refreshed_at=0.0, setpoint_kw=0.0)
    t0 = 2000.0
    assert decide_setpoint(run, 1.1, t0) is None  # unter min + Puffer
    assert decide_setpoint(run, 1.5, t0 + 15) is None
    assert decide_setpoint(run, 1.5, t0 + 15 + BATTERY_PV_FOLLOW_START_DELAY_S) == 1.5


def test_missing_measurement_stops_at_once():
    run = BatteryPvFollowRun(params=PARAMS, refreshed_at=0.0, setpoint_kw=2.0)
    assert decide_setpoint(run, None, 0.0) == 0.0
    run.setpoint_kw = 0.0
    assert decide_setpoint(run, None, 15.0) is None


# ── Coordinator-Verdrahtung ──────────────────────────────────────────────


def _devices() -> list[dict]:
    return [
        {
            CONF_DEVICE_ID: "bat", CONF_DEVICE_TYPE: "battery",
            CONF_ENTITY_POWER: "sensor.bat_power",
            CONF_ENTITY_BATTERY_MODE: "select.bat_mode",
            CONF_ENTITY_BATTERY_POWER_SETPOINT: "number.bat_setpoint",
            CONF_VALUE_BATTERY_MODE_ACTIVE: "Aktiv",
            CONF_VALUE_BATTERY_MODE_PASSIVE: "Passiv",
        },
        {
            CONF_DEVICE_ID: "sub", CONF_DEVICE_TYPE: "grid",
            CONF_ENTITY_POWER: "sensor.sub_power",
        },
    ]


def make_coordinator(hass: HomeAssistant, *, options: dict | None = None):
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=dict(options or {}))
    entry.add_to_hass(hass)
    coord = CrowdergyCoordinator.__new__(CrowdergyCoordinator)
    coord.hass = hass
    coord.entry = entry
    coord.devices = _devices()
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


def _field(hass: HomeAssistant, sub: float, bat: float, setpoint_w: float = 0.0):
    hass.states.async_set("sensor.sub_power", str(sub), {"unit_of_measurement": "kW"})
    hass.states.async_set("sensor.bat_power", str(bat), {"unit_of_measurement": "kW"})
    hass.states.async_set(
        "number.bat_setpoint", str(setpoint_w), {"min": -5000, "max": 5000},
    )
    hass.states.async_set("select.bat_mode", "Passiv")


async def _start(coord) -> BatteryPvFollowRun:
    await coord._handle_ws_message(dict(FRAME))
    run = coord._battery_pv_follow_runs()["bat"]
    coord._battery_pv_follow_tasks()["bat"].cancel()
    return run


async def test_frame_with_pv_follow_starts_and_follows_a_cloud(hass: HomeAssistant):
    coord = make_coordinator(hass)
    _field(hass, sub=-1.92, bat=0.0)
    setpoints = async_mock_service(hass, "number", "set_value")
    modes = async_mock_service(hass, "select", "select_option")

    run = await _start(coord)
    assert [c.data["value"] for c in setpoints] == [1900.0]
    assert [c.data["option"] for c in modes] == ["Aktiv"]
    assert run.setpoint_kw == 1.9

    # Wolke: Akku lädt 1,9 kW, Sub bezieht 0,8 kW → Überschuss 1,1 kW
    _field(hass, sub=0.8, bat=-1.9, setpoint_w=1900.0)
    await coord._battery_pv_follow_tick("bat", run, time.time())
    assert setpoints[-1].data["value"] == 1100.0
    assert run.setpoint_kw == 1.1


async def test_renewal_keeps_the_regulated_setpoint(hass: HomeAssistant):
    coord = make_coordinator(hass)
    _field(hass, sub=-1.92, bat=0.0)
    setpoints = async_mock_service(hass, "number", "set_value")
    async_mock_service(hass, "select", "select_option")
    run = await _start(coord)
    run.setpoint_kw = 1.1
    run.refreshed_at = 0.0
    n = len(setpoints)

    await coord._handle_ws_message({**FRAME, "setpoint_kw": 3.0})

    assert len(setpoints) == n  # Erneuerung schreibt nichts
    assert run.setpoint_kw == 1.1
    assert run.refreshed_at > 0.0


async def test_command_without_pv_follow_ends_the_regulator(hass: HomeAssistant):
    coord = make_coordinator(hass)
    _field(hass, sub=-1.92, bat=0.0)
    async_mock_service(hass, "number", "set_value")
    modes = async_mock_service(hass, "select", "select_option")
    await _start(coord)
    hass.states.async_set("select.bat_mode", "Aktiv")

    await coord._handle_ws_message({
        "type": "command", "device_id": "bat", "action": "set_charge_mode",
        "mode": "passive", "setpoint_kw": 0.0,
    })

    assert "bat" not in coord._battery_pv_follow_runs()
    assert modes[-1].data["option"] == "Passiv"


async def test_refresh_timeout_falls_back_to_passive(hass: HomeAssistant):
    coord = make_coordinator(hass)
    _field(hass, sub=-1.92, bat=0.0)
    async_mock_service(hass, "number", "set_value")
    modes = async_mock_service(hass, "select", "select_option")
    run = await _start(coord)
    hass.states.async_set("select.bat_mode", "Aktiv")
    run.refreshed_at = time.time() - BATTERY_PV_FOLLOW_REFRESH_TIMEOUT_S - 1

    import custom_components.theothergas.battery_pv_follow as mod

    real_sleep = mod.asyncio.sleep

    async def no_sleep(_s):
        await real_sleep(0)

    mod.asyncio.sleep = no_sleep
    try:
        await coord._battery_pv_follow_loop("bat")
    finally:
        mod.asyncio.sleep = real_sleep

    assert "bat" not in coord._battery_pv_follow_runs()
    assert modes[-1].data["option"] == "Passiv"


async def test_no_consent_no_write_and_no_regulator(hass: HomeAssistant):
    coord = make_coordinator(hass, options={OPT_CONSENT_REMOTE_CONTROL: False})
    _field(hass, sub=-1.92, bat=0.0)
    setpoints = async_mock_service(hass, "number", "set_value")
    await coord._handle_ws_message(dict(FRAME))
    assert not setpoints
    assert coord._battery_pv_follow_runs() == {}


async def test_old_style_frame_still_holds_the_setpoint(hass: HomeAssistant):
    coord = make_coordinator(hass)
    _field(hass, sub=-1.92, bat=0.0)
    setpoints = async_mock_service(hass, "number", "set_value")
    async_mock_service(hass, "select", "select_option")
    frame = {k: v for k, v in FRAME.items() if k != "pv_follow"}
    await coord._handle_ws_message(frame)
    assert coord._battery_pv_follow_runs() == {}
    assert [c.data["value"] for c in setpoints] == [1920.0]
