"""Schreibzähler je Gerät, Kanal und Berlin-Tag (`write_counts`).

Rein beobachtend: gezählt wird jeder Service-Call, den der Connector an
Home Assistant absetzt — im Kanal des geschriebenen Slots. Was ein
Idempotenz-Guard, der Circuit-Breaker oder ein Budget vorher abfängt,
zählt nicht. Der Tagesstand geht als kumulatives Feld `write_counts` im
Telemetrie-PATCH ans Backend.

Die Tests decken: jeden Schreibpfad (genau ein Zähler im richtigen
Kanal), Skip-Pfade, Hold-Wiederholung, Tageswechsel, die Regler-Zähler
der Wallbox, die Payload-Form, die Sende-Entscheidung — und dass das
Zählen keinen einzigen Service-Call hinzufügt, entfernt oder verändert.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import httpx
import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.theothergas import command_dispatcher as dispatcher_mod
from custom_components.theothergas import coordinator as coordinator_mod
from custom_components.theothergas.const import (
    COMMAND_LEASE_TTL_S,
    CONF_CHARGE_MODE_VALUE_SOLAR,
    CONF_DEVICE_ID,
    CONF_DEVICE_TYPE,
    CONF_ENTITY_BATTERY_MODE,
    CONF_ENTITY_BATTERY_POWER_SETPOINT,
    CONF_ENTITY_CHARGE_MODE,
    CONF_ENTITY_CONTROL,
    CONF_ENTITY_CONTROL_ECO,
    CONF_ENTITY_CONTROL_HOLD,
    CONF_ENTITY_COOL_CONTROL,
    CONF_ENTITY_WALLBOX_CHARGE_CURRENT,
    CONF_ENTITY_WALLBOX_PHASE_MODE,
    CONF_VALUE_BATTERY_MODE_ACTIVE,
    CONF_VALUE_BATTERY_MODE_PASSIVE,
    CONF_VALUE_COOL_OFF,
    CONF_VALUE_COOL_ON,
    CONF_VALUE_OFF,
    CONF_VALUE_ON,
    CONF_VALUE_WALLBOX_PHASE_1,
    CONF_VALUE_WALLBOX_PHASE_3,
    DOMAIN,
    ENTITY_CONTROL_HOLD_ALWAYS,
    ENTITY_CONTROL_HOLD_AUTO,
    ENTITY_CONTROL_HOLD_NEVER,
    OPT_CONSENT_REMOTE_CONTROL,
    OPT_CONSENT_TELEMETRY,
    WRITE_BREAKER_MAX_PER_HOUR,
)
from custom_components.theothergas.coordinator import CrowdergyCoordinator
from custom_components.theothergas.pv_follow import PV_FOLLOW_START_DELAY_S
from custom_components.theothergas.state_mirror import DeviceStateMirror
from custom_components.theothergas.telemetry_composer import TelemetryComposer
from custom_components.theothergas.telemetry_reader import (
    PER_DEVICE_HEARTBEAT_INTERVAL,
    WRITE_COUNTS_REPORT_INTERVAL_S,
)
from tests import test_battery_load_follow as blf
from tests import test_battery_pv_follow as bpf
from tests import test_pv_follow as pvf

_SLEEP = "custom_components.theothergas.coordinator.asyncio.sleep"
_CHANNEL_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_RESERVED = {"starts", "current_changes"}

# Jeder Service, den der Connector je ruft (Domain → Services).
_SERVICES = {
    "switch": ("turn_on", "turn_off"),
    "number": ("set_value",),
    "input_number": ("set_value",),
    "select": ("select_option",),
    "climate": ("set_temperature", "set_hvac_mode"),
    "water_heater": ("set_temperature", "set_operation_mode"),
}


def _today() -> str:
    return datetime.now(ZoneInfo("Europe/Berlin")).date().isoformat()


def _ok_response() -> httpx.Response:
    return httpx.Response(
        200, json={}, request=httpx.Request("PATCH", "https://api.example/x")
    )


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
    coord.state.active_state_bootstrapped = True
    coord._connector_version = "9.9.9"
    coord._consent_denied_logged = set()
    coord._backend_gone_device_ids = set()
    coord._last_sent_payload = {}
    coord._last_send_at = {}
    coord._last_mirror_at = {}
    coord._last_sent_hash = {}
    coord._prev_energy_kwh = {}
    coord._prev_energy_kwh_discharged = {}
    # Netz und Nebenläufe des Telemetrie-Ticks sind hier nicht das Thema.
    coord._patch_telemetry_with_retry = AsyncMock(return_value=_ok_response())
    coord._push_outdoor_temp = AsyncMock()
    coord._self_heal_holds = AsyncMock()
    return coord


def _mock_all_services(hass: HomeAssistant) -> dict[tuple[str, str], list]:
    return {
        (domain, service): async_mock_service(hass, domain, service)
        for domain, services in _SERVICES.items()
        for service in services
    }


def _total_calls(mocks: dict[tuple[str, str], list]) -> int:
    return sum(len(calls) for calls in mocks.values())


def _channels(coord: CrowdergyCoordinator, device_id: str) -> dict[str, int]:
    snap = coord.state.write_counts_snapshot(device_id)
    return {} if snap is None else snap["channels"]


def _counted(coord: CrowdergyCoordinator) -> int:
    return sum(
        sum(rec["channels"].values()) for rec in coord.state.write_counts.values()
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


async def _run_until_stopped(coro) -> None:
    try:
        await coro
    except _StopHold:
        pass


def _switch_device(hold: str = ENTITY_CONTROL_HOLD_NEVER) -> dict:
    return {
        CONF_DEVICE_ID: "d1",
        CONF_DEVICE_TYPE: "heating",
        CONF_ENTITY_CONTROL: "switch.kessel",
        CONF_ENTITY_CONTROL_HOLD: hold,
    }


def _wallbox_device() -> dict:
    return {
        CONF_DEVICE_ID: "w1",
        CONF_DEVICE_TYPE: "wallbox",
        CONF_ENTITY_CHARGE_MODE: "select.wb_mode",
        CONF_ENTITY_WALLBOX_CHARGE_CURRENT: "number.wb_current",
        CONF_ENTITY_WALLBOX_PHASE_MODE: "select.wb_phases",
        CONF_VALUE_WALLBOX_PHASE_1: "1-phasig",
        CONF_VALUE_WALLBOX_PHASE_3: "3-phasig",
        CONF_CHARGE_MODE_VALUE_SOLAR: "Solar",
        CONF_ENTITY_CONTROL_HOLD: ENTITY_CONTROL_HOLD_NEVER,
    }


def _battery_device() -> dict:
    return {
        CONF_DEVICE_ID: "b1",
        CONF_DEVICE_TYPE: "battery",
        CONF_ENTITY_BATTERY_MODE: "select.bat_mode",
        CONF_ENTITY_BATTERY_POWER_SETPOINT: "number.bat_setpoint",
        CONF_VALUE_BATTERY_MODE_ACTIVE: "Aktiv",
        CONF_VALUE_BATTERY_MODE_PASSIVE: "Passiv",
    }


# ════════════════════════════════════════════════════════════════════
# A. Zähler im DeviceStateMirror
# ════════════════════════════════════════════════════════════════════


def test_mirror_counts_per_device_channel_and_day():
    state = DeviceStateMirror()
    assert state.write_counts_snapshot("d1") is None

    state.count_write("d1", "control", "2026-11-23")
    state.count_write("d1", "control", "2026-11-23")
    state.count_write("d1", "eco", "2026-11-23")
    state.count_write("d2", "charge_mode", "2026-11-23")

    assert state.write_counts_snapshot("d1") == {
        "day": "2026-11-23", "channels": {"control": 2, "eco": 1},
    }
    assert state.write_counts_snapshot("d2") == {
        "day": "2026-11-23", "channels": {"charge_mode": 1},
    }


def test_mirror_day_change_resets_the_device():
    state = DeviceStateMirror()
    state.count_write("d1", "control", "2026-11-23")
    state.count_regulator_action("d1", "starts", "2026-11-23")

    state.count_write("d1", "eco", "2026-11-24")

    assert state.write_counts_snapshot("d1") == {
        "day": "2026-11-24", "channels": {"eco": 1},
    }


def test_mirror_regulator_counters_appear_together():
    state = DeviceStateMirror()
    state.count_regulator_action("w1", "starts", "2026-11-23")
    assert state.write_counts_snapshot("w1") == {
        "day": "2026-11-23", "channels": {}, "starts": 1, "current_changes": 0,
    }
    state.count_regulator_action("w1", "current_changes", "2026-11-23")
    state.count_regulator_action("w1", "current_changes", "2026-11-23")
    snap = state.write_counts_snapshot("w1")
    assert (snap["starts"], snap["current_changes"]) == (1, 2)
    with pytest.raises(ValueError):
        state.count_regulator_action("w1", "stops", "2026-11-23")


def test_mirror_snapshot_is_a_copy():
    state = DeviceStateMirror()
    state.count_write("d1", "control", "2026-11-23")
    snap = state.write_counts_snapshot("d1")
    snap["channels"]["control"] = 99
    assert state.write_counts_snapshot("d1")["channels"] == {"control": 1}


def test_channel_names_follow_the_contract_and_are_reexported():
    channels = dispatcher_mod.WRITE_CHANNELS
    assert len(channels) == len(set(channels)) <= 16
    for name in channels:
        assert _CHANNEL_RE.match(name), name
        assert name not in _RESERVED
    assert set(coordinator_mod.WRITE_COUNT_REGULATOR_KEYS) == _RESERVED
    assert coordinator_mod.WRITE_CHANNELS is channels
    assert coordinator_mod.WRITE_COUNTS_FIELD == "write_counts"
    assert (
        coordinator_mod.WRITE_COUNTS_REPORT_INTERVAL_S
        == WRITE_COUNTS_REPORT_INTERVAL_S
    )


# ════════════════════════════════════════════════════════════════════
# B. Jeder Schreibpfad zählt genau einen Call im richtigen Kanal
# ════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("entity_id", "raw_value", "service"),
    [
        ("switch.kessel", "", "turn_on"),
        ("number.sg_ready", "3", "set_value"),
        ("input_number.sg_ready", "3", "set_value"),
        ("select.sg_ready", "Erhöht", "select_option"),
        ("climate.wp", "45", "set_temperature"),
        ("climate.split", "heat", "set_hvac_mode"),
        ("water_heater.boiler", "55", "set_temperature"),
        ("water_heater.boiler", "performance", "set_operation_mode"),
    ],
)
async def test_every_control_domain_counts_one_control_write(
    hass: HomeAssistant, entity_id, raw_value, service,
):
    domain = entity_id.split(".", 1)[0]
    coord = make_coordinator(hass, [{
        CONF_DEVICE_ID: "d1",
        CONF_DEVICE_TYPE: "generic",
        CONF_ENTITY_CONTROL: entity_id,
    }])
    mocks = _mock_all_services(hass)

    await coord._write_entity_control(
        entity_id, domain, raw_value, True, verbose=True, device_id="d1",
    )

    assert len(mocks[(domain, service)]) == 1
    assert _total_calls(mocks) == 1
    assert coord.state.write_counts_snapshot("d1") == {
        "day": _today(), "channels": {"control": 1},
    }


async def test_sse_is_on_frame_counts_control(hass: HomeAssistant):
    coord = make_coordinator(hass, [_switch_device()])
    coord.state.active_state["d1"] = True
    hass.states.async_set("switch.kessel", "off")
    mocks = _mock_all_services(hass)

    await coord._handle_ws_message(
        {"type": "telemetry", "device_id": "d1", "data": {"is_on": True}}
    )

    assert len(mocks[("switch", "turn_on")]) == 1
    assert _channels(coord, "d1") == {"control": 1}


async def test_idempotent_skip_counts_nothing(hass: HomeAssistant):
    """Die Entity steht schon auf dem kommandierten Wert → kein Call,
    kein Zähler, kein Eintrag."""
    coord = make_coordinator(hass, [_switch_device()])
    hass.states.async_set("switch.kessel", "on")
    mocks = _mock_all_services(hass)

    await coord._apply_device_state("d1", True)

    assert _total_calls(mocks) == 0
    assert coord.state.write_counts_snapshot("d1") is None


async def test_tripped_breaker_counts_nothing(hass: HomeAssistant):
    coord = make_coordinator(hass, [_switch_device()])
    hass.states.async_set("switch.kessel", "off")
    coord.state.entity_write_counts["switch.kessel"] = (
        time.time(), WRITE_BREAKER_MAX_PER_HOUR,
    )
    mocks = _mock_all_services(hass)

    await coord._apply_device_state("d1", True)

    assert _total_calls(mocks) == 0
    assert coord.state.write_counts_snapshot("d1") is None


async def test_no_remote_control_consent_counts_nothing(hass: HomeAssistant):
    coord = make_coordinator(
        hass, [_switch_device()], options={OPT_CONSENT_REMOTE_CONTROL: False},
    )
    hass.states.async_set("switch.kessel", "off")
    mocks = _mock_all_services(hass)

    await coord._apply_device_state("d1", True)

    assert _total_calls(mocks) == 0
    assert coord.state.write_counts_snapshot("d1") is None


async def test_bad_value_that_never_reaches_ha_counts_nothing(hass: HomeAssistant):
    """Ein nicht-numerischer Wert für eine Number-Entity scheitert vor
    dem Service-Call — es wurde nichts abgesetzt."""
    coord = make_coordinator(hass, [{
        CONF_DEVICE_ID: "d1", CONF_DEVICE_TYPE: "generic",
        CONF_ENTITY_CONTROL: "number.sg_ready",
    }])
    mocks = _mock_all_services(hass)

    await coord._write_entity_control(
        "number.sg_ready", "number", "kaputt", True, verbose=True,
        device_id="d1",
    )

    assert _total_calls(mocks) == 0
    assert coord.state.write_counts_snapshot("d1") is None


async def test_failed_service_call_is_still_counted(hass: HomeAssistant):
    """Ein Call, der abgesetzt wurde und dann wirft, bleibt gezählt —
    er kann das Gerät erreicht haben."""
    coord = make_coordinator(hass, [_wallbox_device()])
    hass.states.async_set("select.wb_mode", "Solar")

    async def boom(call):
        raise RuntimeError("box offline")

    hass.services.async_register("select", "select_option", boom)

    ok = await coord._apply_charge_mode("w1", "An", schedule_hold=False)

    assert ok is False
    assert _channels(coord, "w1") == {"charge_mode": 1}


async def test_eco_setpoint_counts_in_its_own_channel(hass: HomeAssistant):
    coord = make_coordinator(hass, [{
        CONF_DEVICE_ID: "d1",
        CONF_DEVICE_TYPE: "warmwater",
        CONF_ENTITY_CONTROL: "number.wp_komfort",
        CONF_ENTITY_CONTROL_ECO: "number.wp_eco",
        CONF_VALUE_ON: "55",
        CONF_VALUE_OFF: "40",
        CONF_ENTITY_CONTROL_HOLD: ENTITY_CONTROL_HOLD_NEVER,
    }])
    hass.states.async_set("number.wp_komfort", "40", {"min": 10, "max": 65})
    hass.states.async_set("number.wp_eco", "40", {"min": 10, "max": 65})
    mocks = _mock_all_services(hass)

    await coord._apply_device_state("d1", True)

    assert _total_calls(mocks) == 2
    assert _channels(coord, "d1") == {"control": 1, "eco": 1}

    # Komfort steht schon, nur der ECO-Sollwert weicht ab → nur `eco`.
    hass.states.async_set("number.wp_komfort", "55", {"min": 10, "max": 65})
    await coord._apply_device_state("d1", True)

    assert _total_calls(mocks) == 3
    assert _channels(coord, "d1") == {"control": 1, "eco": 2}


async def test_dedicated_cool_entity_counts_cool_control(hass: HomeAssistant):
    coord = make_coordinator(hass, [{
        CONF_DEVICE_ID: "d1",
        CONF_DEVICE_TYPE: "heating",
        CONF_ENTITY_CONTROL: "switch.kessel",
        CONF_ENTITY_COOL_CONTROL: "select.wp_kuehlen",
        CONF_VALUE_COOL_ON: "Kühlen",
        CONF_VALUE_COOL_OFF: "Aus",
    }])
    mocks = _mock_all_services(hass)

    await coord._apply_cool_state("d1", True)

    assert len(mocks[("select", "select_option")]) == 1
    assert _total_calls(mocks) == 1
    assert _channels(coord, "d1") == {"cool_control": 1}


async def test_climate_cool_fallback_counts_on_the_control_entity(
    hass: HomeAssistant,
):
    """Ohne eigene Kühl-Entity geht der Kühl-Modus auf die Steuer-Entity
    selbst — der Zugriff zählt in deren Kanal. Steht der Modus schon,
    wird weder geschrieben noch gezählt."""
    coord = make_coordinator(hass, [{
        CONF_DEVICE_ID: "d1",
        CONF_DEVICE_TYPE: "aircon",
        CONF_ENTITY_CONTROL: "climate.split",
    }])
    hass.states.async_set("climate.split", "off")
    mocks = _mock_all_services(hass)

    await coord._apply_cool_state("d1", True)

    assert len(mocks[("climate", "set_hvac_mode")]) == 1
    assert _channels(coord, "d1") == {"control": 1}

    hass.states.async_set("climate.split", "cool")
    await coord._apply_cool_state("d1", True)

    assert _total_calls(mocks) == 1
    assert _channels(coord, "d1") == {"control": 1}


async def test_wallbox_command_counts_phase_current_and_mode(hass: HomeAssistant):
    coord = make_coordinator(hass, [_wallbox_device()])
    coord.state.last_sse_event_at = time.time()
    hass.states.async_set("number.wb_current", "6", {"min": 6, "max": 16})
    mocks = _mock_all_services(hass)

    await coord._handle_ws_message({
        "type": "command", "device_id": "w1", "action": "set_charge_mode",
        "value": "An", "current_a": 9, "phases": 1,
    })

    assert _total_calls(mocks) == 3
    assert _channels(coord, "w1") == {
        "charge_phases": 1, "charge_current": 1, "charge_mode": 1,
    }

    # Solar: weder Strom noch Phase → nur der Modus.
    await coord._handle_ws_message({
        "type": "command", "device_id": "w1", "action": "set_charge_mode",
        "value": "Solar",
    })

    assert _total_calls(mocks) == 4
    assert _channels(coord, "w1") == {
        "charge_phases": 1, "charge_current": 1, "charge_mode": 2,
    }


async def test_numeric_charge_mode_entity_counts_charge_mode(hass: HomeAssistant):
    coord = make_coordinator(hass, [{
        CONF_DEVICE_ID: "w1",
        CONF_DEVICE_TYPE: "wallbox",
        CONF_ENTITY_CHARGE_MODE: "number.wb_mode",
        CONF_ENTITY_CONTROL_HOLD: ENTITY_CONTROL_HOLD_NEVER,
    }])
    hass.states.async_set("number.wb_mode", "0", {"min": 0, "max": 3})
    mocks = _mock_all_services(hass)

    await coord._apply_charge_mode("w1", "2")

    assert len(mocks[("number", "set_value")]) == 1
    assert _channels(coord, "w1") == {"charge_mode": 1}


async def test_unmapped_phase_entity_counts_no_phase_write(hass: HomeAssistant):
    dev = _wallbox_device()
    dev.pop(CONF_ENTITY_WALLBOX_PHASE_MODE)
    coord = make_coordinator(hass, [dev])
    hass.states.async_set("number.wb_current", "6", {"min": 6, "max": 16})
    mocks = _mock_all_services(hass)

    await coord._apply_charge_mode(
        "w1", "An", charge_current_a=8, charge_phases=3,
    )

    assert _total_calls(mocks) == 2
    assert _channels(coord, "w1") == {"charge_current": 1, "charge_mode": 1}


async def test_battery_active_counts_setpoint_and_mode(hass: HomeAssistant):
    coord = make_coordinator(hass, [_battery_device()])
    hass.states.async_set("select.bat_mode", "Passiv")
    hass.states.async_set("number.bat_setpoint", "0", {"min": -5000, "max": 5000})
    mocks = _mock_all_services(hass)

    assert await coord._apply_battery_setpoint("b1", "charge", 2.0) is True

    assert _total_calls(mocks) == 2
    assert _channels(coord, "b1") == {"battery_setpoint": 1, "battery_mode": 1}

    # Zielzustand steht (±10 W, Modus Aktiv) → kein Call, kein Zähler.
    hass.states.async_set("select.bat_mode", "Aktiv")
    hass.states.async_set(
        "number.bat_setpoint", "2005", {"min": -5000, "max": 5000},
    )
    assert await coord._apply_battery_setpoint("b1", "charge", 2.0) is True

    assert _total_calls(mocks) == 2
    assert _channels(coord, "b1") == {"battery_setpoint": 1, "battery_mode": 1}


async def test_battery_passive_counts_only_the_mode(hass: HomeAssistant):
    coord = make_coordinator(hass, [_battery_device()])
    hass.states.async_set("select.bat_mode", "Aktiv")
    hass.states.async_set("number.bat_setpoint", "2000", {"min": -5000, "max": 5000})
    mocks = _mock_all_services(hass)

    await coord._apply_battery_setpoint("b1", "passive", 0.0)

    assert _total_calls(mocks) == 1
    assert _channels(coord, "b1") == {"battery_mode": 1}


# ════════════════════════════════════════════════════════════════════
# C. Wiederholer: Hold, Self-Heal, Resync, Lease, AI-aus
# ════════════════════════════════════════════════════════════════════


async def test_always_hold_repeat_counts_every_rewrite(hass: HomeAssistant):
    coord = make_coordinator(hass, [_switch_device(ENTITY_CONTROL_HOLD_ALWAYS)])
    coord.state.active_state["d1"] = True
    coord.state.last_sse_event_at = time.time()
    hass.states.async_set("switch.kessel", "on")
    mocks = _mock_all_services(hass)

    with patch(_SLEEP, _breaking_sleep(4)):  # Initial-Delay + drei Ticks
        await _run_until_stopped(
            coord._hold_loop("d1", "switch.kessel", "on", "switch", True,
                             ENTITY_CONTROL_HOLD_ALWAYS)
        )

    assert len(mocks[("switch", "turn_on")]) == 3
    assert _channels(coord, "d1") == {"control": 3}


async def test_auto_hold_without_drift_counts_nothing(hass: HomeAssistant):
    coord = make_coordinator(hass, [_switch_device(ENTITY_CONTROL_HOLD_AUTO)])
    coord.state.active_state["d1"] = True
    coord.state.last_sse_event_at = time.time()
    hass.states.async_set("switch.kessel", "on")
    mocks = _mock_all_services(hass)

    with patch(_SLEEP, _breaking_sleep(4)):
        await _run_until_stopped(
            coord._hold_loop("d1", "switch.kessel", "on", "switch", True,
                             ENTITY_CONTROL_HOLD_AUTO)
        )

    assert _total_calls(mocks) == 0
    assert coord.state.write_counts_snapshot("d1") is None


async def test_charge_mode_hold_repeat_counts_mode_and_current(
    hass: HomeAssistant,
):
    coord = make_coordinator(hass, [_wallbox_device()])
    coord.state.last_sse_event_at = time.time()
    coord.state.held_charge_mode["w1"] = "An"
    coord.state.held_charge_current["w1"] = 9
    hass.states.async_set("number.wb_current", "6", {"min": 6, "max": 16})
    mocks = _mock_all_services(hass)

    with patch(_SLEEP, _breaking_sleep(3)):  # Initial-Delay + zwei Ticks
        await _run_until_stopped(coord._charge_mode_hold_loop("w1"))

    assert _total_calls(mocks) == 4
    assert _channels(coord, "w1") == {"charge_current": 2, "charge_mode": 2}


async def test_self_heal_write_counts_control(hass: HomeAssistant):
    coord = make_coordinator(hass, [_switch_device(ENTITY_CONTROL_HOLD_AUTO)])
    del coord._self_heal_holds  # der echte Self-Heal
    coord.state.active_state["d1"] = True
    coord.state.on_state["d1"] = True
    coord.state.last_sse_event_at = time.time()
    hass.states.async_set("switch.kessel", "off")
    mocks = _mock_all_services(hass)

    await coord._self_heal_holds(["d1"])
    coord._cancel_hold("d1")

    assert len(mocks[("switch", "turn_on")]) == 1
    assert _channels(coord, "d1") == {"control": 1}


async def test_resync_reapply_counts_control(hass: HomeAssistant):
    coord = make_coordinator(hass, [_switch_device()])
    coord.state.active_state["d1"] = True
    coord.state.on_state["d1"] = False
    hass.states.async_set("switch.kessel", "off")
    coord._authenticated_request = AsyncMock(return_value=httpx.Response(
        200,
        json=[{"id": "d1", "is_active": True, "is_on": True, "cool_on": False}],
        request=httpx.Request("GET", "https://api.example/x"),
    ))
    mocks = _mock_all_services(hass)

    await TelemetryComposer(coord).resync_once()

    assert len(mocks[("switch", "turn_on")]) == 1
    assert _channels(coord, "d1") == {"control": 1}


async def test_lease_safe_default_counts_wallbox_and_battery(hass: HomeAssistant):
    coord = make_coordinator(hass, [_wallbox_device(), _battery_device()])
    coord.state.last_sse_event_at = time.time() - COMMAND_LEASE_TTL_S - 5
    hass.states.async_set("select.bat_mode", "Aktiv")
    mocks = _mock_all_services(hass)

    await coord._charge_mode_lease_expiry("w1")
    await coord._charge_mode_lease_expiry("b1")

    assert [c.data["option"] for c in mocks[("select", "select_option")]] == [
        "Solar", "Passiv",
    ]
    assert _channels(coord, "w1") == {"charge_mode": 1}
    assert _channels(coord, "b1") == {"battery_mode": 1}


async def test_ai_off_handover_counts_its_write(hass: HomeAssistant):
    coord = make_coordinator(hass, [_switch_device(), _battery_device()])
    hass.states.async_set("switch.kessel", "on")
    hass.states.async_set("select.bat_mode", "Aktiv")
    mocks = _mock_all_services(hass)

    await coord._handover_ai_off("d1")
    await coord._handover_ai_off("b1")

    assert len(mocks[("switch", "turn_off")]) == 1
    assert _channels(coord, "d1") == {"control": 1}
    assert _channels(coord, "b1") == {"battery_mode": 1}


# ════════════════════════════════════════════════════════════════════
# D. Lokale Regler
# ════════════════════════════════════════════════════════════════════


async def test_pv_follow_counts_starts_and_current_changes(hass: HomeAssistant):
    coord = pvf.make_coordinator(hass)
    coord.state.last_sse_event_at = time.time()
    pvf._field(hass, grid=-3.6, box=0.0, bat=-2.8, soc=92.0)
    currents = async_mock_service(hass, "number", "set_value")
    modes = async_mock_service(hass, "select", "select_option")

    run = await pvf._start(coord)
    t0 = time.time()
    await coord._pv_follow_tick("wb", run, t0)
    # Start-Verzögerung läuft: nichts geschrieben, nichts gezählt.
    assert coord.state.write_counts_snapshot("wb") is None

    await coord._pv_follow_tick("wb", run, t0 + PV_FOLLOW_START_DELAY_S)
    assert run.charging is True
    assert coord.state.write_counts_snapshot("wb") == {
        "day": _today(),
        "channels": {"charge_current": 1, "charge_mode": 1},
        "starts": 1,
        "current_changes": 0,
    }

    # Mehr Überschuss → Stromänderung 9 A → 10 A.
    pvf._field(hass, grid=-1.0, box=6.2, bat=0.0, soc=92.0)
    await coord._pv_follow_tick("wb", run, t0 + PV_FOLLOW_START_DELAY_S + 15)
    assert [c.data["value"] for c in currents] == [9, 10]
    assert coord.state.write_counts_snapshot("wb") == {
        "day": _today(),
        "channels": {"charge_current": 2, "charge_mode": 2},
        "starts": 1,
        "current_changes": 1,
    }

    # Gleicher Überschuss → hold: kein Call, kein Zähler.
    await coord._pv_follow_tick("wb", run, t0 + PV_FOLLOW_START_DELAY_S + 30)
    assert len(currents) == 2 and len(modes) == 2
    assert coord.state.write_counts_snapshot("wb")["current_changes"] == 1

    # Stopp schreibt den Modus, ist aber weder Start noch Stromänderung.
    assert await coord._pv_follow_write_stop("wb") is True
    snap = coord.state.write_counts_snapshot("wb")
    assert snap["channels"] == {"charge_current": 2, "charge_mode": 3}
    assert (snap["starts"], snap["current_changes"]) == (1, 1)


async def test_pv_follow_failed_start_is_not_a_start(hass: HomeAssistant):
    """Scheitert der Modus-Write, ist die Aktion nicht geschrieben: die
    abgesetzten Calls zählen in ihren Kanälen, `starts` nicht."""
    coord = pvf.make_coordinator(hass)
    coord.state.last_sse_event_at = time.time()
    pvf._field(hass, grid=-8.0, box=0.0, bat=0.0, soc=50.0)
    async_mock_service(hass, "number", "set_value")

    async def boom(call):
        raise RuntimeError("box offline")

    hass.services.async_register("select", "select_option", boom)
    run = await pvf._start(coord)
    t0 = time.time()
    await coord._pv_follow_tick("wb", run, t0)
    await coord._pv_follow_tick("wb", run, t0 + PV_FOLLOW_START_DELAY_S)

    assert run.charging is False
    assert coord.state.write_counts_snapshot("wb") == {
        "day": _today(),
        "channels": {"charge_current": 1, "charge_mode": 1},
    }


async def test_battery_pv_follow_counts_setpoint_writes(hass: HomeAssistant):
    coord = bpf.make_coordinator(hass)
    bpf._field(hass, sub=-1.92, bat=0.0)
    setpoints = async_mock_service(hass, "number", "set_value")
    async_mock_service(hass, "select", "select_option")

    run = await bpf._start(coord)
    assert _channels(coord, "bat") == {"battery_setpoint": 1, "battery_mode": 1}

    # Wolke: der Regler senkt den Setpoint; der Modus steht schon.
    bpf._field(hass, sub=0.8, bat=-1.9, setpoint_w=1900.0)
    hass.states.async_set("select.bat_mode", "Aktiv")
    await coord._battery_pv_follow_tick("bat", run, time.time())

    assert len(setpoints) == 2
    assert _channels(coord, "bat") == {"battery_setpoint": 2, "battery_mode": 1}


async def test_battery_load_follow_counts_setpoint_writes(hass: HomeAssistant):
    coord = bpf.make_coordinator(hass)
    bpf._field(hass, sub=1.81, bat=0.0)
    setpoints = async_mock_service(hass, "number", "set_value")
    async_mock_service(hass, "select", "select_option")

    run = await blf._start(coord)
    assert _channels(coord, "bat") == {"battery_setpoint": 1, "battery_mode": 1}

    # Last weg: der Regler nimmt die Entladung zurück.
    bpf._field(hass, sub=-1.0, bat=0.86, setpoint_w=-850.0)
    hass.states.async_set("select.bat_mode", "Aktiv")
    t0 = time.time()
    for i in range(5):
        await coord._battery_load_follow_tick("bat", run, t0 + i * blf.STEP)

    assert _channels(coord, "bat") == {
        "battery_setpoint": len(setpoints), "battery_mode": 1,
    }
    assert len(setpoints) >= 2


# ════════════════════════════════════════════════════════════════════
# E. Tageswechsel (Berlin)
# ════════════════════════════════════════════════════════════════════


class _FakeDatetime:
    current = datetime(2026, 11, 23, 23, 59, tzinfo=ZoneInfo("Europe/Berlin"))

    @classmethod
    def now(cls, tz=None):
        return cls.current.astimezone(tz) if tz else cls.current


async def test_berlin_day_change_starts_a_new_count(hass: HomeAssistant):
    coord = make_coordinator(hass, [_wallbox_device()])
    mocks = _mock_all_services(hass)
    berlin = ZoneInfo("Europe/Berlin")

    with patch.object(dispatcher_mod, "datetime", _FakeDatetime):
        _FakeDatetime.current = datetime(2026, 11, 23, 23, 59, tzinfo=berlin)
        await coord._apply_charge_mode("w1", "An", schedule_hold=False)
        await coord._apply_charge_mode("w1", "An", schedule_hold=False)
        assert coord.state.write_counts_snapshot("w1") == {
            "day": "2026-11-23", "channels": {"charge_mode": 2},
        }

        _FakeDatetime.current = datetime(2026, 11, 24, 0, 0, 30, tzinfo=berlin)
        await coord._apply_charge_mode("w1", "Solar", schedule_hold=False)

    assert _total_calls(mocks) == 3
    assert coord.state.write_counts_snapshot("w1") == {
        "day": "2026-11-24", "channels": {"charge_mode": 1},
    }


async def test_day_is_the_berlin_date_not_utc(hass: HomeAssistant):
    """23:30 UTC ist in Berlin schon der nächste Tag."""
    coord = make_coordinator(hass, [_wallbox_device()])
    _mock_all_services(hass)

    with patch.object(dispatcher_mod, "datetime", _FakeDatetime):
        _FakeDatetime.current = datetime(
            2026, 11, 23, 23, 30, tzinfo=ZoneInfo("UTC"),
        )
        await coord._apply_charge_mode("w1", "An", schedule_hold=False)

    assert coord.state.write_counts_snapshot("w1")["day"] == "2026-11-24"


# ════════════════════════════════════════════════════════════════════
# F. Meldung im Telemetrie-PATCH
# ════════════════════════════════════════════════════════════════════


def _sent_payloads(coord: CrowdergyCoordinator) -> list[dict]:
    return [c.args[1] for c in coord._patch_telemetry_with_retry.await_args_list]


async def test_payload_has_exactly_the_contract_shape(hass: HomeAssistant):
    coord = make_coordinator(hass, [_wallbox_device()])
    hass.states.async_set("select.wb_mode", "Solar")
    hass.states.async_set("number.wb_current", "6", {"min": 6, "max": 16})
    _mock_all_services(hass)
    await coord._apply_charge_mode(
        "w1", "An", schedule_hold=False, charge_current_a=9, charge_phases=3,
    )
    await coord._apply_charge_mode("w1", "An", schedule_hold=False)
    coord._count_regulator_action("w1", "starts")

    await coord._async_update_data()

    field = _sent_payloads(coord)[-1]["write_counts"]
    assert field == {
        "day": _today(),
        "channels": {"charge_phases": 1, "charge_current": 1, "charge_mode": 2},
        "starts": 1,
        "current_changes": 0,
    }
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", field["day"])
    for name, value in field["channels"].items():
        assert _CHANNEL_RE.match(name) and name not in _RESERVED
        assert type(value) is int and 0 <= value <= 1_000_000
    assert type(field["starts"]) is int
    assert type(field["current_changes"]) is int
    # Nie eine Entity-ID, nichts Kundenspezifisches.
    wire = json.dumps(field)
    for entity_id in ("select.wb_mode", "number.wb_current", "select.wb_phases"):
        assert entity_id not in wire
    assert "." not in "".join(field["channels"])


async def test_device_without_regulator_sends_no_regulator_counters(
    hass: HomeAssistant,
):
    coord = make_coordinator(hass, [_switch_device()])
    hass.states.async_set("switch.kessel", "off")
    _mock_all_services(hass)
    await coord._apply_device_state("d1", True)

    await coord._async_update_data()

    assert _sent_payloads(coord)[-1]["write_counts"] == {
        "day": _today(), "channels": {"control": 1},
    }


async def test_no_write_today_sends_no_field(hass: HomeAssistant):
    coord = make_coordinator(hass, [_switch_device()])
    hass.states.async_set("switch.kessel", "off")

    await coord._async_update_data()

    assert "write_counts" not in _sent_payloads(coord)[-1]


async def test_no_telemetry_consent_sends_nothing(hass: HomeAssistant):
    """Das Feld fährt im Telemetrie-PATCH — ohne Telemetrie-Consent gibt
    es keinen PATCH, also auch keinen Zählerstand."""
    coord = make_coordinator(
        hass, [_switch_device()], options={OPT_CONSENT_TELEMETRY: False},
    )
    hass.states.async_set("switch.kessel", "off")
    _mock_all_services(hass)
    await coord._apply_device_state("d1", True)

    await coord._async_update_data()

    coord._patch_telemetry_with_retry.assert_not_awaited()
    assert _channels(coord, "d1") == {"control": 1}  # lokal weitergezählt


async def test_unchanged_counters_trigger_no_patch(hass: HomeAssistant):
    coord = make_coordinator(hass, [_switch_device()])
    hass.states.async_set("switch.kessel", "off")
    _mock_all_services(hass)
    await coord._apply_device_state("d1", True)

    await coord._async_update_data()   # erster Send
    await coord._async_update_data()   # nichts Neues
    coord._last_send_at["d1"] = time.time() - WRITE_COUNTS_REPORT_INTERVAL_S - 60
    await coord._async_update_data()   # auch nach Ablauf der Zähler-Uhr nicht

    assert coord._patch_telemetry_with_retry.await_count == 1


async def test_changed_counter_alone_reports_only_after_the_interval(
    hass: HomeAssistant,
):
    coord = make_coordinator(hass, [_switch_device()])
    hass.states.async_set("switch.kessel", "off")
    _mock_all_services(hass)
    await coord._apply_device_state("d1", True)
    await coord._async_update_data()
    assert coord._patch_telemetry_with_retry.await_count == 1

    await coord._apply_device_state("d1", True)   # Zähler 1 → 2
    await coord._async_update_data()
    assert coord._patch_telemetry_with_retry.await_count == 1

    # Auch der 90-s-Soft-Heartbeat feuert nicht für einen Zähler allein.
    coord._last_send_at["d1"] = time.time() - PER_DEVICE_HEARTBEAT_INTERVAL - 30
    await coord._async_update_data()
    assert coord._patch_telemetry_with_retry.await_count == 1

    coord._last_send_at["d1"] = time.time() - WRITE_COUNTS_REPORT_INTERVAL_S - 1
    await coord._async_update_data()
    assert coord._patch_telemetry_with_retry.await_count == 2
    assert _sent_payloads(coord)[-1]["write_counts"]["channels"] == {"control": 2}


async def test_changed_counter_rides_along_a_patch_that_is_due_anyway(
    hass: HomeAssistant,
):
    coord = make_coordinator(hass, [_switch_device()])
    hass.states.async_set("switch.kessel", "off")
    _mock_all_services(hass)
    await coord._apply_device_state("d1", True)
    await coord._async_update_data()

    await coord._apply_device_state("d1", True)
    hass.states.async_set("switch.kessel", "on")   # is_on kippt → PATCH fällig
    await coord._async_update_data()

    assert coord._patch_telemetry_with_retry.await_count == 2
    assert _sent_payloads(coord)[-1]["write_counts"]["channels"] == {"control": 2}


async def test_sent_snapshot_is_not_the_live_counter(hass: HomeAssistant):
    """Der gesendete Stand ist eine Kopie — sonst sähe der Vergleich mit
    dem letzten Send nie eine Änderung."""
    coord = make_coordinator(hass, [_switch_device()])
    hass.states.async_set("switch.kessel", "off")
    _mock_all_services(hass)
    await coord._apply_device_state("d1", True)
    await coord._async_update_data()

    await coord._apply_device_state("d1", True)

    assert coord._last_sent_payload["d1"]["write_counts"]["channels"] == {
        "control": 1,
    }


async def test_previous_day_is_reported_once_then_dropped(hass: HomeAssistant):
    """Seit Mitternacht kein Write: der Schluss-Stand des Vortags geht
    einmal raus (falls noch nicht gemeldet) und wird dann verworfen."""
    coord = make_coordinator(hass, [_switch_device()])
    hass.states.async_set("switch.kessel", "off")
    coord.state.count_write("d1", "control", "2000-01-01")

    await coord._async_update_data()
    assert _sent_payloads(coord)[-1]["write_counts"] == {
        "day": "2000-01-01", "channels": {"control": 1},
    }

    coord._last_send_at["d1"] = time.time() - 700.0   # Hard-Ceiling-PATCH
    await coord._async_update_data()
    assert coord._patch_telemetry_with_retry.await_count == 2
    assert "write_counts" not in _sent_payloads(coord)[-1]
    assert coord.state.write_counts_snapshot("d1") is None


async def test_mirror_does_not_replay_the_counters(hass: HomeAssistant):
    """Der Mirror hält nur die Freshness-Uhr frisch; die Zähler meldet
    allein der Telemetrie-Tick."""
    coord = make_coordinator(hass, [_switch_device()])
    coord._last_sent_payload["d1"] = {
        "power_kw": 1.0, "is_online": True,
        "write_counts": {"day": _today(), "channels": {"control": 4}},
    }
    coord._last_send_at["d1"] = time.time() - 120.0

    await TelemetryComposer(coord).mirror_once()

    coord._patch_telemetry_with_retry.assert_awaited_once_with(
        "d1", {"power_kw": 1.0, "is_online": True},
    )


def test_payload_hash_ignores_the_counters(hass: HomeAssistant):
    coord = make_coordinator(hass, [])
    base = {"power_kw": 1.0, "is_online": True}
    with_counts = {
        **base, "write_counts": {"day": _today(), "channels": {"control": 7}},
    }
    assert coord._payload_hash(base) == coord._payload_hash(with_counts)
    assert coord._payload_hash(base) != coord._payload_hash({**base, "power_kw": 2.0})


# ════════════════════════════════════════════════════════════════════
# G. Rein beobachtend — kein Call mehr, keiner weniger, keiner anders
# ════════════════════════════════════════════════════════════════════


async def _mixed_scenario(hass: HomeAssistant, *, counting: bool):
    """Ein Lauf quer über alle Schreibpfade inkl. Skip-Pfaden. Liefert
    die abgesetzten Service-Calls in Reihenfolge und den Coordinator."""
    for entity_id in list(hass.states.async_entity_ids()):
        hass.states.async_remove(entity_id)
    coord = make_coordinator(hass, [
        {
            CONF_DEVICE_ID: "ww",
            CONF_DEVICE_TYPE: "warmwater",
            CONF_ENTITY_CONTROL: "climate.wp",
            CONF_ENTITY_CONTROL_ECO: "number.wp_eco",
            CONF_VALUE_ON: "55",
            CONF_VALUE_OFF: "40",
            CONF_ENTITY_CONTROL_HOLD: ENTITY_CONTROL_HOLD_NEVER,
        },
        {
            CONF_DEVICE_ID: "ac",
            CONF_DEVICE_TYPE: "aircon",
            CONF_ENTITY_CONTROL: "climate.split",
            CONF_VALUE_ON: "heat",
            CONF_VALUE_OFF: "off",
            CONF_ENTITY_CONTROL_HOLD: ENTITY_CONTROL_HOLD_NEVER,
        },
        _switch_device(ENTITY_CONTROL_HOLD_ALWAYS),
        _wallbox_device(),
        _battery_device(),
    ])
    if not counting:
        coord._count_write = lambda *_a, **_k: None
        coord._count_regulator_action = lambda *_a, **_k: None
    coord.state.last_sse_event_at = time.time()
    for device_id in ("ww", "ac", "d1", "w1", "b1"):
        coord.state.active_state[device_id] = True
    hass.states.async_set(
        "climate.wp", "heat", {"temperature": 40, "min_temp": 30, "max_temp": 60},
    )
    hass.states.async_set("number.wp_eco", "40", {"min": 30, "max": 50})
    hass.states.async_set("climate.split", "off")
    hass.states.async_set("switch.kessel", "on")
    hass.states.async_set("number.wb_current", "6", {"min": 6, "max": 16})
    hass.states.async_set("select.bat_mode", "Passiv")
    hass.states.async_set("number.bat_setpoint", "0", {"min": -3000, "max": 3000})

    log: list[tuple[str, str, dict]] = []

    def _record(domain: str, service: str):
        async def _handler(call):
            log.append((domain, service, dict(call.data)))
        return _handler

    for domain, services in _SERVICES.items():
        for service in services:
            hass.services.async_register(domain, service, _record(domain, service))

    await coord._apply_device_state("ww", True)        # Komfort + ECO (Clamp 50)
    await coord._apply_device_state("ww", False)       # beide stehen → Skip
    await coord._apply_cool_state("ac", True)          # Kühlen auf der Steuer-Entity
    await coord._apply_device_state("ac", False)       # steht auf off → Skip
    await coord._apply_device_state("d1", True)        # steht schon → Skip
    with patch(_SLEEP, _breaking_sleep(3)):
        await _run_until_stopped(
            coord._hold_loop("d1", "switch.kessel", "", "switch", True,
                             ENTITY_CONTROL_HOLD_ALWAYS)
        )
    await coord._handle_ws_message({
        "type": "command", "device_id": "w1", "action": "set_charge_mode",
        "value": "An", "current_a": 20, "phases": 3,
    })
    await coord._charge_mode_lease_expiry("w1")        # SSE frisch → kein Write
    await coord._handle_ws_message({
        "type": "command", "device_id": "b1", "action": "set_charge_mode",
        "mode": "charge", "setpoint_kw": 4.0,
    })
    await coord._handle_ws_message({                   # steht auf Passiv → Skip
        "type": "command", "device_id": "b1", "action": "set_charge_mode",
        "mode": "passive", "setpoint_kw": 0.0,
    })
    # Breaker getrippt → der Write wird abgefangen.
    coord.state.entity_write_counts["select.wb_mode"] = (
        time.time(), WRITE_BREAKER_MAX_PER_HOUR,
    )
    await coord._apply_charge_mode("w1", "Solar", schedule_hold=False)
    return log, coord


async def test_counting_changes_no_service_call(hass: HomeAssistant):
    """Regressionsschutz: mit und ohne Zähler setzt derselbe Ablauf
    exakt dieselben Service-Calls in derselben Reihenfolge ab."""
    with_counting, coord = await _mixed_scenario(hass, counting=True)
    without_counting, plain = await _mixed_scenario(hass, counting=False)

    assert with_counting == without_counting
    assert len(with_counting) > 0
    assert plain.state.write_counts == {}
    # Jeder abgesetzte Call ist genau einmal gezählt — und nur der.
    assert _counted(coord) == len(with_counting)


async def test_mixed_scenario_counts_land_in_the_right_channels(
    hass: HomeAssistant,
):
    log, coord = await _mixed_scenario(hass, counting=True)

    assert {d: rec["channels"] for d, rec in coord.state.write_counts.items()} == {
        "ww": {"control": 1, "eco": 1},
        "ac": {"control": 1},
        "d1": {"control": 2},
        "w1": {"charge_phases": 1, "charge_current": 1, "charge_mode": 1},
        "b1": {"battery_setpoint": 1, "battery_mode": 1},
    }
    assert len(log) == 10


def test_every_service_call_site_is_counted():
    """Wächter für neue Schreibstellen: jeder `services.async_call` des
    Pakets steht in `command_dispatcher.py`, direkt hinter seinem
    `_count_write`."""
    package = Path(dispatcher_mod.__file__).parent
    sites = 0
    for path in sorted(package.glob("*.py")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if "services.async_call(" not in line or line.lstrip().startswith("#"):
                continue
            sites += 1
            assert path.name == "command_dispatcher.py", f"{path.name}:{i + 1}"
            assert "self._count_write(" in lines[i - 1], f"{path.name}:{i + 1}"
    assert sites == 15


async def test_a_broken_counter_never_costs_a_write(hass: HomeAssistant):
    coord = make_coordinator(hass, [_switch_device()])
    hass.states.async_set("switch.kessel", "off")
    mocks = _mock_all_services(hass)

    def _boom(*_a, **_k):
        raise RuntimeError("Zähler kaputt")

    coord.state.count_write = _boom
    coord.state.count_regulator_action = _boom

    await coord._apply_device_state("d1", True)
    coord._count_regulator_action("d1", "starts")

    assert len(mocks[("switch", "turn_on")]) == 1


async def test_a_broken_counter_never_costs_the_telemetry(hass: HomeAssistant):
    coord = make_coordinator(hass, [_switch_device()])
    hass.states.async_set("switch.kessel", "off")

    def _boom(*_a, **_k):
        raise RuntimeError("Zähler kaputt")

    coord.state.write_counts_snapshot = _boom

    await coord._async_update_data()

    payload = _sent_payloads(coord)[-1]
    assert payload["is_online"] is True
    assert "write_counts" not in payload
