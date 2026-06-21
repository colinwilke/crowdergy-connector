"""Tests für die Hausverbrauchs-Flow-Sensoren (#42, 2026-06-15).

Drei Pflicht-Sensoren (HC-from-PV / -Battery / -Grid) + ein optionaler
4. Sensor (PV→Batterie-Ladeleistung) fließen über die
`telemetry.extra`-Pipeline als kW mit und speisen den Backend-
Hausverbrauchs-Chart (Backend #41 `GET /users/me/energy/today`).

Diese Tests sperren:
  1. den Registry-Vertrag (Felder in `_SOLVER_EXTRA_FIELDS`,
     `_READ_FIELDS`, `_ENTITY_SELECTORS`, `MAPPABLE_ENTITY_DOMAINS`),
  2. die Connector↔Backend-Key-Naht (die extra-Bag-Keys müssen 1:1 die
     vom Backend erwarteten `*_power_kw`-Keys sein),
  3. das tatsächliche Auslesen + W→kW-Normalisieren über `_compose_extra`.
"""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.theothergas.config_flow import (
    _ENTITY_SELECTORS,
    _READ_FIELDS,
    _build_device_record,
)
from custom_components.theothergas.const import (
    CONF_DEVICE_ID,
    CONF_DEVICE_TYPE,
    CONF_ENTITY_HC_BATTERY_POWER,
    CONF_ENTITY_HC_GRID_POWER,
    CONF_ENTITY_HC_PV_POWER,
    CONF_ENTITY_PV_TO_BATTERY_POWER,
    CONF_ENTITY_WALLBOX_CHARGE_CURRENT,
    DOMAIN,
    MAPPABLE_ENTITY_DOMAINS,
)
from custom_components.theothergas.coordinator import (
    _SOLVER_EXTRA_FIELDS,
    CrowdergyCoordinator,
)
from custom_components.theothergas.state_mirror import DeviceStateMirror

# Die vier HC-Felder, ihr Gerätetyp und der Backend-`telemetry.extra`-Key.
# DIESE Keys müssen exakt den Backend-`SOLVER_FIELDS`-Keys entsprechen —
# driften sie auseinander, verwirft `validate_extra` die Werte stumm.
_HC_FIELDS = [
    ("solar", CONF_ENTITY_HC_PV_POWER, "hc_pv_power_kw"),
    ("battery", CONF_ENTITY_HC_BATTERY_POWER, "hc_battery_power_kw"),
    ("grid", CONF_ENTITY_HC_GRID_POWER, "hc_grid_power_kw"),
    ("battery", CONF_ENTITY_PV_TO_BATTERY_POWER, "pv_to_battery_power_kw"),
]


def _make_coordinator(hass: HomeAssistant, devices: list[dict]) -> CrowdergyCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    coord = CrowdergyCoordinator.__new__(CrowdergyCoordinator)
    coord.hass = hass
    coord.entry = entry
    coord.devices = devices
    coord.state = DeviceStateMirror()
    return coord


def test_hc_fields_registered_in_read_fields():
    """Die HC-Sensoren erscheinen am semantisch passenden Gerätetyp im
    Config-Flow-Read-Schritt."""
    assert CONF_ENTITY_HC_PV_POWER in _READ_FIELDS["solar"]
    assert CONF_ENTITY_HC_GRID_POWER in _READ_FIELDS["grid"]
    assert CONF_ENTITY_HC_BATTERY_POWER in _READ_FIELDS["battery"]
    assert CONF_ENTITY_PV_TO_BATTERY_POWER in _READ_FIELDS["battery"]
    # HC-from-PV gehört NICHT ans Netz und umgekehrt.
    assert CONF_ENTITY_HC_PV_POWER not in _READ_FIELDS["grid"]
    assert CONF_ENTITY_HC_GRID_POWER not in _READ_FIELDS["solar"]


def test_thermal_loads_expose_energy_total_read_field():
    """Die steuerbaren thermischen Lasten (heating/warmwater/aircon) bieten
    alle den optionalen kWh-Zähler im Read-Schritt an. Regression: aircon
    fehlte in `_READ_FIELDS` → der Picker fiel auf Power-only zurück und der
    Energiezähler-Slot fehlte im Klima-Mapping (Connector-Bug 2026-06-18)."""
    from custom_components.theothergas.const import CONF_ENTITY_ENERGY_TOTAL

    for dtype in ("heating", "warmwater", "aircon"):
        assert CONF_ENTITY_ENERGY_TOTAL in _READ_FIELDS[dtype], dtype


def test_hc_fields_have_sensor_selectors_and_allowlist():
    """Jedes Read-Feld braucht einen EntitySelector und einen Default-
    DENY-Allowlist-Eintrag (nur `sensor`)."""
    for _dtype, conf_key, _payload in _HC_FIELDS:
        assert conf_key in _ENTITY_SELECTORS, conf_key
        assert MAPPABLE_ENTITY_DOMAINS.get(conf_key) == frozenset({"sensor"}), conf_key


def test_hc_extra_field_registry_matches_backend_keys():
    """Connector↔Backend-Naht: `_SOLVER_EXTRA_FIELDS` mappt jeden HC-
    Conf-Key auf den erwarteten `*_power_kw`-Bag-Key mit dem
    Power-Reader."""
    flat = {
        (dtype, conf_key): (payload_key, reader)
        for dtype, fields in _SOLVER_EXTRA_FIELDS.items()
        for payload_key, conf_key, reader in fields
    }
    for dtype, conf_key, payload_key in _HC_FIELDS:
        assert (dtype, conf_key) in flat, (dtype, conf_key)
        got_payload, reader = flat[(dtype, conf_key)]
        assert got_payload == payload_key
        assert reader == "power"


async def test_compose_extra_reads_hc_power_in_kw(hass: HomeAssistant):
    """Solar-Device mit gemapptem HC-from-PV-Sensor in W → extra-Bag
    trägt den Wert als kW unter `hc_pv_power_kw`."""
    dev = {
        CONF_DEVICE_ID: "s1",
        CONF_DEVICE_TYPE: "solar",
        CONF_ENTITY_HC_PV_POWER: "sensor.hc_from_pv",
    }
    coord = _make_coordinator(hass, [dev])
    hass.states.async_set(
        "sensor.hc_from_pv", "2500", {"unit_of_measurement": "W"}
    )
    extra = coord._compose_extra(dev)
    assert extra == {"hc_pv_power_kw": 2.5}


async def test_compose_extra_battery_reads_both_sensors(hass: HomeAssistant):
    """Battery-Device kann HC-from-Battery UND den optionalen
    PV→Batterie-Sensor tragen; ein kW-Sensor bleibt unkonvertiert."""
    dev = {
        CONF_DEVICE_ID: "b1",
        CONF_DEVICE_TYPE: "battery",
        CONF_ENTITY_HC_BATTERY_POWER: "sensor.hc_from_batt",
        CONF_ENTITY_PV_TO_BATTERY_POWER: "sensor.pv_to_batt",
    }
    coord = _make_coordinator(hass, [dev])
    hass.states.async_set(
        "sensor.hc_from_batt", "800", {"unit_of_measurement": "W"}
    )
    hass.states.async_set(
        "sensor.pv_to_batt", "1.2", {"unit_of_measurement": "kW"}
    )
    extra = coord._compose_extra(dev)
    assert extra == {"hc_battery_power_kw": 0.8, "pv_to_battery_power_kw": 1.2}


async def test_compose_extra_skips_unmapped_and_unavailable(hass: HomeAssistant):
    """Nicht gemappte Felder + `unavailable` Sensoren landen NICHT im
    Bag — kein None-Müll, der die Telemetrie aufbläht."""
    dev = {
        CONF_DEVICE_ID: "g1",
        CONF_DEVICE_TYPE: "grid",
        CONF_ENTITY_HC_GRID_POWER: "sensor.hc_from_grid",
    }
    coord = _make_coordinator(hass, [dev])
    hass.states.async_set("sensor.hc_from_grid", "unavailable")
    assert coord._compose_extra(dev) == {}


async def test_compose_extra_empty_for_device_without_hc(hass: HomeAssistant):
    """Solar-Device ohne gemappten HC-Sensor → leerer Bag (Caller lässt
    `extra` dann ganz weg)."""
    dev = {CONF_DEVICE_ID: "s2", CONF_DEVICE_TYPE: "solar"}
    coord = _make_coordinator(hass, [dev])
    assert coord._compose_extra(dev) == {}


def test_build_device_record_persists_hc_slots():
    """Round-Trip-Regression (v3.28.0-Defekt 2026-06-16): die 4 HC-Slots,
    die das Schema sammelt (`_READ_FIELDS`/`_ENTITY_SELECTORS`) und der
    Coordinator liest (`_SOLVER_EXTRA_FIELDS`), MÜSSEN aus dem Submit in
    den persistierten Device-Record gelangen. Vorher droppte
    `_build_device_record` sie stumm → der Vendor-Wahrheit-Pfad blieb leer
    (Schema angefasst, Record vergessen). Diese Naht Schema → Persistenz
    decken die Registry-/Reader-Tests oben NICHT ab."""
    entity_input = {
        CONF_ENTITY_HC_PV_POWER: "sensor.hc_pv",
        CONF_ENTITY_HC_BATTERY_POWER: "sensor.hc_batt",
        CONF_ENTITY_HC_GRID_POWER: "sensor.hc_grid",
        CONF_ENTITY_PV_TO_BATTERY_POWER: "sensor.pv_to_batt",
    }
    record = _build_device_record("dev-1", "solar", "Solar", entity_input)
    for key, val in entity_input.items():
        assert record[key] == val, f"{key} fehlt im persistierten Record"


def test_build_device_record_persists_wallbox_charge_current():
    """Round-Trip-Regression (v3.33.x-Defekt): die optionale Wallbox-
    Ladestrom-Number-Entity, die das Schema sammelt
    (`config_flow_schemas` Control-Section + `_ENTITY_SELECTORS`) und der
    Dispatcher liest (`command_dispatcher` → `number.set_value`), MUSS aus
    dem Submit in den persistierten Record gelangen. Vorher fehlte sie in
    `_build_device_record` → die Entity ließ sich nicht speichern (beim
    Re-Open weg), und `supports_charge_current` fiel beim Edit auf False
    zurück (Schema angefasst, Record vergessen — selbe Naht wie HC oben)."""
    entity_input = {CONF_ENTITY_WALLBOX_CHARGE_CURRENT: "number.wallbox_strom_a"}
    record = _build_device_record("dev-wb", "wallbox", "Wallbox", entity_input)
    assert record[CONF_ENTITY_WALLBOX_CHARGE_CURRENT] == "number.wallbox_strom_a"


def test_build_device_record_charge_current_default_empty():
    """Ohne Ladestrom-Mapping trägt der Slot einen leeren String (wie alle
    anderen optionalen Slots) — kein KeyError, sauberer stale-Drop."""
    record = _build_device_record("dev-wb2", "wallbox", "Wallbox", {})
    assert record[CONF_ENTITY_WALLBOX_CHARGE_CURRENT] == ""


def test_build_device_record_hc_slots_default_empty():
    """Ohne HC-Eingaben tragen die Slots leere Strings (wie alle anderen
    Read-Slots) — kein KeyError, sauberer stale-Mapping-Drop bei
    Typwechsel."""
    record = _build_device_record("dev-2", "grid", "Netz", {})
    for _type, key, _backend_key in _HC_FIELDS:
        assert record[key] == ""
