"""Crowd-Preset-Slot-Spec — das Mapping-Dictionary für Vendor-Presets.

Single Source of Truth dafür, WAS ein „share setup"-Beitrag pro
Device-Type transportiert (User-Vorgabe 2026-06-11: Contribute über
Solar hinaus, Box-Wizard je Device, „alle benötigten Entities").

Drei Slot-Arten:

* ``entity`` — HA-Entity-IDs. Wandern als ``entity_map`` zum Backend;
  die Box löst sie beim Nachbauen per Suffix-Match gegen die EIGENEN
  Entity-IDs auf (Präfix = kundenseitiger Gerätename, nicht portabel).
* ``value``  — integrationsspezifische Strings (Select-Optionen wie
  „Lock Mode", Hold-Modus). Identisch auf jeder Installation derselben
  Integration → wandern als ``value_map`` mit und werden auf der Box
  VERBATIM übernommen (kein Matching nötig).
* ``flag``   — booleans (Vorzeichen-Konventionen). Serialisiert als
  String ``"true"`` und nur wenn gesetzt — ``box_add_device`` validiert
  seine Payload als str→str, und ein fehlendes Flag ist der Default.

``required`` markiert das funktionale Minimum, mit dem ein Gerät dieses
Typs auf der Box sinnvoll läuft (Messung + bei steuerbaren Typen die
Steuerung). Der Contribute-Flow lehnt unvollständige Beiträge mit einer
verständlichen Fehlliste ab; alles Optionale wird mitgeschickt, wenn
der Beitragende es gemappt hat, und auf der Box als Capability gezeigt.

Bewusste Abweichung von „Batterie kw+kwh+steuerung": die kWh-Zähler
sind bei battery/grid OPTIONAL (nicht jede Integration exposed
getrennte Lade-/Entlade-Zähler); wer das härter will, setzt hier
``required=True`` — eine Zeile, der Rest der Pipeline folgt der Spec.

Konsumenten:
* config_flow Contribute-Steps (Picker-Kandidaten, Vollständigkeits-
  Gate, Payload-Konstruktion),
* box_services.box_add_device (Default-DENY-Allowlist der Value-Slots,
  analog MAPPABLE_ENTITY_DOMAINS für Entity-Slots),
* Backend + box-manager spiegeln die Required-Sets (Vertrag:
  docs/crowd-preset-store.md). NEUE Slots: hier eintragen + in
  MAPPABLE_ENTITY_DOMAINS (wenn entity) + Vertrag nachziehen.
"""
from __future__ import annotations

from dataclasses import dataclass

from .const import (
    CONF_BATTERY_SETPOINT_INVERT_SIGN,
    CONF_CHARGE_MODE_VALUE_LOCK,
    CONF_CHARGE_MODE_VALUE_POWER,
    CONF_CHARGE_MODE_VALUE_SOLAR,
    CONF_ENTITY_BATTERY_MODE,
    CONF_ENTITY_BATTERY_POWER_SETPOINT,
    CONF_ENTITY_CHARGE_MODE,
    CONF_ENTITY_WALLBOX_CHARGE_CURRENT,
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
class PresetSlot:
    key: str
    kind: str  # "entity" | "value" | "flag"
    required: bool
    label_de: str
    """Menschlich lesbar — für die Fehlliste im Contribute-Flow und
    die Capability-Anzeige (Box-GUI spiegelt Gruppen-Labels)."""


# Energie-Zähler-Semantik = E2E-Konvention #17 (final 2026-06-11):
# entity_energy_total = Bezug/Entladen (device → home),
# entity_energy_discharged_total = Einspeisung/Laden.
PRESET_SLOT_SPEC: dict[str, tuple[PresetSlot, ...]] = {
    "solar": (
        PresetSlot(CONF_ENTITY_POWER, "entity", True, "Aktuelle Leistung (kW)"),
        PresetSlot(CONF_ENTITY_ENERGY_TOTAL, "entity", True, "Ertragszähler (kWh)"),
        PresetSlot(CONF_INVERT_POWER_SIGN, "flag", False, "Leistungs-Vorzeichen umkehren"),
    ),
    "grid": (
        PresetSlot(CONF_ENTITY_POWER, "entity", True, "Netzleistung (kW)"),
        PresetSlot(CONF_ENTITY_ENERGY_TOTAL, "entity", True, "Bezugszähler (kWh)"),
        PresetSlot(
            CONF_ENTITY_ENERGY_DISCHARGED_TOTAL,
            "entity", False, "Einspeisezähler (kWh)",
        ),
        PresetSlot(
            CONF_ENTITY_POWER_2,
            "entity", False, "Einspeise-Leistung (kW, zweiter Sensor)",
        ),
        PresetSlot(CONF_INVERT_POWER_SIGN, "flag", False, "Leistungs-Vorzeichen umkehren"),
    ),
    "battery": (
        PresetSlot(CONF_ENTITY_POWER, "entity", True, "Batterieleistung (kW)"),
        PresetSlot(CONF_ENTITY_SOC, "entity", True, "Ladestand (SoC %)"),
        # Steuerung = Battery-Dispatch v3.8 (Mode-Select + Setpoint).
        PresetSlot(CONF_ENTITY_BATTERY_MODE, "entity", True, "Betriebsmodus (Select)"),
        PresetSlot(
            CONF_VALUE_BATTERY_MODE_ACTIVE, "value", True, "Modus-Wert „aktiv“",
        ),
        PresetSlot(
            CONF_VALUE_BATTERY_MODE_PASSIVE, "value", True, "Modus-Wert „passiv“",
        ),
        PresetSlot(
            CONF_ENTITY_BATTERY_POWER_SETPOINT,
            "entity", True, "Leistungs-Sollwert (W, Number)",
        ),
        PresetSlot(CONF_ENTITY_ENERGY_TOTAL, "entity", False, "Entlade-Zähler (kWh)"),
        PresetSlot(
            CONF_ENTITY_ENERGY_DISCHARGED_TOTAL,
            "entity", False, "Lade-Zähler (kWh)",
        ),
        PresetSlot(
            CONF_ENTITY_POWER_2, "entity", False, "Ladeleistung (kW, zweiter Sensor)",
        ),
        PresetSlot(
            CONF_ENTITY_CHARGE_MODE,
            "entity", False, "Lademodus-Select (manuelle App-Steuerung)",
        ),
        PresetSlot(
            CONF_BATTERY_SETPOINT_INVERT_SIGN,
            "flag", False, "Setpoint-Vorzeichen umkehren",
        ),
        PresetSlot(CONF_INVERT_POWER_SIGN, "flag", False, "Leistungs-Vorzeichen umkehren"),
        PresetSlot(CONF_ENTITY_CONTROL_HOLD, "value", False, "Hold-Modus"),
    ),
    "wallbox": (
        PresetSlot(CONF_ENTITY_POWER, "entity", True, "Ladeleistung (kW)"),
        PresetSlot(CONF_ENTITY_ENERGY_TOTAL, "entity", True, "Energiezähler (kWh)"),
        PresetSlot(CONF_ENTITY_CHARGE_MODE, "entity", True, "Lademodus-Select"),
        # Die drei Mode-Strings sind optional — Modi ohne Wert bekommen
        # (wie im interaktiven Flow) schlicht keinen Button in iOS.
        PresetSlot(CONF_CHARGE_MODE_VALUE_LOCK, "value", False, "Lademodus-Wert „Aus“"),
        PresetSlot(CONF_CHARGE_MODE_VALUE_POWER, "value", False, "Lademodus-Wert „An“"),
        PresetSlot(
            CONF_CHARGE_MODE_VALUE_SOLAR,
            "value", False, "Lademodus-Wert „Solaroptimiert“",
        ),
        # Optional: Ladestrom-Steuerung (2026-06-20). Number-Entity die
        # den Ladestrom in Ampere (6–16) setzt — wenn gemappt, wählt der
        # AI im „An"-Modus den optimalen Strom statt nur volle Leistung.
        PresetSlot(
            CONF_ENTITY_WALLBOX_CHARGE_CURRENT,
            "entity", False, "Ladestrom-Number (A, 6–16)",
        ),
        PresetSlot(CONF_ENTITY_SOC, "entity", False, "Ladestand Fahrzeug (SoC %)"),
        PresetSlot(CONF_ENTITY_VEHICLE_STATUS, "entity", False, "Fahrzeugstatus"),
        PresetSlot(
            CONF_VEHICLE_STATUS_VALUE_PLUGGED,
            "value", False, "Fahrzeugstatus-Wert „eingesteckt“",
        ),
        PresetSlot(
            CONF_VEHICLE_STATUS_VALUE_UNPLUGGED,
            "value", False, "Fahrzeugstatus-Wert „ausgesteckt“",
        ),
        PresetSlot(
            CONF_VEHICLE_STATUS_VALUE_ERROR,
            "value", False, "Fahrzeugstatus-Wert „Fehler“",
        ),
        PresetSlot(CONF_INVERT_POWER_SIGN, "flag", False, "Leistungs-Vorzeichen umkehren"),
        PresetSlot(CONF_ENTITY_CONTROL_HOLD, "value", False, "Hold-Modus"),
    ),
    # ── Steuerbare thermische Lasten (#68, 2026-06-18) ──────────────────
    # Gemeinsames Steuer-Muster: EINE Steuer-Entity (entity_control) +
    # optionale on/off-Werte. value_on/value_off sind OPTIONAL, nicht
    # required — bei binären Steuer-Entities (switch/input_boolean/light/
    # fan) schaltet der Connector implizit per turn_on/turn_off, ein leeres
    # value_on/off ist also legitim; nur select/climate/water_heater
    # brauchen die Strings (command_dispatcher._write_control). Pflicht ist
    # daher nur Leistung + Steuer-Entity (analog wallbox = kW + Lademodus).
    # Anonymisierungssicher: nur diese spezifizierten Slots verlassen die
    # Installation (entity-IDs + Integrations-Select-Strings, keine PII;
    # `shares_hardware_with_device_id` ist install-spezifisch → NICHT hier).
    "heating": (
        PresetSlot(CONF_ENTITY_POWER, "entity", True, "Aktuelle Leistung (kW)"),
        PresetSlot(CONF_ENTITY_CONTROL, "entity", True, "Steuerung (An/Aus)"),
        PresetSlot(CONF_VALUE_ON, "value", False, "Steuerwert „An“"),
        PresetSlot(CONF_VALUE_OFF, "value", False, "Steuerwert „Aus“"),
        PresetSlot(CONF_ENTITY_CURRENT_TEMP, "entity", False, "Ist-Temperatur (°C)"),
        PresetSlot(CONF_ENTITY_ENERGY_TOTAL, "entity", False, "Energiezähler (kWh)"),
        # Modulierende WPs: Solver sendet einen Vorlauf-Sollwert pro Tick.
        PresetSlot(
            CONF_ENTITY_VORLAUF_SETPOINT, "entity", False,
            "Vorlauf-Sollwert (°C, modulierend)",
        ),
        PresetSlot(CONF_ENTITY_VORLAUF_TEMP, "entity", False, "Vorlauf-Ist (°C, Sensor)"),
        # Heizung-die-auch-kühlt (Dual-Mode): supports_cooling wird im
        # Backend aus value_cool_on/aircon-Typ abgeleitet, nicht hier.
        PresetSlot(CONF_ENTITY_COOL_CONTROL, "entity", False, "Kühl-Steuerung (separat)"),
        PresetSlot(CONF_VALUE_COOL_ON, "value", False, "Kühlmodus-Wert „An“"),
        PresetSlot(CONF_VALUE_COOL_OFF, "value", False, "Kühlmodus-Wert „Aus“"),
        PresetSlot(CONF_INVERT_POWER_SIGN, "flag", False, "Leistungs-Vorzeichen umkehren"),
        PresetSlot(CONF_ENTITY_CONTROL_HOLD, "value", False, "Hold-Modus"),
    ),
    "warmwater": (
        PresetSlot(CONF_ENTITY_POWER, "entity", True, "Aktuelle Leistung (kW)"),
        PresetSlot(CONF_ENTITY_CONTROL, "entity", True, "Steuerung (An/Aus)"),
        PresetSlot(CONF_VALUE_ON, "value", False, "Steuerwert „An“"),
        PresetSlot(CONF_VALUE_OFF, "value", False, "Steuerwert „Aus“"),
        PresetSlot(CONF_ENTITY_CURRENT_TEMP, "entity", False, "Speicher-Temperatur (°C)"),
        PresetSlot(CONF_ENTITY_ENERGY_TOTAL, "entity", False, "Energiezähler (kWh)"),
        PresetSlot(CONF_INVERT_POWER_SIGN, "flag", False, "Leistungs-Vorzeichen umkehren"),
        PresetSlot(CONF_ENTITY_CONTROL_HOLD, "value", False, "Hold-Modus"),
    ),
    "aircon": (
        PresetSlot(CONF_ENTITY_POWER, "entity", True, "Aktuelle Leistung (kW)"),
        PresetSlot(CONF_ENTITY_CONTROL, "entity", True, "Steuerung (Kühlen An/Aus)"),
        PresetSlot(CONF_VALUE_ON, "value", False, "Steuerwert „An“"),
        PresetSlot(CONF_VALUE_OFF, "value", False, "Steuerwert „Aus“"),
        # supports_cooling ist für aircon backend-seitig immer True; eine
        # separate Kühl-Entity ist optional (manche melden Kühlen über
        # entity_control selbst).
        PresetSlot(CONF_ENTITY_COOL_CONTROL, "entity", False, "Kühl-Steuerung (separat)"),
        PresetSlot(CONF_VALUE_COOL_ON, "value", False, "Kühlmodus-Wert „An“"),
        PresetSlot(CONF_VALUE_COOL_OFF, "value", False, "Kühlmodus-Wert „Aus“"),
        PresetSlot(CONF_ENTITY_CURRENT_TEMP, "entity", False, "Raumtemperatur (°C)"),
        PresetSlot(CONF_ENTITY_ENERGY_TOTAL, "entity", False, "Energiezähler (kWh)"),
        PresetSlot(CONF_INVERT_POWER_SIGN, "flag", False, "Leistungs-Vorzeichen umkehren"),
        PresetSlot(CONF_ENTITY_CONTROL_HOLD, "value", False, "Hold-Modus"),
    ),
}

PRESET_CAPABLE_TYPES = frozenset(PRESET_SLOT_SPEC)

# Allowlist der Nicht-Entity-Slots, die `box_add_device` akzeptiert —
# Gegenstück zu MAPPABLE_ENTITY_DOMAINS (Default-DENY auch für Werte).
# value_on/off (+cool) sind seit #68 über die heating/aircon-Spec
# abgedeckt; die explizite Union bleibt als Backstop für `generic`
# (noch ohne Spec), damit ein künftiger generic-Preset-Pfad nicht an
# dieser Liste scheitert.
PRESET_VALUE_SLOTS: frozenset[str] = frozenset(
    slot.key
    for slots in PRESET_SLOT_SPEC.values()
    for slot in slots
    if slot.kind in ("value", "flag")
) | frozenset({"value_on", "value_off", "value_cool_on", "value_cool_off"})


def _is_set(value: object) -> bool:
    return value not in ("", None, False)


def extract_preset_maps(device: dict) -> tuple[dict[str, str], dict[str, str]]:
    """Device-Record (Config-Entry) → (entity_map, value_map) gemäß Spec.

    Bewusst Allowlist-getrieben statt „alle entity_*-Keys" (Pre-v3.24-
    Verhalten): nur spezifizierte Slots verlassen die Installation —
    das IST die Anonymisierungs-Schicht, die Contribute bisher auf
    Solar beschränkt hat. Flags werden als "true" serialisiert (siehe
    Modul-Doku), unbekannte Typen liefern leere Maps.
    """
    entity_map: dict[str, str] = {}
    value_map: dict[str, str] = {}
    for slot in PRESET_SLOT_SPEC.get(device.get("device_type", ""), ()):
        value = device.get(slot.key)
        if not _is_set(value):
            continue
        if slot.kind == "entity":
            entity_map[slot.key] = str(value)
        elif slot.kind == "flag":
            value_map[slot.key] = "true"
        else:
            value_map[slot.key] = str(value)
    return entity_map, value_map


def missing_required_labels(device: dict) -> list[str]:
    """Fehlende Pflicht-Slots eines Device-Records, als deutsche Labels
    für die Contribute-Fehlermeldung. Leer = Beitrag ist vollständig."""
    return [
        slot.label_de
        for slot in PRESET_SLOT_SPEC.get(device.get("device_type", ""), ())
        if slot.required and not _is_set(device.get(slot.key))
    ]
