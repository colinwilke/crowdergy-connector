"""device_class-Filter pro Entity-Slot (#46, kW/kWh-Typsicherheit).

Sperrt den Vertrag aus `config_flow._ENTITY_SELECTORS`: eindeutige
Read-Slots tragen einen device_class-Filter (Leistungs-Slots → "power",
Energie-Zähler → "energy", SoC → "battery", reine Temp-Sensoren →
"temperature"), damit der Picker je Slot nur den passenden Mess-Typ
anbietet und die häufige kW/kWh-Verwechslung verhindert.

Die Multi-Domain-/Control-Slots dürfen KEINEN device_class-Filter tragen:
der HA-Filter ist hart, und climate/water_heater-Entities haben keine
sensor-device_class — ein Filter würde sie ausblenden und u. a. den
dokumentierten climate-first Edit-Reload-Pfad (CONF_ENTITY_CURRENT_TEMP)
brechen.
"""
from __future__ import annotations

import pytest

from custom_components.theothergas import config_flow as cf
from custom_components.theothergas.const import (
    CONF_ENTITY_CHARGE_MODE,
    CONF_ENTITY_CLIMATE,
    CONF_ENTITY_CONTROL,
    CONF_ENTITY_CURRENT_TEMP,
    CONF_ENTITY_ENERGY_DISCHARGED_TOTAL,
    CONF_ENTITY_ENERGY_TOTAL,
    CONF_ENTITY_HC_BATTERY_POWER,
    CONF_ENTITY_HC_GRID_POWER,
    CONF_ENTITY_HC_PV_POWER,
    CONF_ENTITY_POWER,
    CONF_ENTITY_POWER_2,
    CONF_ENTITY_PV_TO_BATTERY_POWER,
    CONF_ENTITY_SOC,
    CONF_ENTITY_VEHICLE_STATUS,
    CONF_ENTITY_VORLAUF_SETPOINT,
    CONF_ENTITY_VORLAUF_TEMP,
    CONF_ENTITY_WATER_HEATER,
)

# Slot → erwartete device_class. HA normalisiert den Filter intern auf eine
# Liste, daher vergleichen wir gegen [<class>].
_EXPECTED_DEVICE_CLASS = {
    CONF_ENTITY_POWER: "power",
    CONF_ENTITY_POWER_2: "power",
    CONF_ENTITY_HC_PV_POWER: "power",
    CONF_ENTITY_HC_BATTERY_POWER: "power",
    CONF_ENTITY_HC_GRID_POWER: "power",
    CONF_ENTITY_PV_TO_BATTERY_POWER: "power",
    CONF_ENTITY_ENERGY_TOTAL: "energy",
    CONF_ENTITY_ENERGY_DISCHARGED_TOTAL: "energy",
    CONF_ENTITY_SOC: "battery",
    CONF_ENTITY_VORLAUF_TEMP: "temperature",
}

# Multi-Domain-/Control-Slots, die KEINEN device_class-Filter tragen dürfen.
_NO_DEVICE_CLASS = {
    CONF_ENTITY_CURRENT_TEMP,
    CONF_ENTITY_CONTROL,
    CONF_ENTITY_CLIMATE,
    CONF_ENTITY_WATER_HEATER,
    CONF_ENTITY_CHARGE_MODE,
    CONF_ENTITY_VORLAUF_SETPOINT,
    CONF_ENTITY_VEHICLE_STATUS,
}


@pytest.mark.parametrize(
    ("slot", "device_class"), sorted(_EXPECTED_DEVICE_CLASS.items())
)
def test_read_slot_carries_expected_device_class(slot, device_class):
    """Eindeutige Read-Slots filtern auf ihren Mess-Typ."""
    config = cf._ENTITY_SELECTORS[slot].config
    # HA normalisiert den Filter auf eine Liste.
    assert config.get("device_class") == [device_class]
    # domain-Filter bleibt erhalten — der device_class-Filter ergänzt ihn,
    # ersetzt ihn nicht.
    assert config.get("domain") == ["sensor"]


@pytest.mark.parametrize("slot", sorted(_NO_DEVICE_CLASS))
def test_multi_domain_slot_has_no_device_class(slot):
    """Multi-Domain-/Control-Slots dürfen NICHT gefiltert werden."""
    config = cf._ENTITY_SELECTORS[slot].config
    assert "device_class" not in config


def test_current_temp_keeps_climate_water_heater_domains():
    """CONF_ENTITY_CURRENT_TEMP behält climate/water_heater (Edit-Reload)."""
    config = cf._ENTITY_SELECTORS[CONF_ENTITY_CURRENT_TEMP].config
    assert set(config.get("domain", [])) == {
        "sensor",
        "climate",
        "water_heater",
    }
    assert "device_class" not in config
