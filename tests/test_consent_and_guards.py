"""Regression-Tests für die Review-Fixes 2026-06-11 (Coordinator-Seite).

* CN-1: Remote-Control-Consent-Gate zentral in den `_apply_*`-Methoden —
  deckt damit auch Resync-Loop und Hold-Self-Heal ab, nicht nur den
  SSE-Dispatch.
* CN-3: Hold-Self-Heal respektiert die SSE-Staleness (gleicher
  Threshold wie der `_hold_loop`-Bail) — kein Überschreiben von
  User-Eingriffen im Backend-Outage.
* CN-4: Idempotenz-Guard im Raum-Sollwert-Pfad (#341) liest bei
  climate.* das `temperature`-Attribut statt des HVAC-Mode-Strings.
* CN-6: number-Domains werden in der Hold-/Expected-Logik numerisch
  verglichen ("60" == "60.0").

Die Tests bauen den Coordinator über `__new__` + manuelles Attribut-
Seeding auf — `CrowdergyCoordinator.__init__` zieht die volle
DataUpdateCoordinator-Maschinerie hoch, die für diese reine Apply-/
Guard-Logik nicht gebraucht wird.
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.theothergas.const import (
    CONF_DEVICE_ID,
    CONF_DEVICE_TYPE,
    CONF_ENTITY_BATTERY_MODE,
    CONF_ENTITY_BATTERY_POWER_SETPOINT,
    CONF_ENTITY_CHARGE_MODE,
    CONF_ENTITY_CONTROL,
    CONF_ENTITY_CONTROL_HOLD,
    CONF_ENTITY_COOL_CONTROL,
    CONF_VALUE_BATTERY_MODE_ACTIVE,
    CONF_VALUE_BATTERY_MODE_PASSIVE,
    CONF_VALUE_COOL_ON,
    CONF_VALUE_OFF,
    CONF_VALUE_ON,
    DOMAIN,
    ENTITY_CONTROL_HOLD_NEVER,
    OPT_CONSENT_REMOTE_CONTROL,
    SSE_STALE_THRESHOLD_S,
)
from custom_components.theothergas.coordinator import CrowdergyCoordinator
from custom_components.theothergas.state_mirror import DeviceStateMirror


def make_coordinator(
    hass: HomeAssistant,
    devices: list[dict],
    *,
    options: dict | None = None,
) -> CrowdergyCoordinator:
    """Coordinator ohne DataUpdateCoordinator-Init — nur die Attribute,
    die die `_apply_*`-/Guard-Pfade tatsächlich lesen."""
    entry = MockConfigEntry(
        domain=DOMAIN, data={}, options=dict(options or {})
    )
    entry.add_to_hass(hass)
    coord = CrowdergyCoordinator.__new__(CrowdergyCoordinator)
    coord.hass = hass
    coord.entry = entry
    coord.devices = devices
    coord.state = DeviceStateMirror()
    coord._consent_denied_logged = set()
    coord._backend_gone_device_ids = set()  # v3.26.0: 404/410-Gate (siehe __init__)
    coord._last_sent_payload = {}
    coord._last_send_at = {}
    coord._last_mirror_at = {}
    coord._last_sent_hash = {}
    coord._prev_energy_kwh = {}
    coord._prev_energy_kwh_discharged = {}
    return coord


NO_CONSENT = {OPT_CONSENT_REMOTE_CONTROL: False}


# ── CN-1: zentrales Consent-Gate in allen _apply_*-Methoden ──────────


async def test_apply_device_state_blocked_without_consent(hass: HomeAssistant):
    dev = {
        CONF_DEVICE_ID: "d1",
        CONF_DEVICE_TYPE: "heating",
        CONF_ENTITY_CONTROL: "switch.kessel",
        CONF_ENTITY_CONTROL_HOLD: ENTITY_CONTROL_HOLD_NEVER,
    }
    coord = make_coordinator(hass, [dev], options=NO_CONSENT)
    hass.states.async_set("switch.kessel", "off")
    on_calls = async_mock_service(hass, "switch", "turn_on")

    await coord._apply_device_state("d1", True)

    assert on_calls == []
    assert coord.state.hold_tasks == {}


async def test_apply_device_state_writes_with_consent(hass: HomeAssistant):
    """Sanity-Gegentest: Default (Flag fehlt) bleibt voll funktional."""
    dev = {
        CONF_DEVICE_ID: "d1",
        CONF_DEVICE_TYPE: "heating",
        CONF_ENTITY_CONTROL: "switch.kessel",
        CONF_ENTITY_CONTROL_HOLD: ENTITY_CONTROL_HOLD_NEVER,
    }
    coord = make_coordinator(hass, [dev])
    hass.states.async_set("switch.kessel", "off")
    on_calls = async_mock_service(hass, "switch", "turn_on")

    await coord._apply_device_state("d1", True)

    assert len(on_calls) == 1


async def test_apply_cool_state_blocked_without_consent(hass: HomeAssistant):
    dev = {
        CONF_DEVICE_ID: "d1",
        CONF_DEVICE_TYPE: "heating",
        CONF_ENTITY_COOL_CONTROL: "switch.sg_cool",
        CONF_VALUE_COOL_ON: "",
    }
    coord = make_coordinator(hass, [dev], options=NO_CONSENT)
    hass.states.async_set("switch.sg_cool", "off")
    on_calls = async_mock_service(hass, "switch", "turn_on")

    await coord._apply_cool_state("d1", True)

    assert on_calls == []


async def test_apply_charge_mode_blocked_without_consent(hass: HomeAssistant):
    dev = {
        CONF_DEVICE_ID: "d1",
        CONF_DEVICE_TYPE: "wallbox",
        CONF_ENTITY_CHARGE_MODE: "select.lademodus",
    }
    coord = make_coordinator(hass, [dev], options=NO_CONSENT)
    hass.states.async_set("select.lademodus", "Eco")
    calls = async_mock_service(hass, "select", "select_option")

    await coord._apply_charge_mode("d1", "Power Mode")

    assert calls == []
    # Gate sitzt VOR dem Hold-Start — kein gehaltener Wert, kein Task.
    assert coord.state.held_charge_mode == {}
    assert coord.state.charge_mode_hold_tasks == {}


async def test_apply_battery_setpoint_blocked_without_consent(hass: HomeAssistant):
    dev = {
        CONF_DEVICE_ID: "d1",
        CONF_DEVICE_TYPE: "battery",
        CONF_ENTITY_BATTERY_MODE: "select.bat_mode",
        CONF_VALUE_BATTERY_MODE_ACTIVE: "Aktiv",
        CONF_VALUE_BATTERY_MODE_PASSIVE: "Passiv",
        CONF_ENTITY_BATTERY_POWER_SETPOINT: "number.bat_setpoint",
    }
    coord = make_coordinator(hass, [dev], options=NO_CONSENT)
    hass.states.async_set("select.bat_mode", "Passiv")
    hass.states.async_set("number.bat_setpoint", "0.0")
    select_calls = async_mock_service(hass, "select", "select_option")
    number_calls = async_mock_service(hass, "number", "set_value")

    await coord._apply_battery_setpoint("d1", "charge", 5.0)

    assert select_calls == []
    assert number_calls == []


async def test_apply_room_setpoint_blocked_without_consent(hass: HomeAssistant):
    dev = {
        CONF_DEVICE_ID: "d1",
        CONF_DEVICE_TYPE: "heating",
        CONF_ENTITY_CONTROL: "climate.wp",
        CONF_VALUE_ON: "30",
        CONF_VALUE_OFF: "10",
    }
    coord = make_coordinator(hass, [dev], options=NO_CONSENT)
    coord.state.on_state["d1"] = True
    hass.states.async_set("climate.wp", "heat", {"temperature": 21.0})
    calls = async_mock_service(hass, "climate", "set_temperature")

    await coord._apply_room_setpoint("d1", 23.0)

    assert calls == []


# ── CN-3: Self-Heal respektiert SSE-Staleness ────────────────────────


async def test_self_heal_skips_when_sse_stale(hass: HomeAssistant):
    dev = {
        CONF_DEVICE_ID: "d1",
        CONF_DEVICE_TYPE: "warmwater",
        CONF_ENTITY_CONTROL: "switch.ww",
    }
    coord = make_coordinator(hass, [dev])
    coord.state.active_state["d1"] = True
    coord.state.on_state["d1"] = True
    coord.state.last_sse_event_at = time.time() - SSE_STALE_THRESHOLD_S - 30
    coord._apply_device_state = AsyncMock()

    await coord._self_heal_holds(["d1"])

    coord._apply_device_state.assert_not_awaited()


async def test_self_heal_restarts_hold_when_sse_fresh(hass: HomeAssistant):
    dev = {
        CONF_DEVICE_ID: "d1",
        CONF_DEVICE_TYPE: "warmwater",
        CONF_ENTITY_CONTROL: "switch.ww",
    }
    coord = make_coordinator(hass, [dev])
    coord.state.active_state["d1"] = True
    coord.state.on_state["d1"] = True
    coord.state.last_sse_event_at = time.time() - 1
    coord._apply_device_state = AsyncMock()

    await coord._self_heal_holds(["d1"])

    coord._apply_device_state.assert_awaited_once_with("d1", True)


async def test_self_heal_skips_without_consent(hass: HomeAssistant):
    """CN-1-Seite des Self-Heals: ohne Consent kein Re-Apply, auch
    bei frischem SSE."""
    dev = {
        CONF_DEVICE_ID: "d1",
        CONF_DEVICE_TYPE: "warmwater",
        CONF_ENTITY_CONTROL: "switch.ww",
    }
    coord = make_coordinator(hass, [dev], options=NO_CONSENT)
    coord.state.active_state["d1"] = True
    coord.state.on_state["d1"] = True
    coord.state.last_sse_event_at = time.time() - 1
    coord._apply_device_state = AsyncMock()

    await coord._self_heal_holds(["d1"])

    coord._apply_device_state.assert_not_awaited()


# ── CN-4: climate-Idempotenz über das temperature-Attribut ───────────


async def test_room_setpoint_climate_same_setpoint_skips_write(hass: HomeAssistant):
    dev = {
        CONF_DEVICE_ID: "d1",
        CONF_DEVICE_TYPE: "heating",
        CONF_ENTITY_CONTROL: "climate.wp",
        CONF_VALUE_ON: "30",
        CONF_VALUE_OFF: "10",
    }
    coord = make_coordinator(hass, [dev])
    coord.state.active_state["d1"] = True
    coord.state.on_state["d1"] = True
    # climate.* trägt im state den HVAC-Mode; der Setpoint sitzt im
    # Attribut `temperature` — exakt der Standardfall.
    hass.states.async_set("climate.wp", "heat", {"temperature": 22.5})
    calls = async_mock_service(hass, "climate", "set_temperature")

    await coord._apply_room_setpoint("d1", 22.5)
    coord._cancel_hold("d1")

    assert calls == []


async def test_room_setpoint_climate_changed_setpoint_writes(hass: HomeAssistant):
    dev = {
        CONF_DEVICE_ID: "d1",
        CONF_DEVICE_TYPE: "heating",
        CONF_ENTITY_CONTROL: "climate.wp",
        CONF_VALUE_ON: "30",
        CONF_VALUE_OFF: "10",
    }
    coord = make_coordinator(hass, [dev])
    coord.state.active_state["d1"] = True
    coord.state.on_state["d1"] = True
    hass.states.async_set("climate.wp", "heat", {"temperature": 22.5})
    calls = async_mock_service(hass, "climate", "set_temperature")

    await coord._apply_room_setpoint("d1", 24.0)
    coord._cancel_hold("d1")

    assert len(calls) == 1
    assert calls[0].data["temperature"] == 24.0


# ── CN-6: numerischer Vergleich für number-Domains ───────────────────


def test_states_match_number_semantics():
    m = CrowdergyCoordinator._states_match
    assert m("60.0", "60", "number") is True
    assert m("60", "60.0", "input_number") is True
    assert m("60.5", "60", "number") is False
    assert m("on", "on", "switch") is True
    assert m("heat", "off", "climate") is False
    assert m(None, "60", "number") is False
    assert m("60", None, "number") is False
    # Nicht-numerische Werte in number-Domain fallen sauber auf
    # String-Vergleich zurück.
    assert m("abc", "abc", "number") is True


async def test_apply_device_state_number_idempotent(hass: HomeAssistant):
    """expected "60" vs HA-State "60.0" → kein Write (vorher schrieb
    der Pfad bei jedem Apply, der AUTO-Hold alle 30 s)."""
    dev = {
        CONF_DEVICE_ID: "d1",
        CONF_DEVICE_TYPE: "heating",
        CONF_ENTITY_CONTROL: "number.sg_ready",
        CONF_VALUE_ON: "60",
        CONF_VALUE_OFF: "0",
        CONF_ENTITY_CONTROL_HOLD: ENTITY_CONTROL_HOLD_NEVER,
    }
    coord = make_coordinator(hass, [dev])
    hass.states.async_set("number.sg_ready", "60.0")
    calls = async_mock_service(hass, "number", "set_value")

    await coord._apply_device_state("d1", True)

    assert calls == []


async def test_apply_device_state_number_drift_writes(hass: HomeAssistant):
    dev = {
        CONF_DEVICE_ID: "d1",
        CONF_DEVICE_TYPE: "heating",
        CONF_ENTITY_CONTROL: "number.sg_ready",
        CONF_VALUE_ON: "60",
        CONF_VALUE_OFF: "0",
        CONF_ENTITY_CONTROL_HOLD: ENTITY_CONTROL_HOLD_NEVER,
    }
    coord = make_coordinator(hass, [dev])
    hass.states.async_set("number.sg_ready", "53.0")
    calls = async_mock_service(hass, "number", "set_value")

    await coord._apply_device_state("d1", True)

    assert len(calls) == 1
    assert calls[0].data["value"] == 60.0


# ── AI-off via HA switch + Vorlauf gate (#350) ────────────────────────


def _battery_dev() -> dict:
    return {
        CONF_DEVICE_ID: "bat",
        CONF_DEVICE_TYPE: "battery",
        CONF_ENTITY_BATTERY_MODE: "select.bat_mode",
        CONF_ENTITY_BATTERY_POWER_SETPOINT: "number.bat_setpoint",
        CONF_VALUE_BATTERY_MODE_ACTIVE: "Aktiv",
        CONF_VALUE_BATTERY_MODE_PASSIVE: "Passiv",
    }


async def test_ha_switch_ai_off_runs_handover_once(hass: HomeAssistant):
    """Switching AI off from the HA switch must run the AI-off handover
    (battery → passive). The switch used to set the cache optimistically,
    so the backend's echo frame saw no change and the handover never ran;
    the echo must not run it a second time either."""
    from unittest.mock import MagicMock

    from custom_components.theothergas.switch import CrowdergyActiveSwitch

    coord = make_coordinator(hass, [_battery_dev()])
    coord.data = None
    coord.state.active_state["bat"] = True
    coord.async_post_command = AsyncMock(return_value=True)
    hass.states.async_set("select.bat_mode", "Aktiv")
    modes = async_mock_service(hass, "select", "select_option")

    switch = CrowdergyActiveSwitch(coord, _battery_dev(), MagicMock())
    switch.async_write_ha_state = MagicMock()
    await switch.async_turn_off()

    assert coord.state.active_state["bat"] is False
    assert [c.data["option"] for c in modes] == ["Passiv"]

    # Backend echo of the toggle: no second handover.
    await coord._handle_ws_message({
        "type": "telemetry", "device_id": "bat", "data": {"is_active": False},
    })
    assert len(modes) == 1


async def test_room_setpoint_not_written_with_ai_off(hass: HomeAssistant):
    dev = {
        CONF_DEVICE_ID: "hz",
        CONF_DEVICE_TYPE: "heating",
        CONF_ENTITY_CONTROL: "climate.wp",
        CONF_VALUE_ON: "30",
        CONF_VALUE_OFF: "10",
    }
    coord = make_coordinator(hass, [dev])
    coord.state.on_state["hz"] = True
    hass.states.async_set(
        "climate.wp", "heat", {"temperature": 10, "min_temp": 5, "max_temp": 30},
    )
    calls = async_mock_service(hass, "climate", "set_temperature")

    coord.state.active_state["hz"] = False
    await coord._apply_room_setpoint("hz", 23.0)
    assert calls == []

    coord.state.active_state["hz"] = True
    await coord._apply_room_setpoint("hz", 23.0)
    coord._cancel_hold("hz")
    assert [c.data["temperature"] for c in calls] == [23.0]
