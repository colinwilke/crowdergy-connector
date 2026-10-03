"""ECO-Sollwert mitschreiben (heating/warmwater).

WPs im Programmbetrieb (Stiebel ISG) halten je nach Zeitfenster den
Komfort- ODER den ECO-Sollwert und ignorieren den anderen. Ist der
optionale Slot `entity_control_eco` gemappt, geht jede Ziel-Temperatur,
die der Connector nach `entity_control` schreibt, auch dorthin. Diese
Datei pinnt:

* AN und AUS schreiben beide Entities (number + climate/water_heater).
* Eigener Clamp je Entity (min/max der ECO-Entity), eigener Vergleich
  gegen den geklemmten Wert → keine Dauer-Scheindrift.
* Idempotenz: stehen beide schon, gibt es keinen Service-Call.
* Circuit-Breaker der ECO-Entity wird respektiert.
* Ohne Remote-Control-Consent: keine Writes, auch nicht aus dem Hold.
* Hold-Loop: Echo-Drift nach eigenem Write wird repariert; Fremd-Drift
  im AUTO-Hold pausiert das Gerät (#140); ALWAYS schreibt den
  ECO-Sollwert nur bei Abweichung.
* Nur Temperaturen werden gespiegelt (Schalter/Modus-Strings nie), nur
  bei heating/warmwater.
* Config-Flow: Slot im Schema, `_build_device_record` persistiert ihn,
  Allowlist + Preset-Spec + Control-Slot-Liste kennen ihn.
"""
from __future__ import annotations

import time
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.theothergas.config_flow_mapping import _build_device_record
from custom_components.theothergas.config_flow_schemas import _entities_schema
from custom_components.theothergas.const import (
    CONF_DEVICE_ID,
    CONF_DEVICE_TYPE,
    CONF_ENTITY_CONTROL,
    CONF_ENTITY_CONTROL_ECO,
    CONF_ENTITY_CONTROL_HOLD,
    CONF_VALUE_OFF,
    CONF_VALUE_ON,
    DOMAIN,
    ENTITY_CONTROL_HOLD_ALWAYS,
    ENTITY_CONTROL_HOLD_AUTO,
    ENTITY_CONTROL_HOLD_NEVER,
    LOCAL_OVERRIDE_HOLD_S,
    MAPPABLE_ENTITY_DOMAINS,
    OPT_CONSENT_REMOTE_CONTROL,
    WRITE_BREAKER_MAX_PER_HOUR,
)
from custom_components.theothergas.coordinator import CrowdergyCoordinator
from custom_components.theothergas.entity_mapper import CONTROL_SLOT_KEYS
from custom_components.theothergas.preset_spec import (
    PRESET_SLOT_SPEC,
    extract_preset_maps,
)
from custom_components.theothergas.state_mirror import DeviceStateMirror

_SLEEP = "custom_components.theothergas.coordinator.asyncio.sleep"
ECO = "number.wp_eco_ww"
KOMFORT = "number.wp_komfort_ww"


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


def _ww_device(
    control: str = KOMFORT,
    eco: str = ECO,
    *,
    device_type: str = "warmwater",
    value_on: str = "55",
    value_off: str = "40",
    hold: str = ENTITY_CONTROL_HOLD_NEVER,
) -> dict:
    return {
        CONF_DEVICE_ID: "d1",
        CONF_DEVICE_TYPE: device_type,
        CONF_ENTITY_CONTROL: control,
        CONF_ENTITY_CONTROL_ECO: eco,
        CONF_VALUE_ON: value_on,
        CONF_VALUE_OFF: value_off,
        CONF_ENTITY_CONTROL_HOLD: hold,
    }


def _number(hass, entity_id: str, value: float, lo: float = 10, hi: float = 65):
    hass.states.async_set(entity_id, str(value), {"min": lo, "max": hi})


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


def _by_entity(calls) -> dict[str, list]:
    out: dict[str, list] = {}
    for c in calls:
        out.setdefault(c.data["entity_id"], []).append(c.data)
    return out


# ════════════════════════════════════════════════════════════════════
# A. Dispatch — beide Entities, AN und AUS
# ════════════════════════════════════════════════════════════════════


async def test_on_writes_both_komfort_and_eco(hass: HomeAssistant):
    coord = make_coordinator(hass, [_ww_device()])
    _number(hass, KOMFORT, 40.0)
    _number(hass, ECO, 40.0)
    calls = async_mock_service(hass, "number", "set_value")

    await coord._apply_device_state("d1", True)

    by = _by_entity(calls)
    assert by[KOMFORT] == [{"entity_id": KOMFORT, "value": 55.0}]
    assert by[ECO] == [{"entity_id": ECO, "value": 55.0}]


async def test_off_writes_both_komfort_and_eco(hass: HomeAssistant):
    coord = make_coordinator(hass, [_ww_device()])
    _number(hass, KOMFORT, 55.0)
    _number(hass, ECO, 49.5)
    calls = async_mock_service(hass, "number", "set_value")

    await coord._apply_device_state("d1", False)

    by = _by_entity(calls)
    assert by[KOMFORT] == [{"entity_id": KOMFORT, "value": 40.0}]
    assert by[ECO] == [{"entity_id": ECO, "value": 40.0}]


async def test_eco_on_water_heater_uses_set_temperature(hass: HomeAssistant):
    coord = make_coordinator(
        hass, [_ww_device("water_heater.ww", "water_heater.ww_eco")],
    )
    hass.states.async_set(
        "water_heater.ww", "eco", {"temperature": 40.0, "min_temp": 20, "max_temp": 65},
    )
    hass.states.async_set(
        "water_heater.ww_eco", "eco", {"temperature": 40.0, "min_temp": 20, "max_temp": 65},
    )
    temp_calls = async_mock_service(hass, "water_heater", "set_temperature")
    mode_calls = async_mock_service(hass, "water_heater", "set_operation_mode")

    await coord._apply_device_state("d1", True)

    by = _by_entity(temp_calls)
    assert by["water_heater.ww"] == [{"entity_id": "water_heater.ww", "temperature": 55.0}]
    assert by["water_heater.ww_eco"] == [
        {"entity_id": "water_heater.ww_eco", "temperature": 55.0}
    ]
    assert mode_calls == []


async def test_primary_already_set_still_syncs_eco(hass: HomeAssistant):
    """Idempotenz-Zweig der Primär-Entity darf den ECO-Sollwert nicht
    überspringen — genau der Feld-Fall: Komfort stand, ECO nicht."""
    coord = make_coordinator(hass, [_ww_device()])
    _number(hass, KOMFORT, 55.0)
    _number(hass, ECO, 49.5)
    calls = async_mock_service(hass, "number", "set_value")

    await coord._apply_device_state("d1", True)

    assert [c.data for c in calls] == [{"entity_id": ECO, "value": 55.0}]


# ════════════════════════════════════════════════════════════════════
# B. Clamp je Entity + Idempotenz gegen den geklemmten Wert
# ════════════════════════════════════════════════════════════════════


async def test_eco_clamped_to_own_limits(hass: HomeAssistant, caplog):
    coord = make_coordinator(hass, [_ww_device()])
    _number(hass, KOMFORT, 40.0, hi=65)
    _number(hass, ECO, 40.0, hi=50)  # ECO-Register erlaubt weniger
    calls = async_mock_service(hass, "number", "set_value")

    await coord._apply_device_state("d1", True)

    by = _by_entity(calls)
    assert by[KOMFORT] == [{"entity_id": KOMFORT, "value": 55.0}]
    assert by[ECO] == [{"entity_id": ECO, "value": 50.0}]
    assert "write clamp" in caplog.text and ECO in caplog.text
    # control_value_rejected gehört der Primär-Entity (nicht geklemmt).
    assert "d1" not in coord.state.value_rejected_devices
    # last_written_value beschreibt den Befehl an die Primär-Entity.
    assert coord.state.last_written_value["d1"] == "55.0"


async def test_no_rewrite_when_both_match_incl_clamped_eco(hass: HomeAssistant):
    coord = make_coordinator(hass, [_ww_device()])
    _number(hass, KOMFORT, 55.0)
    _number(hass, ECO, 50.0, hi=50)  # steht korrekt auf dem geklemmten Wert
    calls = async_mock_service(hass, "number", "set_value")

    await coord._apply_device_state("d1", True)

    assert calls == []


# ════════════════════════════════════════════════════════════════════
# C. Breaker + Consent
# ════════════════════════════════════════════════════════════════════


async def test_eco_breaker_respected(hass: HomeAssistant):
    coord = make_coordinator(hass, [_ww_device()])
    coord.state.entity_write_counts[ECO] = (
        time.time(), WRITE_BREAKER_MAX_PER_HOUR + 1,
    )
    _number(hass, KOMFORT, 40.0)
    _number(hass, ECO, 40.0)
    calls = async_mock_service(hass, "number", "set_value")

    await coord._apply_device_state("d1", True)

    assert [c.data["entity_id"] for c in calls] == [KOMFORT]
    assert "d1" in coord.state.write_breaker_devices


async def test_eco_write_stamps_own_write_clock(hass: HomeAssistant):
    coord = make_coordinator(hass, [_ww_device()])
    _number(hass, KOMFORT, 40.0)
    _number(hass, ECO, 40.0)
    async_mock_service(hass, "number", "set_value")
    before = time.time()

    await coord._apply_device_state("d1", True)

    assert coord.state.last_own_write_at[ECO] >= before
    assert coord.state.entity_write_counts[ECO][1] == 1


async def test_no_writes_without_remote_control_consent(hass: HomeAssistant):
    coord = make_coordinator(
        hass, [_ww_device()], options={OPT_CONSENT_REMOTE_CONTROL: False},
    )
    _number(hass, KOMFORT, 40.0)
    _number(hass, ECO, 40.0)
    calls = async_mock_service(hass, "number", "set_value")

    await coord._apply_device_state("d1", True)
    assert await coord._sync_eco_setpoint("d1", "55", True) is True

    assert calls == []


async def test_hold_loop_no_eco_write_without_consent(hass: HomeAssistant):
    coord = make_coordinator(
        hass, [_ww_device(hold=ENTITY_CONTROL_HOLD_AUTO)],
        options={OPT_CONSENT_REMOTE_CONTROL: False},
    )
    coord.state.active_state["d1"] = True
    coord.state.last_sse_event_at = time.time()
    _number(hass, KOMFORT, 55.0)
    _number(hass, ECO, 40.0)
    calls = async_mock_service(hass, "number", "set_value")

    with patch(_SLEEP, _breaking_sleep(3)):
        await _run_until_stopped(
            coord._hold_loop("d1", KOMFORT, "55", "number", True,
                             ENTITY_CONTROL_HOLD_AUTO)
        )

    assert [c.data["entity_id"] for c in calls if c.data["entity_id"] == ECO] == []
    assert "d1" not in coord.state.local_override_until


# ════════════════════════════════════════════════════════════════════
# D. Hold-Loop — Drift-Repair vs. Übersteuerung
# ════════════════════════════════════════════════════════════════════


async def test_hold_auto_repairs_eco_echo_after_own_write(hass: HomeAssistant):
    coord = make_coordinator(hass, [_ww_device(hold=ENTITY_CONTROL_HOLD_AUTO)])
    coord.state.active_state["d1"] = True
    coord.state.last_sse_event_at = time.time()
    coord.state.last_own_write_at[ECO] = time.time()
    _number(hass, KOMFORT, 55.0)
    _number(hass, ECO, 49.5)  # Register ist kurz nach unserem Write zurück
    calls = async_mock_service(hass, "number", "set_value")

    with patch(_SLEEP, _breaking_sleep(2)):
        await _run_until_stopped(
            coord._hold_loop("d1", KOMFORT, "55", "number", True,
                             ENTITY_CONTROL_HOLD_AUTO)
        )

    assert [c.data for c in calls] == [{"entity_id": ECO, "value": 55.0}]
    assert "d1" not in coord.state.local_override_until


async def test_hold_auto_foreign_eco_drift_pauses_device(hass: HomeAssistant):
    """Fremd-Drift am ECO-Sollwert ohne eigenen Write = Nutzer-Eingriff:
    kein Rewrite, Gerät pausiert (wie am Komfort-Sollwert)."""
    coord = make_coordinator(hass, [_ww_device(hold=ENTITY_CONTROL_HOLD_AUTO)])
    coord.state.active_state["d1"] = True
    coord.state.last_sse_event_at = time.time()
    _number(hass, KOMFORT, 55.0)
    _number(hass, ECO, 45.0)
    calls = async_mock_service(hass, "number", "set_value")

    with patch(_SLEEP, _breaking_sleep(8)):
        await _run_until_stopped(
            coord._hold_loop("d1", KOMFORT, "55", "number", True,
                             ENTITY_CONTROL_HOLD_AUTO)
        )

    assert calls == []
    until = coord.state.local_override_until.get("d1", 0.0)
    assert until > time.time() + LOCAL_OVERRIDE_HOLD_S - 60


async def test_hold_auto_own_write_never_flags_eco_override(hass: HomeAssistant):
    """Apply schreibt ECO → der folgende AUTO-Hold sieht den eigenen
    Write und stuft eine Echo-Abweichung nicht als Eingriff ein; stimmt
    alles, schreibt er gar nicht."""
    coord = make_coordinator(hass, [_ww_device(hold=ENTITY_CONTROL_HOLD_AUTO)])
    coord.state.active_state["d1"] = True
    coord.state.last_sse_event_at = time.time()
    _number(hass, KOMFORT, 55.0)
    _number(hass, ECO, 40.0)
    calls = async_mock_service(hass, "number", "set_value")
    with patch.object(coord, "_start_hold"):
        await coord._apply_device_state("d1", True)
    assert [c.data["entity_id"] for c in calls] == [ECO]
    _number(hass, ECO, 55.0)  # Register hat übernommen

    with patch(_SLEEP, _breaking_sleep(4)):
        await _run_until_stopped(
            coord._hold_loop("d1", KOMFORT, "55", "number", True,
                             ENTITY_CONTROL_HOLD_AUTO)
        )

    assert len(calls) == 1
    assert "d1" not in coord.state.local_override_until


async def test_hold_always_writes_eco_only_on_mismatch(hass: HomeAssistant):
    coord = make_coordinator(hass, [_ww_device(hold=ENTITY_CONTROL_HOLD_ALWAYS)])
    coord.state.active_state["d1"] = True
    coord.state.last_sse_event_at = time.time()
    _number(hass, KOMFORT, 55.0)
    _number(hass, ECO, 55.0)
    calls = async_mock_service(hass, "number", "set_value")

    with patch(_SLEEP, _breaking_sleep(3)):  # initial-delay + 2 Ticks
        await _run_until_stopped(
            coord._hold_loop("d1", KOMFORT, "55", "number", True,
                             ENTITY_CONTROL_HOLD_ALWAYS)
        )

    # ALWAYS: Komfort blind je Tick, ECO nie (stand schon).
    assert {c.data["entity_id"] for c in calls} == {KOMFORT}
    assert "d1" not in coord.state.local_override_until


# ════════════════════════════════════════════════════════════════════
# E. Scope — nur Temperaturen, nur heating/warmwater
# ════════════════════════════════════════════════════════════════════


async def test_switch_control_never_mirrors_to_eco(hass: HomeAssistant):
    coord = make_coordinator(
        hass, [_ww_device("switch.ww", value_on="", value_off="")],
    )
    hass.states.async_set("switch.ww", "off")
    _number(hass, ECO, 40.0)
    sw = async_mock_service(hass, "switch", "turn_on")
    num = async_mock_service(hass, "number", "set_value")

    await coord._apply_device_state("d1", True)

    assert len(sw) == 1
    assert num == []


async def test_mode_strings_never_mirror_to_eco(hass: HomeAssistant):
    coord = make_coordinator(
        hass, [_ww_device("select.sg_ready", value_on="Erhöht", value_off="Normal")],
    )
    hass.states.async_set("select.sg_ready", "Normal")
    _number(hass, ECO, 40.0)
    sel = async_mock_service(hass, "select", "select_option")
    num = async_mock_service(hass, "number", "set_value")

    await coord._apply_device_state("d1", True)

    assert len(sel) == 1
    assert num == []


async def test_other_types_ignore_eco_slot(hass: HomeAssistant):
    coord = make_coordinator(hass, [_ww_device(device_type="generic")])
    _number(hass, KOMFORT, 40.0)
    _number(hass, ECO, 40.0)
    calls = async_mock_service(hass, "number", "set_value")

    await coord._apply_device_state("d1", True)

    assert [c.data["entity_id"] for c in calls] == [KOMFORT]


async def test_without_eco_mapping_behaviour_unchanged(hass: HomeAssistant):
    coord = make_coordinator(hass, [_ww_device(eco="")])
    _number(hass, KOMFORT, 40.0)
    calls = async_mock_service(hass, "number", "set_value")

    await coord._apply_device_state("d1", True)

    assert [c.data for c in calls] == [{"entity_id": KOMFORT, "value": 55.0}]


# ════════════════════════════════════════════════════════════════════
# F. Config-Flow / SSOT
# ════════════════════════════════════════════════════════════════════


def test_build_device_record_persists_eco_slot():
    record = _build_device_record(
        "dev-1", "warmwater", "Warmwasser",
        {CONF_ENTITY_CONTROL: KOMFORT, CONF_ENTITY_CONTROL_ECO: ECO},
    )
    assert record[CONF_ENTITY_CONTROL_ECO] == ECO


def test_build_device_record_eco_default_empty():
    record = _build_device_record("dev-1", "heating", "Heizung", {})
    assert record[CONF_ENTITY_CONTROL_ECO] == ""


def _control_keys(schema) -> set[str]:
    for key, value in schema.schema.items():
        if str(key) == "control_section":
            return {str(k) for k in value.schema.schema}
    return set()


def test_schema_offers_eco_only_for_heating_and_warmwater():
    for dtype in ("heating", "warmwater"):
        for mode in ("manual", "climate"):
            assert CONF_ENTITY_CONTROL_ECO in _control_keys(
                _entities_schema(dtype, {}, mode)
            ), (dtype, mode)
    for dtype in ("aircon", "generic", "battery", "wallbox"):
        assert CONF_ENTITY_CONTROL_ECO not in _control_keys(
            _entities_schema(dtype, {})
        ), dtype


def test_eco_slot_is_allowlisted_writable_control_slot():
    assert MAPPABLE_ENTITY_DOMAINS[CONF_ENTITY_CONTROL_ECO] == frozenset(
        {"number", "input_number", "climate", "water_heater"}
    )
    # Steuer-Slot: der Preset-Prefill rät hier nie (#300).
    assert CONF_ENTITY_CONTROL_ECO in CONTROL_SLOT_KEYS


def test_preset_spec_carries_eco_slot_for_thermal_types_only():
    for dtype, slots in PRESET_SLOT_SPEC.items():
        keys = {s.key for s in slots}
        assert (CONF_ENTITY_CONTROL_ECO in keys) == (
            dtype in ("heating", "warmwater")
        ), dtype
    entity_map, _ = extract_preset_maps(
        {CONF_DEVICE_TYPE: "warmwater", CONF_ENTITY_CONTROL: KOMFORT,
         CONF_ENTITY_CONTROL_ECO: ECO},
    )
    assert entity_map[CONF_ENTITY_CONTROL_ECO] == ECO
