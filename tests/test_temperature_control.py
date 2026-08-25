"""Temperatur-Modus für Wärmepumpen (heating / warmwater, 2026-07-02).

User-Vorgabe: WPs werden NIE hart an/aus geschaltet — die gemappten
value_on / value_off sind ZIEL-Temperaturen (AN = Maximal-, AUS =
Minimaltemperatur), geschrieben via `set_temperature`. Die WP regelt
ihre Laufzeit selbst. Diese Datei pinnt:

* Dispatch: numerische Werte auf climate/water_heater → set_temperature,
  NIE set_hvac_mode / set_operation_mode (inkl. AUS-Pfad!).
* Legacy: nicht-numerische Werte ("heat"/"off") → alter Modus-Pfad.
* Idempotenz: Ziel-Temperatur steht schon → kein Service-Call.
* Hold-Loop (AUTO): vergleicht das `temperature`-Attribut, repariert
  Drift per set_temperature-Rewrite.
* Read-back `_read_is_on_state`: Max-Temp = AN, Min-Temp = AUS,
  fremder Wert = None (Backend behält den letzten Zustand).
* Config-Flow: heating/warmwater + climate/water_heater bekommen im
  Values-Step °C-NumberSelector-Felder statt HVAC-Modus-Dropdowns.
"""
from __future__ import annotations

import time
from unittest.mock import patch

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.helpers import selector
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.theothergas.config_flow_schemas import _values_schema
from custom_components.theothergas.const import (
    CONF_DEVICE_ID,
    CONF_DEVICE_TYPE,
    CONF_ENTITY_CONTROL,
    CONF_ENTITY_CONTROL_HOLD,
    CONF_VALUE_OFF,
    CONF_VALUE_ON,
    DOMAIN,
    ENTITY_CONTROL_HOLD_AUTO,
    ENTITY_CONTROL_HOLD_NEVER,
    is_temperature_control,
    temperature_control_value,
)
from custom_components.theothergas.coordinator import CrowdergyCoordinator
from custom_components.theothergas.state_mirror import DeviceStateMirror

_SLEEP = "custom_components.theothergas.coordinator.asyncio.sleep"


def make_coordinator(
    hass: HomeAssistant,
    devices: list[dict],
    *,
    options: dict | None = None,
) -> CrowdergyCoordinator:
    """Coordinator ohne DataUpdateCoordinator-Init — gleiches Muster wie
    test_hold_loops_and_eviction.make_coordinator."""
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


def _wp_device(
    entity: str = "climate.wp",
    *,
    device_type: str = "heating",
    value_on: str = "50",
    value_off: str = "35",
    hold: str = ENTITY_CONTROL_HOLD_NEVER,
) -> dict:
    return {
        CONF_DEVICE_ID: "d1",
        CONF_DEVICE_TYPE: device_type,
        CONF_ENTITY_CONTROL: entity,
        CONF_VALUE_ON: value_on,
        CONF_VALUE_OFF: value_off,
        CONF_ENTITY_CONTROL_HOLD: hold,
    }


class _StopHold(BaseException):
    """Bricht die endlose Hold-Loop deterministisch (BaseException, damit
    der `except Exception`-Crash-Handler sie nicht schluckt)."""


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


# ════════════════════════════════════════════════════════════════════
# A. Helper-Semantik
# ════════════════════════════════════════════════════════════════════


def test_temperature_control_value_parses_numbers_only():
    assert temperature_control_value("50") == 50.0
    assert temperature_control_value(47.5) == 47.5
    assert temperature_control_value("heat") is None
    assert temperature_control_value("") is None
    assert temperature_control_value(None) is None
    assert temperature_control_value(True) is None


def test_is_temperature_control_requires_thermal_domain():
    assert is_temperature_control("climate", "50")
    assert is_temperature_control("water_heater", "45.5")
    assert not is_temperature_control("climate", "heat")
    assert not is_temperature_control("switch", "50")
    assert not is_temperature_control("number", "50")  # eigener set_value-Pfad


# ════════════════════════════════════════════════════════════════════
# B. Dispatch — set_temperature statt harter Modus-Writes
# ════════════════════════════════════════════════════════════════════


async def test_heating_on_writes_max_temperature_not_hvac_mode(hass: HomeAssistant):
    coord = make_coordinator(hass, [_wp_device()])
    hass.states.async_set("climate.wp", "heat", {"temperature": 35.0})
    temp_calls = async_mock_service(hass, "climate", "set_temperature")
    mode_calls = async_mock_service(hass, "climate", "set_hvac_mode")

    await coord._apply_device_state("d1", True)

    assert len(temp_calls) == 1
    assert temp_calls[0].data == {"entity_id": "climate.wp", "temperature": 50.0}
    assert mode_calls == []


async def test_heating_off_writes_min_temperature_never_hvac_off(hass: HomeAssistant):
    """Der Kern der User-Vorgabe: AUS = Minimaltemperatur, KEIN hartes
    set_hvac_mode("off") — die WP entscheidet selbst, wie lange sie läuft."""
    coord = make_coordinator(hass, [_wp_device()])
    hass.states.async_set("climate.wp", "heat", {"temperature": 50.0})
    temp_calls = async_mock_service(hass, "climate", "set_temperature")
    mode_calls = async_mock_service(hass, "climate", "set_hvac_mode")

    await coord._apply_device_state("d1", False)

    assert len(temp_calls) == 1
    assert temp_calls[0].data == {"entity_id": "climate.wp", "temperature": 35.0}
    assert mode_calls == []


async def test_warmwater_water_heater_temperature_mode(hass: HomeAssistant):
    coord = make_coordinator(
        hass,
        [_wp_device("water_heater.ww", device_type="warmwater",
                    value_on="55", value_off="45")],
    )
    hass.states.async_set("water_heater.ww", "eco", {"temperature": 45.0})
    temp_calls = async_mock_service(hass, "water_heater", "set_temperature")
    mode_calls = async_mock_service(hass, "water_heater", "set_operation_mode")

    await coord._apply_device_state("d1", True)

    assert len(temp_calls) == 1
    assert temp_calls[0].data == {"entity_id": "water_heater.ww", "temperature": 55.0}
    assert mode_calls == []


async def test_climate_legacy_mode_strings_still_write_hvac_mode(hass: HomeAssistant):
    """Nicht-numerische Werte = Legacy-Mapping — der alte Modus-Pfad
    bleibt für Bestands-Configs/andere Typen unverändert."""
    coord = make_coordinator(
        hass, [_wp_device(value_on="heat", value_off="off")]
    )
    hass.states.async_set("climate.wp", "off", {"temperature": 40.0})
    temp_calls = async_mock_service(hass, "climate", "set_temperature")
    mode_calls = async_mock_service(hass, "climate", "set_hvac_mode")

    await coord._apply_device_state("d1", True)

    assert temp_calls == []
    assert len(mode_calls) == 1
    assert mode_calls[0].data == {"entity_id": "climate.wp", "hvac_mode": "heat"}


async def test_apply_skips_when_target_temperature_already_set(hass: HomeAssistant):
    """Idempotenz-Guard liest im Temperatur-Modus das `temperature`-
    Attribut (nicht state.state = "heat") — Ziel steht schon → kein Call."""
    coord = make_coordinator(hass, [_wp_device()])
    hass.states.async_set("climate.wp", "heat", {"temperature": 50.0})
    temp_calls = async_mock_service(hass, "climate", "set_temperature")

    await coord._apply_device_state("d1", True)

    assert temp_calls == []


# ════════════════════════════════════════════════════════════════════
# C. Hold-Loop — Drift-Repair auf dem Temperatur-Attribut
# ════════════════════════════════════════════════════════════════════


async def test_hold_loop_auto_skips_when_temperature_matches(hass: HomeAssistant):
    coord = make_coordinator(hass, [_wp_device(hold=ENTITY_CONTROL_HOLD_AUTO)])
    coord.state.active_state["d1"] = True
    coord.state.last_sse_event_at = time.time()
    hass.states.async_set("climate.wp", "heat", {"temperature": 50.0})
    temp_calls = async_mock_service(hass, "climate", "set_temperature")

    with patch(_SLEEP, _breaking_sleep(3)):
        await _run_until_stopped(
            coord._hold_loop("d1", "climate.wp", "50", "climate", True,
                             ENTITY_CONTROL_HOLD_AUTO)
        )

    assert temp_calls == []


async def test_hold_loop_auto_repairs_temperature_drift(hass: HomeAssistant):
    """WP setzt die Ziel-Temperatur kurz nach unserem eigenen Write
    zurück (Echo/Revert im #140-Grace-Fenster) → AUTO-Hold schreibt die
    kommandierte Temperatur nach (set_temperature, nie Modus). Fremder
    Drift OHNE eigenen Write im Fenster ist seit #140 ein
    Nutzer-Eingriff und pausiert stattdessen (test_safety_bundle.py)."""
    coord = make_coordinator(hass, [_wp_device(hold=ENTITY_CONTROL_HOLD_AUTO)])
    coord.state.active_state["d1"] = True
    coord.state.last_sse_event_at = time.time()
    coord.state.last_own_write_at["climate.wp"] = time.time()
    hass.states.async_set("climate.wp", "heat", {"temperature": 42.0})
    temp_calls = async_mock_service(hass, "climate", "set_temperature")
    mode_calls = async_mock_service(hass, "climate", "set_hvac_mode")

    with patch(_SLEEP, _breaking_sleep(2)):  # initial-delay + 1 Tick
        await _run_until_stopped(
            coord._hold_loop("d1", "climate.wp", "50", "climate", True,
                             ENTITY_CONTROL_HOLD_AUTO)
        )

    assert len(temp_calls) == 1
    assert temp_calls[0].data == {"entity_id": "climate.wp", "temperature": 50.0}
    assert mode_calls == []


# ════════════════════════════════════════════════════════════════════
# D. Read-back — is_on aus der Ziel-Temperatur
# ════════════════════════════════════════════════════════════════════


async def test_read_is_on_state_temperature_mode(hass: HomeAssistant):
    coord = make_coordinator(hass, [_wp_device()])
    dev = coord.devices[0]

    hass.states.async_set("climate.wp", "heat", {"temperature": 50.0})
    assert coord._read_is_on_state(dev) is True

    hass.states.async_set("climate.wp", "heat", {"temperature": 35.0})
    assert coord._read_is_on_state(dev) is False

    # User hat manuell eine dritte Temperatur gesetzt → unentscheidbar,
    # Backend behält den letzten Zustand.
    hass.states.async_set("climate.wp", "heat", {"temperature": 42.0})
    assert coord._read_is_on_state(dev) is None

    # Kein temperature-Attribut → None (kein Raten aus dem hvac-Modus).
    hass.states.async_set("climate.wp", "heat", {})
    assert coord._read_is_on_state(dev) is None


async def test_read_is_on_state_legacy_climate_unchanged(hass: HomeAssistant):
    coord = make_coordinator(
        hass, [_wp_device(value_on="heat", value_off="off")]
    )
    dev = coord.devices[0]
    hass.states.async_set("climate.wp", "heat", {"temperature": 42.0})
    assert coord._read_is_on_state(dev) is True
    hass.states.async_set("climate.wp", "off", {"temperature": 42.0})
    assert coord._read_is_on_state(dev) is False


# ════════════════════════════════════════════════════════════════════
# E. Config-Flow — °C-Felder statt HVAC-Dropdown für WPs
# ════════════════════════════════════════════════════════════════════


def _schema_field_types(schema: vol.Schema) -> dict[str, object]:
    return {key.schema: validator for key, validator in schema.schema.items()}


async def test_values_schema_heating_climate_offers_temperature_fields(
    hass: HomeAssistant,
):
    hass.states.async_set(
        "climate.wp", "heat",
        {"hvac_modes": ["heat", "off"], "min_temp": 20.0, "max_temp": 60.0,
         "target_temp_step": 0.5},
    )
    schema = _values_schema(hass, "climate.wp", {}, device_type="heating")
    fields = _schema_field_types(schema)
    assert isinstance(fields[CONF_VALUE_ON], selector.NumberSelector)
    assert isinstance(fields[CONF_VALUE_OFF], selector.NumberSelector)
    cfg = fields[CONF_VALUE_ON].config
    assert cfg["min"] == 20.0
    assert cfg["max"] == 60.0


async def test_values_schema_heating_cooling_value_keeps_mode_dropdown(
    hass: HomeAssistant,
):
    """value_cool_on bleibt ein Modus-Mapping (climate.set_hvac_mode('cool'))
    — nur AN/AUS wandern auf Temperaturen."""
    hass.states.async_set(
        "climate.wp", "heat", {"hvac_modes": ["heat", "cool", "off"]}
    )
    schema = _values_schema(
        hass, "climate.wp", {}, include_cooling=True, device_type="heating"
    )
    fields = _schema_field_types(schema)
    assert isinstance(fields[CONF_VALUE_ON], selector.NumberSelector)
    assert isinstance(fields["value_cool_on"], selector.SelectSelector)


async def test_values_schema_aircon_keeps_mode_dropdowns(hass: HomeAssistant):
    """aircon bleibt Modus-basiert (Kühlen = invertierte Temperatur-
    Semantik, bewusst NICHT im Temperatur-Modus)."""
    hass.states.async_set(
        "climate.ac", "cool", {"hvac_modes": ["heat", "cool", "off"]}
    )
    schema = _values_schema(
        hass, "climate.ac", {}, include_cooling=True, cooling_first=True,
        device_type="aircon",
    )
    fields = _schema_field_types(schema)
    assert isinstance(fields[CONF_VALUE_ON], selector.SelectSelector)
    assert isinstance(fields[CONF_VALUE_OFF], selector.SelectSelector)
