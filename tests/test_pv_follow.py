"""Lokaler PV-Überschuss-Regler (#292).

Pure Regeln (`surplus_kw`, `decide`) plus die Verdrahtung am Coordinator:
`pv_follow`-Frame → Regler-Tick → Strom/Modus-Writes, Ende durch
`set_charge_mode` / `pv_follow_stop`, Consent-Gate.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from types import SimpleNamespace

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.theothergas.const import (
    CONF_CHARGE_MODE_VALUE_LOCK,
    CONF_CHARGE_MODE_VALUE_POWER,
    CONF_CHARGE_MODE_VALUE_SOLAR,
    CONF_DEVICE_ID,
    CONF_DEVICE_TYPE,
    CONF_ENTITY_CHARGE_MODE,
    CONF_ENTITY_POWER,
    CONF_ENTITY_SOC,
    CONF_ENTITY_WALLBOX_CHARGE_CURRENT,
    DOMAIN,
    OPT_CONSENT_REMOTE_CONTROL,
)
from custom_components.theothergas import coordinator as coordinator_mod
from custom_components.theothergas.coordinator import CrowdergyCoordinator
from custom_components.theothergas.pv_follow import (
    PV_FOLLOW_MIN_ON_S,
    PV_FOLLOW_SENSOR_STALE_S,
    PV_FOLLOW_START_DELAY_S,
    PV_FOLLOW_STOP_DELAY_S,
    PV_FOLLOW_WRITE_RETRY_S,
    PvFollowParams,
    PvFollowRun,
    decide,
    reconcile_idle_draw,
    state_age_s,
    surplus_kw,
)
from custom_components.theothergas.state_mirror import DeviceStateMirror

FRAME = {
    "type": "command", "device_id": "wb", "action": "pv_follow",
    "grid_device_id": "grid", "battery_device_ids": ["bat"],
    "min_current_a": 6, "max_current_a": 16, "kw_per_amp": 0.69,
    "soc_start_pct": 80, "soc_hold_pct": 75, "start_buffer_kw": 0.5,
}
PARAMS = PvFollowParams.from_frame(FRAME)


# ── pure ─────────────────────────────────────────────────────────────────


def test_surplus_counts_filling_passive_battery():
    """Feldfall 2026-09-27: Export 3,6 kW, Akku lädt 2,8 kW bei 92 %."""
    kw = dict(grid_kw=-3.6, box_kw=0.0, running=False)
    assert surplus_kw(PARAMS, batteries=[(-2.8, 92.0)], **kw) == 6.4
    # Akku noch nicht voll genug → Akku-Vorrang, nur der Export zählt
    assert surplus_kw(PARAMS, batteries=[(-2.8, 70.0)], **kw) == 3.6
    # unbekannter SoC zählt als „wird nicht voll"
    assert surplus_kw(PARAMS, batteries=[(-2.8, None)], **kw) == 3.6
    # läuft die Box, gilt die Halte-Schwelle
    assert surplus_kw(
        PARAMS, grid_kw=0.0, box_kw=4.2, batteries=[(-1.0, 78.0)], running=True,
    ) == 5.2
    # ohne Messung keine Aussage
    assert surplus_kw(PARAMS, grid_kw=None, box_kw=0.0, batteries=[], running=False) is None


def test_surplus_subtracts_discharging_battery_regardless_of_soc():
    """Passiver Akku deckt die Box: Netz 0, Akku entlädt 4,2 kW → kein
    Überschuss — unabhängig davon, ob der Akku über der SoC-Schwelle ist."""
    for soc in (95.0, 50.0, None):
        assert surplus_kw(
            PARAMS, grid_kw=0.0, box_kw=4.2, batteries=[(4.2, soc)],
            running=True,
        ) == 0.0
    # Teildeckung: Export 1,0 + Box 4,2 − Entladung 2,0
    assert surplus_kw(
        PARAMS, grid_kw=-1.0, box_kw=4.2, batteries=[(2.0, 90.0)], running=True,
    ) == 3.2
    # ein Akku entlädt, ein anderer lädt (voll genug): beide zählen
    assert surplus_kw(
        PARAMS, grid_kw=0.0, box_kw=5.0,
        batteries=[(1.0, 90.0), (-0.5, 90.0)], running=True,
    ) == 4.5


def test_surplus_is_invariant_against_own_draw():
    before = surplus_kw(PARAMS, grid_kw=-6.4, box_kw=0.0, batteries=[], running=False)
    after = surplus_kw(PARAMS, grid_kw=-1.4, box_kw=5.0, batteries=[], running=True)
    assert before == after == 6.4


def test_start_needs_buffer_for_the_start_delay():
    run = PvFollowRun(params=PARAMS, refreshed_at=0.0, charging=False)
    t0 = 1000.0
    assert decide(run, 6.4, True, t0) == ("hold", None)
    assert decide(run, 6.4, True, t0 + PV_FOLLOW_START_DELAY_S - 1) == ("hold", None)
    assert decide(run, 6.4, True, t0 + PV_FOLLOW_START_DELAY_S) == ("start", 9)
    # zwischen min (4,14) und min + Puffer: kein Start
    run = PvFollowRun(params=PARAMS, refreshed_at=0.0, charging=False)
    decide(run, 4.3, True, t0)
    assert decide(run, 4.3, True, t0 + 600) == ("hold", None)


def test_running_box_follows_and_stops_with_hysteresis():
    t0 = 10_000.0
    run = PvFollowRun(
        params=PARAMS, refreshed_at=0.0, charging=True, amps=9,
        switched_at=t0 - PV_FOLLOW_MIN_ON_S,
    )
    # zwischen min und min + Puffer läuft sie weiter, auf 6 A
    assert decide(run, 4.3, True, t0) == ("adjust", 6)
    run.amps = 6
    assert decide(run, 4.3, True, t0 + 15) == ("hold", None)
    # unter min: erst nach der Stopp-Verzögerung
    assert decide(run, 3.0, True, t0 + 30) == ("hold", None)
    assert decide(run, 3.0, True, t0 + 30 + PV_FOLLOW_STOP_DELAY_S) == ("stop", None)
    # Ampere gedeckelt
    run = PvFollowRun(params=PARAMS, refreshed_at=0.0, charging=True, amps=6)
    assert decide(run, 20.0, True, t0) == ("adjust", 16)


def test_missing_measurement_or_unplugged_stops_at_once():
    run = PvFollowRun(params=PARAMS, refreshed_at=0.0, charging=True, amps=9)
    assert decide(run, None, True, 0.0) == ("stop", None)
    assert decide(run, 8.0, False, 0.0) == ("stop", None)
    idle = PvFollowRun(params=PARAMS, refreshed_at=0.0, charging=False)
    assert decide(idle, 8.0, False, 0.0) == ("hold", None)


def test_constants_are_reexported_from_coordinator():
    assert coordinator_mod.PV_FOLLOW_SENSOR_STALE_S == PV_FOLLOW_SENSOR_STALE_S
    assert coordinator_mod.PV_FOLLOW_WRITE_RETRY_S == PV_FOLLOW_WRITE_RETRY_S


def test_idle_draw_is_stopped_or_adopted_after_the_stop_delay():
    t0 = 5000.0
    run = PvFollowRun(params=PARAMS, refreshed_at=0.0, charging=False)
    # Box zieht 11 kW, Überschuss (ohne Box) nur 2 kW → nach Verzögerung Stopp
    assert reconcile_idle_draw(run, 11.0, 2.0, True, t0) == "none"
    assert reconcile_idle_draw(run, 11.0, 2.0, True, t0 + PV_FOLLOW_STOP_DELAY_S - 1) == "none"
    assert reconcile_idle_draw(run, 11.0, 2.0, True, t0 + PV_FOLLOW_STOP_DELAY_S) == "stop"
    # nächster Versuch erst nach einer weiteren vollen Verzögerung
    assert reconcile_idle_draw(run, 11.0, 2.0, True, t0 + PV_FOLLOW_STOP_DELAY_S + 15) == "none"
    # fehlende Messung oder abgesteckt → Stopp
    run = PvFollowRun(params=PARAMS, refreshed_at=0.0, charging=False, draw_since=t0)
    assert reconcile_idle_draw(run, 11.0, None, True, t0 + PV_FOLLOW_STOP_DELAY_S) == "stop"
    # trägt der Überschuss die Box → Übernahme
    run = PvFollowRun(params=PARAMS, refreshed_at=0.0, charging=False, draw_since=t0)
    assert reconcile_idle_draw(run, 11.0, 4.5, True, t0 + PV_FOLLOW_STOP_DELAY_S) == "adopt"
    # Box zieht nichts (oder Regler lädt selbst) → Uhr zurück
    run = PvFollowRun(params=PARAMS, refreshed_at=0.0, charging=False, draw_since=t0)
    assert reconcile_idle_draw(run, 0.2, 2.0, True, t0 + 999) == "none"
    assert run.draw_since is None
    run = PvFollowRun(params=PARAMS, refreshed_at=0.0, charging=True, draw_since=t0)
    assert reconcile_idle_draw(run, 11.0, 2.0, True, t0 + 999) == "none"
    assert run.draw_since is None


def test_state_age_prefers_last_reported():
    now = 10_000.0
    old = datetime.fromtimestamp(now - 900, tz=timezone.utc)
    fresh = datetime.fromtimestamp(now - 10, tz=timezone.utc)
    assert state_age_s(SimpleNamespace(last_reported=fresh, last_updated=old), now) == 10
    assert state_age_s(SimpleNamespace(last_updated=old), now) == 900
    assert state_age_s(SimpleNamespace(), now) is None


# ── Coordinator-Verdrahtung ──────────────────────────────────────────────


def _devices() -> list[dict]:
    return [
        {
            CONF_DEVICE_ID: "wb", CONF_DEVICE_TYPE: "wallbox",
            CONF_ENTITY_POWER: "sensor.wb_power",
            CONF_ENTITY_CHARGE_MODE: "select.wb_mode",
            CONF_ENTITY_WALLBOX_CHARGE_CURRENT: "number.wb_current",
            CONF_CHARGE_MODE_VALUE_POWER: "An",
            CONF_CHARGE_MODE_VALUE_SOLAR: "Solar",
            CONF_CHARGE_MODE_VALUE_LOCK: "Aus",
        },
        {
            CONF_DEVICE_ID: "grid", CONF_DEVICE_TYPE: "grid",
            CONF_ENTITY_POWER: "sensor.grid_power",
        },
        {
            CONF_DEVICE_ID: "bat", CONF_DEVICE_TYPE: "battery",
            CONF_ENTITY_POWER: "sensor.bat_power",
            CONF_ENTITY_SOC: "sensor.bat_soc",
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


def _field(hass: HomeAssistant, grid: float, box: float, bat: float, soc: float):
    hass.states.async_set("sensor.grid_power", str(grid), {"unit_of_measurement": "kW"})
    hass.states.async_set("sensor.wb_power", str(box), {"unit_of_measurement": "kW"})
    hass.states.async_set("sensor.bat_power", str(bat), {"unit_of_measurement": "kW"})
    hass.states.async_set("sensor.bat_soc", str(soc))
    hass.states.async_set("number.wb_current", "6", {"min": 6, "max": 16})
    hass.states.async_set("select.wb_mode", "Solar")


async def _start(coord) -> PvFollowRun:
    await coord._handle_ws_message(dict(FRAME))
    run = coord._pv_follow_runs()["wb"]
    # Hintergrund-Loop anhalten — die Tests treiben die Ticks selbst.
    coord._pv_follow_tasks()["wb"].cancel()
    return run


async def test_feldfall_starts_box_on_battery_share(hass: HomeAssistant):
    coord = make_coordinator(hass)
    coord.state.last_sse_event_at = time.time()
    _field(hass, grid=-3.6, box=0.0, bat=-2.8, soc=92.0)
    currents = async_mock_service(hass, "number", "set_value")
    modes = async_mock_service(hass, "select", "select_option")

    run = await _start(coord)
    t0 = time.time()
    await coord._pv_follow_tick("wb", run, t0)
    assert run.charging is False and not currents
    await coord._pv_follow_tick("wb", run, t0 + PV_FOLLOW_START_DELAY_S)

    assert run.charging is True
    assert [c.data["value"] for c in currents] == [9]
    assert [c.data["option"] for c in modes] == ["An"]


async def test_set_charge_mode_ends_controller_without_extra_write(hass: HomeAssistant):
    coord = make_coordinator(hass)
    _field(hass, grid=0.0, box=0.0, bat=0.0, soc=50.0)
    modes = async_mock_service(hass, "select", "select_option")
    async_mock_service(hass, "number", "set_value")
    await _start(coord)

    await coord._handle_ws_message({
        "type": "command", "device_id": "wb",
        "action": "set_charge_mode", "value": "Aus",
    })
    coord._cancel_charge_mode_hold("wb")

    assert "wb" not in coord._pv_follow_runs()
    assert [c.data["option"] for c in modes] == ["Aus"]


async def test_stop_frame_leaves_charging_box_on_solar(hass: HomeAssistant):
    coord = make_coordinator(hass)
    _field(hass, grid=-8.0, box=0.0, bat=0.0, soc=50.0)
    modes = async_mock_service(hass, "select", "select_option")
    async_mock_service(hass, "number", "set_value")
    run = await _start(coord)
    run.charging = True

    await coord._handle_ws_message({
        "type": "command", "device_id": "wb", "action": "pv_follow_stop",
    })

    assert "wb" not in coord._pv_follow_runs()
    assert [c.data["option"] for c in modes] == ["Solar"]


async def test_no_consent_no_controller(hass: HomeAssistant):
    coord = make_coordinator(hass, options={OPT_CONSENT_REMOTE_CONTROL: False})
    await coord._handle_ws_message(dict(FRAME))
    assert coord._pv_follow_runs() == {}


async def test_frozen_grid_sensor_stops_running_box(hass: HomeAssistant):
    """Eingefrorener Netz-Sensor (kein last_reported seit > Stale-Schwelle)
    gilt als fehlende Messung → laufende Box stoppt sofort."""
    coord = make_coordinator(hass)
    coord.state.last_sse_event_at = time.time()
    _field(hass, grid=-8.0, box=5.0, bat=0.0, soc=50.0)
    modes = async_mock_service(hass, "select", "select_option")
    async_mock_service(hass, "number", "set_value")
    run = await _start(coord)
    t0 = time.time()
    await coord._pv_follow_tick("wb", run, t0)
    assert run.charging is True and run.amps == 16  # Übernahme, Strom regeln
    # Box-Sensor bleibt frisch, Netz-Sensor friert ein
    later = t0 + PV_FOLLOW_SENSOR_STALE_S + 30
    real_get = coord._get_state

    def get_state(entity_id):
        st = real_get(entity_id)
        if entity_id == "sensor.wb_power":
            return SimpleNamespace(
                state=st.state, attributes=st.attributes,
                last_reported=datetime.fromtimestamp(later - 5, tz=timezone.utc),
            )
        return st

    coord._get_state = get_state
    modes.clear()
    await coord._pv_follow_tick("wb", run, later)
    assert [c.data["option"] for c in modes] == ["Solar"]
    assert run.charging is False


async def test_failed_stop_is_not_committed_and_retried(hass: HomeAssistant):
    coord = make_coordinator(hass)
    coord.state.last_sse_event_at = time.time()
    _field(hass, grid=0.0, box=5.0, bat=0.0, soc=50.0)
    async_mock_service(hass, "number", "set_value")
    run = await _start(coord)
    run.charging, run.amps = True, 7
    t0 = time.time()
    # Abgesteckt → sofortiger Stopp; der Write scheitert (kein Consent)
    coord.entry = MockConfigEntry(
        domain=DOMAIN, data={}, options={OPT_CONSENT_REMOTE_CONTROL: False},
    )
    hass.states.async_set("sensor.grid_power", "x")  # Messung fehlt
    modes = async_mock_service(hass, "select", "select_option")
    await coord._pv_follow_tick("wb", run, t0)
    assert run.charging is True and run.amps == 7 and not modes
    # innerhalb des Back-offs kein neuer Versuch
    coord.entry = make_coordinator(hass).entry
    await coord._pv_follow_tick("wb", run, t0 + 15)
    assert not modes and run.charging is True
    # danach wird der Stopp wiederholt und erst dann übernommen
    await coord._pv_follow_tick("wb", run, t0 + PV_FOLLOW_WRITE_RETRY_S)
    assert [c.data["option"] for c in modes] == ["Solar"]
    assert run.charging is False and run.amps is None


async def test_failed_start_is_not_committed(hass: HomeAssistant):
    coord = make_coordinator(hass)
    coord.state.last_sse_event_at = time.time()
    _field(hass, grid=-8.0, box=0.0, bat=0.0, soc=50.0)
    async_mock_service(hass, "number", "set_value")
    # Modus-Write wirft → Start gilt als nicht erfolgt
    async def boom(call):
        raise RuntimeError("box offline")
    hass.services.async_register("select", "select_option", boom)
    run = await _start(coord)
    t0 = time.time()
    await coord._pv_follow_tick("wb", run, t0)
    await coord._pv_follow_tick("wb", run, t0 + PV_FOLLOW_START_DELAY_S)
    assert run.charging is False and run.amps is None
    assert run.retry_at == t0 + PV_FOLLOW_START_DELAY_S + PV_FOLLOW_WRITE_RETRY_S


async def test_box_left_drawing_from_grid_is_stopped_again(hass: HomeAssistant):
    """Box steht noch auf „An" (früherer Solver-Befehl), der Regler hält sie
    für gestoppt: Netzbezug 9 kW, Box 11 kW → S = 2 kW < min → erneuter
    Stopp nach der Stopp-Verzögerung."""
    coord = make_coordinator(hass)
    coord.state.last_sse_event_at = time.time()
    _field(hass, grid=9.0, box=11.0, bat=0.0, soc=50.0)
    hass.states.async_set("select.wb_mode", "An")
    modes = async_mock_service(hass, "select", "select_option")
    async_mock_service(hass, "number", "set_value")
    run = await _start(coord)
    run.charging = False  # z. B. nach einem verschluckten Stopp
    t0 = time.time()
    await coord._pv_follow_tick("wb", run, t0)
    await coord._pv_follow_tick("wb", run, t0 + 60)
    assert not modes
    await coord._pv_follow_tick("wb", run, t0 + PV_FOLLOW_STOP_DELAY_S)
    assert [c.data["option"] for c in modes] == ["Solar"]
    assert run.charging is False and run.switched_at == t0 + PV_FOLLOW_STOP_DELAY_S


async def test_box_drawing_in_firmware_solar_mode_is_left_alone(hass: HomeAssistant):
    """Stopp-Zustand ist „Solar": lädt die Firmware dort selbst aus PV,
    schreibt der Regler „Solar" nicht alle paar Minuten neu."""
    coord = make_coordinator(hass)
    coord.state.last_sse_event_at = time.time()
    _field(hass, grid=0.5, box=1.4, bat=0.0, soc=50.0)  # select steht auf Solar
    modes = async_mock_service(hass, "select", "select_option")
    async_mock_service(hass, "number", "set_value")
    run = await _start(coord)
    run.charging = False
    t0 = time.time()
    await coord._pv_follow_tick("wb", run, t0)
    await coord._pv_follow_tick("wb", run, t0 + PV_FOLLOW_STOP_DELAY_S)
    assert not modes and run.charging is False
