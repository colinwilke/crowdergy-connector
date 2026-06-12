"""Slot-Schema für Crowd-Presets — der PUBLIC Teil des Store-Vertrags.

Vertrag: docs/crowd-preset-store.md. Dieses Modul ist die SSOT dafür,
WELCHE Slots eines Device-Records portabel sind und in einen
Crowd-Preset-Beitrag gehören:

* **Entity-Slots** (`entity_map`, slot → entity_id): werden beim
  Anwenden auf einer fremden Installation per Suffix-Match neu
  aufgelöst (Entity-ID-Präfixe tragen den kundenseitigen Gerätenamen,
  nur der Integrations-Messname ist portabel).
* **Value-Slots** (`value_map`, slot → string): Select-Optionen und
  Flags der Integration (z.B. "External"/"Internal" beim Plenticore-
  Battery-Mode) — installationsUNabhängig, werden verbatim übernommen.
  Boolean-Flags laut Vertrag als String `"true"` (nicht gesetzt =
  Slot weglassen).

BEWUSST NICHT im Preset (nicht portabel bzw. installationsspezifisch):
* `entity_climate` / `entity_water_heater` — Form-only Felder des
  Climate-first-Onboardings; kollabieren beim Speichern auf
  `entity_control`, das im Preset landet.
* `entity_outdoor_temp_c` — integration-wide, kein Device-Slot.
* `shares_hardware_with_device_id` — Backend-Device-ID der jeweiligen
  Installation.
* `included_in_haushalt` — hängt an der Zähler-Topologie des Haushalts.
* `district` / `city` / `region` — Standort.

Konsistenz-Invariante (Test-gesichert): jeder Entity-Slot hier muss in
`MAPPABLE_ENTITY_DOMAINS` (const.py, Default-DENY) stehen — sonst würde
`box_add_device` ein Store-Preset beim Anwenden ablehnen.

Das Backend prüft nur die Shape (string→string, Key-Bounds); die
Slot-Semantik erzwingen Connector/Box beim Anwenden. Store-DATEN und
Kuration bleiben im Backend hinter Auth — hier liegt nur das Schema.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .const import (
    CONF_BATTERY_SETPOINT_INVERT_SIGN,
    CONF_CHARGE_MODE_VALUE_LOCK,
    CONF_CHARGE_MODE_VALUE_POWER,
    CONF_CHARGE_MODE_VALUE_SOLAR,
    CONF_ENTITY_BATTERY_MODE,
    CONF_ENTITY_BATTERY_POWER_SETPOINT,
    CONF_ENTITY_CHARGE_MODE,
    CONF_ENTITY_CONTROL,
    CONF_ENTITY_CONTROL_HOLD,
    CONF_ENTITY_COOL_CONTROL,
    CONF_ENTITY_CURRENT_TEMP,
    CONF_ENTITY_ENERGY_DISCHARGED_TOTAL,
    CONF_ENTITY_ENERGY_TOTAL,
    CONF_ENTITY_POWER,
    CONF_ENTITY_POWER_2,
    CONF_ENTITY_SOC,
    CONF_ENTITY_VEHICLE_STATUS,
    CONF_ENTITY_VORLAUF_SETPOINT,
    CONF_ENTITY_VORLAUF_TEMP,
    CONF_INVERT_POWER_SIGN,
    CONF_SUPPORTS_COOLING,
    CONF_VALUE_BATTERY_MODE_ACTIVE,
    CONF_VALUE_BATTERY_MODE_PASSIVE,
    CONF_VALUE_COOL_OFF,
    CONF_VALUE_COOL_ON,
    CONF_VALUE_OFF,
    CONF_VALUE_ON,
    CONF_VEHICLE_STATUS_VALUE_ERROR,
    CONF_VEHICLE_STATUS_VALUE_PLUGGED,
    CONF_VEHICLE_STATUS_VALUE_UNPLUGGED,
)


@dataclass(frozen=True)
class PresetSlots:
    """Portable Slots eines Device-Types.

    `required_entities` müssen auf der Ziel-Installation auflösbar
    sein, damit das Preset anwendbar ist (die Box registriert kein
    Gerät ohne Power-Slot); `optional_entities` werden bei
    Nicht-Auflösung als `missing_slots` gemeldet statt zu blocken.
    """

    required_entities: frozenset[str]
    optional_entities: frozenset[str]
    value_slots: frozenset[str]

    @property
    def entity_slots(self) -> frozenset[str]:
        return self.required_entities | self.optional_entities


_POWER_ONLY = frozenset({CONF_ENTITY_POWER})
_SIGN_FLAG = frozenset({CONF_INVERT_POWER_SIGN})
_ON_OFF = frozenset({CONF_VALUE_ON, CONF_VALUE_OFF, CONF_ENTITY_CONTROL_HOLD})
_COOLING_VALUES = frozenset(
    {CONF_SUPPORTS_COOLING, CONF_VALUE_COOL_ON, CONF_VALUE_COOL_OFF}
)

PRESET_SLOT_SPEC: dict[str, PresetSlots] = {
    "solar": PresetSlots(
        required_entities=_POWER_ONLY,
        optional_entities=frozenset({CONF_ENTITY_ENERGY_TOTAL}),
        value_slots=_SIGN_FLAG,
    ),
    "grid": PresetSlots(
        required_entities=_POWER_ONLY,
        optional_entities=frozenset({
            CONF_ENTITY_POWER_2,
            CONF_ENTITY_ENERGY_TOTAL,
            CONF_ENTITY_ENERGY_DISCHARGED_TOTAL,
        }),
        value_slots=_SIGN_FLAG,
    ),
    "haushalt": PresetSlots(
        required_entities=_POWER_ONLY,
        optional_entities=frozenset({CONF_ENTITY_ENERGY_TOTAL}),
        value_slots=_SIGN_FLAG,
    ),
    "battery": PresetSlots(
        required_entities=_POWER_ONLY,
        optional_entities=frozenset({
            CONF_ENTITY_POWER_2,
            CONF_ENTITY_ENERGY_TOTAL,
            CONF_ENTITY_ENERGY_DISCHARGED_TOTAL,
            CONF_ENTITY_SOC,
            CONF_ENTITY_BATTERY_MODE,
            CONF_ENTITY_BATTERY_POWER_SETPOINT,
        }),
        value_slots=_SIGN_FLAG | frozenset({
            CONF_VALUE_BATTERY_MODE_ACTIVE,
            CONF_VALUE_BATTERY_MODE_PASSIVE,
            CONF_BATTERY_SETPOINT_INVERT_SIGN,
        }),
    ),
    "wallbox": PresetSlots(
        required_entities=_POWER_ONLY,
        optional_entities=frozenset({
            CONF_ENTITY_ENERGY_TOTAL,
            CONF_ENTITY_SOC,
            CONF_ENTITY_VEHICLE_STATUS,
            CONF_ENTITY_CONTROL,
            CONF_ENTITY_CHARGE_MODE,
        }),
        value_slots=_SIGN_FLAG | _ON_OFF | frozenset({
            CONF_CHARGE_MODE_VALUE_LOCK,
            CONF_CHARGE_MODE_VALUE_POWER,
            CONF_CHARGE_MODE_VALUE_SOLAR,
            CONF_VEHICLE_STATUS_VALUE_PLUGGED,
            CONF_VEHICLE_STATUS_VALUE_UNPLUGGED,
            CONF_VEHICLE_STATUS_VALUE_ERROR,
        }),
    ),
    "heating": PresetSlots(
        required_entities=_POWER_ONLY,
        optional_entities=frozenset({
            CONF_ENTITY_ENERGY_TOTAL,
            CONF_ENTITY_CURRENT_TEMP,
            CONF_ENTITY_VORLAUF_TEMP,
            CONF_ENTITY_VORLAUF_SETPOINT,
            CONF_ENTITY_CONTROL,
            CONF_ENTITY_COOL_CONTROL,
        }),
        value_slots=_SIGN_FLAG | _ON_OFF | _COOLING_VALUES,
    ),
    "warmwater": PresetSlots(
        required_entities=_POWER_ONLY,
        optional_entities=frozenset({
            CONF_ENTITY_ENERGY_TOTAL,
            CONF_ENTITY_CURRENT_TEMP,
            CONF_ENTITY_CONTROL,
        }),
        value_slots=_SIGN_FLAG | _ON_OFF,
    ),
    "aircon": PresetSlots(
        required_entities=_POWER_ONLY,
        optional_entities=frozenset({
            CONF_ENTITY_ENERGY_TOTAL,
            CONF_ENTITY_CURRENT_TEMP,
            CONF_ENTITY_CONTROL,
            CONF_ENTITY_COOL_CONTROL,
        }),
        value_slots=_SIGN_FLAG | _ON_OFF | _COOLING_VALUES,
    ),
    "generic": PresetSlots(
        required_entities=_POWER_ONLY,
        optional_entities=frozenset({
            CONF_ENTITY_ENERGY_TOTAL,
            CONF_ENTITY_CONTROL,
        }),
        value_slots=_SIGN_FLAG | _ON_OFF,
    ),
}


def split_device_record(
    device_type: str, record: Mapping[str, Any]
) -> tuple[dict[str, str], dict[str, str]]:
    """Zerlegt einen Device-Record (Config-Entry-Dict) in die portablen
    `(entity_map, value_map)` für einen Contribute-Beitrag.

    Nicht-portable Keys werden verworfen; Boolean-Flags True → "true",
    False/leer → weggelassen (laut Vertrag trägt ein fehlender Slot
    keine Information — die Box übernimmt nur gesetzte Werte).
    """
    spec = PRESET_SLOT_SPEC.get(device_type)
    if spec is None:
        return {}, {}
    entity_map: dict[str, str] = {}
    for slot in sorted(spec.entity_slots):
        value = record.get(slot)
        if isinstance(value, str) and value:
            entity_map[slot] = value
    value_map: dict[str, str] = {}
    for slot in sorted(spec.value_slots):
        value = record.get(slot)
        if value is True:
            value_map[slot] = "true"
        elif isinstance(value, str) and value:
            value_map[slot] = value
    return entity_map, value_map
