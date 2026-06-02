"""Config flow for Crowdergy Connector integration."""
from __future__ import annotations

import logging
from typing import Any

import httpx
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_API_URL,
    CONF_CITY,
    CONF_DEVICE_ID,
    CONF_DEVICE_CONFIG_MODE,
    CONF_DEVICE_NAME,
    CONF_DEVICE_TYPE,
    CONF_DEVICES,
    CONFIG_MODE_CLIMATE,
    CONFIG_MODE_MANUAL,
    CONF_DISTRICT,
    CONF_ENTITY_OUTDOOR_TEMP,
    CONF_EMAIL,
    CONF_ENTITY_CHARGE_MODE,
    CONF_ENTITY_CLIMATE,
    CONF_ENTITY_WATER_HEATER,
    CONF_ENTITY_CONTROL,
    CONF_ENTITY_POWER,
    CONF_ENTITY_POWER_2,
    CONF_ENTITY_SOC,
    CONF_ENTITY_VEHICLE_STATUS,
    CONF_ENTITY_CURRENT_TEMP,
    CONF_ENTITY_VORLAUF_TEMP,
    CONF_ENTITY_ENERGY_TOTAL,
    CONF_ENTITY_ENERGY_DISCHARGED_TOTAL,  # noqa: F401 — used as slot key
    CONF_INVERT_POWER_SIGN,
    CONF_PASSWORD,
    CONF_REFRESH_TOKEN,
    CONF_REGION,
    CONF_USER_ID,
    CONF_ENTITY_CONTROL_HOLD,
    CONF_VALUE_OFF,
    CONF_VALUE_ON,
    CONF_VEHICLE_STATUS_VALUE_PLUGGED,
    CONF_VEHICLE_STATUS_VALUE_UNPLUGGED,
    CONF_VEHICLE_STATUS_VALUE_ERROR,
    CONF_CHARGE_MODE_VALUE_LOCK,
    CONF_CHARGE_MODE_VALUE_POWER,
    CONF_CHARGE_MODE_VALUE_SOLAR,
    CONF_BATTERY_VALUE_CHARGE,
    CONF_BATTERY_VALUE_IDLE,
    CONF_BATTERY_VALUE_DISCHARGE,
    CONF_BATTERY_VALUE_PASSIVE,
    CONF_SHARES_HARDWARE_WITH,
    CONF_INCLUDED_IN_HAUSHALT,
    CONF_SUPPORTS_COOLING,
    CONF_ENTITY_COOL_CONTROL,
    CONF_VALUE_COOL_ON,
    CONF_VALUE_COOL_OFF,
    CONF_SETUP_MODE,
    SETUP_MODE_AUTO,
    SETUP_MODE_MANUAL,
    HEURISTIC_ACCEPT,
    MAPPING_LLM_ENABLED,
    ENTITY_CONTROL_HOLD_ALWAYS,
    ENTITY_CONTROL_HOLD_AUTO,
    ENTITY_CONTROL_HOLD_NEVER,
    DEFAULT_API_URL,
    DEVICE_TYPES,
    DOMAIN,
    USER_AGENT,
)
from .entity_mapper import DeviceGroup, discover_devices, discover_devices_with_llm

_LOGGER = logging.getLogger(__name__)

# German labels for the device-type picker.
DEVICE_TYPE_LABELS_DE = {
    "solar": "Solar",
    "battery": "Batterie",
    "wallbox": "Wallbox",
    "grid": "Netz",
    "heating": "Heizung (Wärmepumpe)",
    "warmwater": "Warmwasser (Wärmepumpe)",
    "generic": "Sonstiges",
    "haushalt": "Haushalt",
}

# Device types that the Crowdergy app can switch on/off through the
# user-mapped entity_control. Solar / Grid / Haushalt are read-only.
_CONTROLLABLE_TYPES = {"battery", "wallbox", "heating", "warmwater", "generic"}

# v2.4: device types that can carry the "included in haushalt sensor"
# flag. Classic consumers — their draw is plausibly already counted by
# the user's haushalt-sensor (single home-meter). Solar / Grid /
# Battery / Haushalt itself are excluded: solar generates, grid is the
# meter itself, battery is bidirectional storage, haushalt is the
# residual we're computing.
_HAUSHALT_FLAG_TYPES = {
    "heating", "warmwater", "wallbox", "generic",
}

# Device-Typen die einen Kühl-Modus konfigurieren können — heating-
# family only. Warmwater nie (DHW-Tank ist monotonic-heat), wallbox /
# battery haben eigene Mode-Controls, solar / grid / haushalt sind
# read-only, generic ist Catch-all ohne Thermo-Modell.
_COOLING_CAPABLE_TYPES = {"heating"}

# Entity domains where on/off is implicit (turn_on / turn_off services) —
# no value_on / value_off needs to be typed by the user.
_BINARY_DOMAINS = {"switch", "input_boolean", "light", "fan"}


def _is_binary_entity(entity_id: str) -> bool:
    if not entity_id:
        return False
    return entity_id.split(".", 1)[0] in _BINARY_DOMAINS


def _auto_fill_binary_vehicle_status(entity_input: dict[str, Any]) -> None:
    """Wallbox-Fahrzeugstatus auto-mappen wenn die Entity ein
    binary_sensor.* ist — bei Binary-Sensors ist on/off die einzige
    sinnvolle Ausprägung, da hat der User nichts zu mappen.

    Häufiger Stolperstein (2026-05-30): HA's UI zeigt für binary
    sensors lokalisierte Labels ("Eingesteckt"/"Ausgesteckt"), aber
    der ROHE state.state ist "on"/"off". User tippten "Eingesteckt"
    in das Mapping-Feld → kein Match im Coordinator → iOS bekommt
    Rohwert "on" → Status-Display verwirrend.

    Auto-Fill nur wenn der User nichts eingetragen hat (er kann
    immer noch overriden, falls ein binary_sensor exotische States
    liefert wie "True"/"False")."""
    entity = entity_input.get(CONF_ENTITY_VEHICLE_STATUS, "") or ""
    if not entity.startswith("binary_sensor."):
        return
    if not entity_input.get(CONF_VEHICLE_STATUS_VALUE_PLUGGED, ""):
        entity_input[CONF_VEHICLE_STATUS_VALUE_PLUGGED] = "on"
    if not entity_input.get(CONF_VEHICLE_STATUS_VALUE_UNPLUGGED, ""):
        entity_input[CONF_VEHICLE_STATUS_VALUE_UNPLUGGED] = "off"

# Which read-side telemetry fields each device type exposes. Crowdergize-
# capable types additionally get the control trio (entity_control +
# value_on + value_off) rendered as a separate section.
_READ_FIELDS: dict[str, list[str]] = {
    "solar":     [CONF_ENTITY_POWER, CONF_ENTITY_ENERGY_TOTAL],
    # v3.0 bidirektional: zweites Power-Sensor-Feld neben dem zweiten
    # Energie-Zähler. power_1 = Bezug (Energie raus aus Netz, ins Haus),
    # power_2 = Einspeisung. Coordinator computet signed power_1 - power_2.
    "grid":      [
        CONF_ENTITY_POWER, CONF_ENTITY_ENERGY_TOTAL,
        CONF_ENTITY_POWER_2, CONF_ENTITY_ENERGY_DISCHARGED_TOTAL,
    ],
    "heating":   [
        CONF_ENTITY_POWER, CONF_ENTITY_ENERGY_TOTAL,
    ],
    "warmwater": [
        CONF_ENTITY_POWER, CONF_ENTITY_ENERGY_TOTAL,
    ],
    "haushalt":  [CONF_ENTITY_POWER, CONF_ENTITY_ENERGY_TOTAL],
    # Batterie: power_1 = Entladung, power_2 = Ladung, energy_1 +
    # energy_2 die zugehörigen kWh-Zähler. SoC zwischen den Power-
    # Paaren damit visuell zusammengehört.
    "battery":   [
        CONF_ENTITY_POWER, CONF_ENTITY_ENERGY_TOTAL,
        CONF_ENTITY_POWER_2, CONF_ENTITY_ENERGY_DISCHARGED_TOTAL,
        CONF_ENTITY_SOC,
    ],
    # Wallbox V2G "kommt später" — heute nur unidirektional.
    "wallbox":   [
        CONF_ENTITY_POWER, CONF_ENTITY_ENERGY_TOTAL,
        CONF_ENTITY_SOC, CONF_ENTITY_VEHICLE_STATUS,
    ],
    "generic":   [CONF_ENTITY_POWER, CONF_ENTITY_ENERGY_TOTAL],
}

# Bidirektionale Typen: zwei Power-Sensoren + zwei Energie-Zähler,
# Coordinator computet signed-Werte aus dem Differenzpaar. Unidirek-
# tionale Typen mit nur Power_1 + invert_power_sign-Fallback wenn der
# Sensor in der "falschen" Richtung sigt.
_BIDIRECTIONAL_TYPES = {"grid", "battery"}

# Entity-selector configs keyed by the CONF_ENTITY_* name.
_ENTITY_SELECTORS: dict[str, selector.EntitySelector] = {
    CONF_ENTITY_POWER: selector.EntitySelector(
        selector.EntitySelectorConfig(domain="sensor")
    ),
    # v3.0 zweites Power-Sensor-Feld (bidirektional) — selber
    # Selector-Typ wie CONF_ENTITY_POWER.
    CONF_ENTITY_POWER_2: selector.EntitySelector(
        selector.EntitySelectorConfig(domain="sensor")
    ),
    CONF_ENTITY_SOC: selector.EntitySelector(
        selector.EntitySelectorConfig(domain="sensor")
    ),
    CONF_ENTITY_VEHICLE_STATUS: selector.EntitySelector(
        selector.EntitySelectorConfig(domain=["sensor", "binary_sensor"])
    ),
    CONF_ENTITY_CURRENT_TEMP: selector.EntitySelector(
        # sensor = klassischer Pfad; climate / water_heater nötig weil
        # _apply_climate_first die climate-/water_heater-Entity in das
        # Feld kopiert und der Selector beim Edit-Reload sonst die
        # Validierung verweigert.
        selector.EntitySelectorConfig(
            domain=["sensor", "climate", "water_heater"]
        )
    ),
    # Solver-only Vorlauf-Temperatur (v3.3+). Optional pro heating-
    # Gerät; wenn gesetzt verfeinert das Backend den COP-Schätzer
    # gegenüber dem statischen W35-Annahme-Modell. Nur Sensor-Domain
    # — die Vorlauf-Temp sitzt typisch in einer eigenen Modbus-/
    # Number-Entity, nicht als Attribut einer Climate-Entity.
    CONF_ENTITY_VORLAUF_TEMP: selector.EntitySelector(
        selector.EntitySelectorConfig(domain="sensor")
    ),
    # Climate-first Pick für heating. Aus dem climate-State
    # leitet der Connector Steuerung (set_hvac_mode), Ist-Temperatur
    # (Attribut current_temperature) und Modi-Liste (hvac_modes) ab.
    CONF_ENTITY_CLIMATE: selector.EntitySelector(
        selector.EntitySelectorConfig(domain="climate")
    ),
    # Water-heater-first Pick für warmwater (v3.0.6). Analog
    # entity_climate aber für HA's water_heater-Domain — viele
    # Brauchwasser-WPs sitzen dort statt unter climate.
    CONF_ENTITY_WATER_HEATER: selector.EntitySelector(
        selector.EntitySelectorConfig(domain="water_heater")
    ),
    # Any settable HA entity — connector adapts the service call to the
    # entity's domain at runtime (switch.turn_on/off, number.set_value,
    # select.select_option, climate.set_hvac_mode,
    # water_heater.set_operation_mode, …).
    CONF_ENTITY_CONTROL: selector.EntitySelector(
        selector.EntitySelectorConfig(domain=[
            "switch", "input_boolean", "number", "select",
            "light", "fan", "climate", "water_heater",
            "input_number", "input_select",
        ])
    ),
    # Wallbox-only Lademodus target — restricted to select entities since
    # the iOS picker offers a multi-option choice (typically the wallbox
    # integration's own charge-mode select).
    CONF_ENTITY_CHARGE_MODE: selector.EntitySelector(
        selector.EntitySelectorConfig(domain=["select", "input_select"])
    ),
    # Energy meter — HA `total_increasing` kWh sensor (lifetime
    # cumulative). Restricted to plain sensor entities; the backend
    # rejects non-monotonic data via a delta clamp.
    CONF_ENTITY_ENERGY_TOTAL: selector.EntitySelector(
        selector.EntitySelectorConfig(domain="sensor")
    ),
    # Battery-only: second `total_increasing` kWh sensor for the
    # discharge counter — splits charge / discharge into separate
    # streams server-side.
    CONF_ENTITY_ENERGY_DISCHARGED_TOTAL: selector.EntitySelector(
        selector.EntitySelectorConfig(domain="sensor")
    ),
}


# ── Schema builders ─────────────────────────────────────────────────────────


def _type_name_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Step 1: pick device type + name."""
    d = defaults or {}
    type_selector = selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[
                selector.SelectOptionDict(
                    value=dt,
                    label=DEVICE_TYPE_LABELS_DE.get(dt, dt.capitalize()),
                )
                for dt in DEVICE_TYPES
            ],
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )
    type_field: Any = (
        vol.Required(CONF_DEVICE_TYPE, default=d[CONF_DEVICE_TYPE])
        if d.get(CONF_DEVICE_TYPE)
        else vol.Required(CONF_DEVICE_TYPE)
    )
    name_field: Any = (
        vol.Required(CONF_DEVICE_NAME, default=d[CONF_DEVICE_NAME])
        if d.get(CONF_DEVICE_NAME)
        else vol.Required(CONF_DEVICE_NAME)
    )
    return vol.Schema(
        {
            type_field: type_selector,
            name_field: str,
        }
    )


def _config_mode_schema(
    defaults: dict[str, Any] | None = None,
) -> vol.Schema:
    """v3.0 Step 1b: KonfigMode-Picker für heating/warmwater.
    Default Manuell für neue Geräte (sicher: alle Entities einzeln,
    funktioniert mit jedem HA-Setup). Climate ist die moderne Variante
    für WPs die als climate.* Entity in HA erscheinen — Steuerung +
    Ist-Temperatur + Modi kommen automatisch.
    """
    d = defaults or {}
    default_mode = d.get(CONF_DEVICE_CONFIG_MODE) or CONFIG_MODE_MANUAL
    # Inline labels weggelassen damit der `selector.config_mode.options`
    # Translation-Block aus de.json/strings.json greift — sonst überstimmen
    # die Inline-Werte die Übersetzungen.
    return vol.Schema({
        vol.Required(
            CONF_DEVICE_CONFIG_MODE, default=default_mode
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[CONFIG_MODE_MANUAL, CONFIG_MODE_CLIMATE],
                mode=selector.SelectSelectorMode.LIST,
                translation_key="config_mode",
            )
        ),
    })


def _entity_field(key: str, defaults: dict[str, Any]) -> Any:
    if defaults.get(key):
        return vol.Optional(key, default=defaults[key])
    return vol.Optional(key)


def _entities_schema(
    device_type: str,
    defaults: dict[str, Any] | None = None,
    config_mode: str = CONFIG_MODE_MANUAL,
    include_name: bool = False,
) -> vol.Schema:
    """Step 2: typ-abhängiges Entity-Schema.

    Layout: read-side Sensoren oben (immer mindestens
    entity_current_power_kw). Für controllable types eine
    Steuerungs-Section mit entity_control bzw. entity_charge_mode.
    Werte (value_on/off bzw. Lademodus-Mapping) folgen im nächsten
    Step typ-bewusst aus dem chosen entity_control hergeleitet.

    include_name=True (Edit-Flow only) zeigt den Anzeigenamen oben am
    Step, damit der User beim Bearbeiten umbenennen kann ohne durch
    einen separaten Typ/Name-Step zu müssen (Type ist im Edit-Flow
    sowieso fixed).
    """
    d = defaults or {}
    read_fields = _READ_FIELDS.get(device_type, [CONF_ENTITY_POWER])
    schema_dict: dict[Any, Any] = {}

    if include_name:
        name_default = d.get(CONF_DEVICE_NAME, "")
        schema_dict[
            vol.Required(CONF_DEVICE_NAME, default=name_default)
        ] = str

    # "Vorzeichen umkehren"-Toggle für die Power-Entität. Crowdergy-
    # Konvention: positiv = Bezug / Verbrauch / Lade. HA-Sensoren die
    # umgekehrt liegen (manche Modbus-Wirkleistungen, manche Goe-
    # Charger-Templates) kann der User mit einem Haken statt einem
    # HA-Template kompensieren.
    invert_field = vol.Optional(
        CONF_INVERT_POWER_SIGN,
        default=bool(d.get(CONF_INVERT_POWER_SIGN, False)),
    )
    if len(read_fields) == 1 and device_type not in _CONTROLLABLE_TYPES:
        # Single-purpose read-only types (solar/grid/haushalt): no
        # section wrapping — looks silly with one field.
        for key in read_fields:
            schema_dict[_entity_field(key, d)] = _ENTITY_SELECTORS[key]
        schema_dict[invert_field] = selector.BooleanSelector()
    else:
        read_schema_fields: dict[Any, Any] = {
            _entity_field(key, d): _ENTITY_SELECTORS[key] for key in read_fields
        }
        read_schema_fields[invert_field] = selector.BooleanSelector()
        read_schema = vol.Schema(read_schema_fields)
        schema_dict[vol.Required("read_section")] = section(
            read_schema, {"collapsed": False}
        )

    # v3.0: Haushalt-Toggle für Verbraucher direkt hier statt eigener
    # Step am Ende. Default True für neue Geräte (häufigster Fall:
    # Hauszähler sieht alles), Edit-Flow zieht stored value.
    if device_type in _HAUSHALT_FLAG_TYPES:
        haushalt_default = d.get(CONF_INCLUDED_IN_HAUSHALT, True)
        schema_dict[
            vol.Optional(
                CONF_INCLUDED_IN_HAUSHALT, default=haushalt_default
            )
        ] = selector.BooleanSelector()

    if device_type == "wallbox":
        # Wallbox's full control surface lives behind ONE entity: a
        # select with Lock / Solar / Power options that the solver
        # picks between per slot. The follow-up step then maps each
        # option to its select-string in the wallbox's firmware
        # (typically named differently per vendor — go-eCharger calls
        # them "Lock Mode" / "Solar Pure Mode" / "Power Mode").
        # We dropped the parallel entity_control + value_on/value_off
        # path because it duplicated the same on/off semantics the
        # Lademodus already covers via the Lock option, and users
        # found being asked twice for what looked like the same
        # entity confusing.
        control_schema = vol.Schema({
            _entity_field(CONF_ENTITY_CHARGE_MODE, d):
                _ENTITY_SELECTORS[CONF_ENTITY_CHARGE_MODE],
        })
        schema_dict[vol.Required("control_section")] = section(
            control_schema, {"collapsed": False}
        )
    elif device_type == "battery":
        # Battery uses the 4-mode dispatch entity (typically a
        # number-entity taking +max / 0 / -max W). Solver picks
        # charge / idle / discharge / passive per slot, mapped to
        # one of the three written values in the follow-up step.
        # No separate entity_control / value_on/value_off — the
        # 4-mode entity covers everything the inverter exposes.
        control_schema = vol.Schema({
            _entity_field(CONF_ENTITY_CHARGE_MODE, d):
                _ENTITY_SELECTORS[CONF_ENTITY_CHARGE_MODE],
        })
        schema_dict[vol.Required("control_section")] = section(
            control_schema, {"collapsed": False}
        )
    elif device_type in {"heating", "warmwater"}:
        # v3.0: Branch nach KonfigMode aus Step 1b.
        # Climate-Mode: nur entity_climate; Ist-Temp + Steuerung
        # leitet der Connector daraus ab (set_hvac_mode + Attribut
        # current_temperature). _apply_climate_first kopiert beim
        # Submit auf entity_control + entity_current_temp_c sodass
        # die downstream-Pipeline unverändert läuft.
        # Manual-Mode: klassischer Pfad mit separater Steuer- + Ist-
        # Temp-Entity. Edit-Flow erbt config_mode aus dem stored Wert
        # (Default manual für Legacy v2.x).
        stored_mode = d.get(CONF_DEVICE_CONFIG_MODE)
        # C1-Auto-Migration (2026-06-01): pre-v3.0 entries hatten kein
        # CONF_DEVICE_CONFIG_MODE. Wenn die gespeicherte entity_control
        # zufällig auf eine climate.* / water_heater.* Entity zeigt,
        # behandeln wir das als CONFIG_MODE_CLIMATE und füllen das
        # entity_climate-/entity_water_heater-Feld aus entity_control
        # vor, damit der Edit-Flow defaults sinnvoll rendert.
        if not stored_mode:
            legacy_ctrl = d.get(CONF_ENTITY_CONTROL, "")
            if isinstance(legacy_ctrl, str) and legacy_ctrl.startswith(
                ("climate.", "water_heater.")
            ):
                stored_mode = CONFIG_MODE_CLIMATE
                if legacy_ctrl.startswith("climate.") and not d.get(
                    CONF_ENTITY_CLIMATE
                ):
                    d[CONF_ENTITY_CLIMATE] = legacy_ctrl
                elif legacy_ctrl.startswith("water_heater.") and not d.get(
                    CONF_ENTITY_WATER_HEATER
                ):
                    d[CONF_ENTITY_WATER_HEATER] = legacy_ctrl
        effective_mode = (
            stored_mode or config_mode or CONFIG_MODE_MANUAL
        )
        if effective_mode == CONFIG_MODE_CLIMATE:
            # Heating → climate-Domain, Warmwasser → water_heater-Domain.
            # User-Wunsch 2026-05-30: für beide IMMER ein optionales
            # Ist-Temperatur-Override-Feld anbieten — manche Vendor-
            # Integrationen liefern eine kaputte current_temperature
            # über die Climate-/Water-Heater-Entity und der User braucht
            # einen sauberen separaten Sensor als Override.
            if device_type == "warmwater":
                primary_field = _entity_field(CONF_ENTITY_WATER_HEATER, d)
                primary_selector = _ENTITY_SELECTORS[CONF_ENTITY_WATER_HEATER]
            else:
                primary_field = _entity_field(CONF_ENTITY_CLIMATE, d)
                primary_selector = _ENTITY_SELECTORS[CONF_ENTITY_CLIMATE]
            control_fields: dict[Any, Any] = {
                primary_field: primary_selector,
                _entity_field(CONF_ENTITY_CURRENT_TEMP, d):
                    _ENTITY_SELECTORS[CONF_ENTITY_CURRENT_TEMP],
            }
            # Vorlauf-Temp gibt's für beide heating-family-Typen: bei
            # heating ist's der HK-Vorlauf, bei warmwater der
            # Warmwasser-Vorlauf der WW-Erzeugung (Brauchwasser-WP
            # liefert oft eine eigene VL-Temperatur fürs Erhitzen).
            # In beiden Fällen verbessert es die COP-Schätzung.
            control_fields[
                _entity_field(CONF_ENTITY_VORLAUF_TEMP, d)
            ] = _ENTITY_SELECTORS[CONF_ENTITY_VORLAUF_TEMP]
            control_schema = vol.Schema(control_fields)
        else:
            control_fields = {
                _entity_field(CONF_ENTITY_CONTROL, d):
                    _ENTITY_SELECTORS[CONF_ENTITY_CONTROL],
                _entity_field(CONF_ENTITY_CURRENT_TEMP, d):
                    _ENTITY_SELECTORS[CONF_ENTITY_CURRENT_TEMP],
            }
            control_fields[
                _entity_field(CONF_ENTITY_VORLAUF_TEMP, d)
            ] = _ENTITY_SELECTORS[CONF_ENTITY_VORLAUF_TEMP]
            control_schema = vol.Schema(control_fields)
        schema_dict[vol.Required("control_section")] = section(
            control_schema, {"collapsed": False}
        )
    elif device_type in _CONTROLLABLE_TYPES:
        # generic: universeller entity_control. value_on/off im
        # Follow-up typ-bewusst.
        control_schema = vol.Schema({
            _entity_field(CONF_ENTITY_CONTROL, d): _ENTITY_SELECTORS[CONF_ENTITY_CONTROL],
        })
        schema_dict[vol.Required("control_section")] = section(
            control_schema, {"collapsed": False}
        )

    return vol.Schema(schema_dict)


def _flatten_sections(user_input: dict[str, Any]) -> dict[str, Any]:
    """Lift nested section dicts back into a flat mapping the persistence layer expects.

    `section(...)` wraps its fields under the section's key in the result;
    everything else (and any flat fields) is passed through unchanged.
    """
    flat: dict[str, Any] = {}
    for key, value in user_input.items():
        if key in ("read_section", "control_section") and isinstance(value, dict):
            flat.update(value)
        else:
            flat[key] = value
    return flat


def _apply_climate_first(entity_input: dict[str, Any]) -> dict[str, Any]:
    """Climate-/Water-Heater-first: wenn entity_climate ODER
    entity_water_heater gesetzt ist, kopieren wir den Wert auf
    entity_control. Damit weiß der Connector im Dispatch ob er
    `climate.set_hvac_mode` (climate.*) oder
    `water_heater.set_operation_mode` (water_heater.*) ruft —
    die Domain-Auswahl ist im Service-Adapter.

    **Warmwasser** (water_heater.*): die `current_temperature` der
    primary entity IST die Tank-Temp — auto-copy nach
    `entity_current_temp` ist semantisch korrekt.

    **Heating** (climate.*): die `current_temperature` ist bei den
    meisten WP-/FBH-Setups die VORLAUF-Temp, NICHT die Raumtemp.
    Auto-copy nach `entity_current_temp` würde das Vorlauf-Signal
    als Raumtemp ans Backend pushen und das Thermomodell vergiften
    (35–45 °C als T_room interpretiert). Ab v3.4.6 daher: für
    heating-Devices KEIN auto-copy mehr — der User muss
    `entity_current_temp` als separaten Raumtemp-Sensor
    konfigurieren wenn er Crowdergy-AI auf der Heizung will.
    Coordinator routet `climate.current_temperature` separat in
    `vorlauf_temp_c` (Solver-Extra).

    Mutiert das Dict in-place und gibt es zurück (chainable).
    """
    primary = (
        entity_input.get(CONF_ENTITY_CLIMATE, "")
        or entity_input.get(CONF_ENTITY_WATER_HEATER, "")
    )
    if primary:
        entity_input[CONF_ENTITY_CONTROL] = primary
        # Auto-copy nach entity_current_temp NUR für water_heater
        # (warmwater) — bei climate (heating) ist current_temperature
        # die Vorlauf-Temp und gehört nicht in current_temp_c.
        is_water_heater = primary.startswith("water_heater.")
        if is_water_heater and not entity_input.get(CONF_ENTITY_CURRENT_TEMP, ""):
            entity_input[CONF_ENTITY_CURRENT_TEMP] = primary
    return entity_input


# ── Step 3 (Crowdergize-fähig): value_on / value_off, typ-bewusst ──────────


def _value_selector(hass, entity_id: str):
    """Build a type-aware selector for value_on / value_off based on the
    chosen entity_control. Returns None when no useful introspection is
    available — caller falls back to a plain string field then.
    """
    if not entity_id:
        return None
    domain = entity_id.split(".", 1)[0]
    state = hass.states.get(entity_id)
    if state is None:
        return None
    if domain in ("select", "input_select"):
        options = state.attributes.get("options") or []
        if options:
            return selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(value=o, label=o) for o in options
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )
    if domain in ("number", "input_number"):
        min_v = state.attributes.get("min")
        max_v = state.attributes.get("max")
        step_v = state.attributes.get("step", 1)
        if min_v is not None and max_v is not None:
            return selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=float(min_v),
                    max=float(max_v),
                    step=float(step_v),
                    mode=selector.NumberSelectorMode.BOX,
                )
            )
    if domain == "climate":
        modes = state.attributes.get("hvac_modes") or []
        if modes:
            return selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(value=m, label=m) for m in modes
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )
    if domain == "water_heater":
        # HA's water_heater exposes the available modes as the
        # `operation_list` attribute (vendor-specific: "eco" / "boost"
        # / "off" / "performance" / "electric" / etc.). Mirror the
        # climate-side selector so the value_on / value_off step
        # presents a dropdown instead of a free-text field.
        modes = state.attributes.get("operation_list") or []
        if modes:
            return selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(value=m, label=m) for m in modes
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )
    return None


def _values_schema(
    hass,
    entity_control: str,
    defaults: dict[str, Any],
    include_cooling: bool = False,
) -> vol.Schema:
    """Step C schema: value_on + value_off, typed if entity_control supports it.

    v3.0 include_cooling=True (heating type) → zusätzlich value_cool_on
    inline. value_cool_off entfällt im Schema, weil es bei climate-* und
    SG-Ready 1:1 identisch zu value_off ist (climate.set_hvac_mode("off"));
    _register_device leitet es daraus ab.
    """
    value_sel = _value_selector(hass, entity_control)

    def _field(key: str):
        default = defaults.get(key, "")
        # NumberSelector chokes on empty-string defaults; use None there.
        is_number = (
            entity_control
            and entity_control.split(".", 1)[0] in ("number", "input_number")
        )
        if default == "" and is_number:
            return vol.Optional(key)
        if default == "":
            return vol.Optional(key)
        # Cast for NumberSelector consistency
        if is_number:
            try:
                return vol.Optional(key, default=float(default))
            except (TypeError, ValueError):
                return vol.Optional(key)
        return vol.Optional(key, default=str(default))

    field_type: Any = value_sel if value_sel is not None else str

    schema: dict[Any, Any] = {
        _field(CONF_VALUE_ON): field_type,
        _field(CONF_VALUE_OFF): field_type,
    }
    if include_cooling:
        schema[_field(CONF_VALUE_COOL_ON)] = field_type
    schema[_hold_mode_field(defaults)] = _hold_mode_selector()
    return vol.Schema(schema)


def _hold_mode_field(defaults: dict[str, Any] | None = None) -> Any:
    """Voluptuous-Field für die Hold-Mode Auswahl. Default = "auto"."""
    d = defaults or {}
    return vol.Optional(
        CONF_ENTITY_CONTROL_HOLD,
        default=d.get(CONF_ENTITY_CONTROL_HOLD) or ENTITY_CONTROL_HOLD_AUTO,
    )


def _hold_mode_selector() -> selector.SelectSelector:
    """3-Option Dropdown — gilt für entity_control (heating/warmwater/
    generic) UND für entity_charge_mode (wallbox/battery). Auto =
    aktuell wie Always, später ggf. mit Smart-Verifikation.
    Labels kommen aus den `selector.entity_control_hold.options`
    Translation-Blöcken (de.json/strings.json) — Inline-Labels würden
    die Übersetzungen überschreiben.
    """
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[
                ENTITY_CONTROL_HOLD_AUTO,
                ENTITY_CONTROL_HOLD_ALWAYS,
                ENTITY_CONTROL_HOLD_NEVER,
            ],
            mode=selector.SelectSelectorMode.DROPDOWN,
            translation_key="entity_control_hold",
        )
    )


# ── v2.0: vehicle-status ternary mapping (wallbox-only) ────────────────────


def _vehicle_status_schema(
    hass, entity_vehicle_status: str, defaults: dict[str, Any] | None = None
) -> vol.Schema:
    """Schema for the three string values that map the wallbox's
    vehicle-status sensor to the normalised ternary plugged /
    unplugged / error. If the sensor is a select-style entity we
    introspect the options and present a dropdown; otherwise plain
    text fields with the current state pre-filled as a hint for the
    "plugged" or "unplugged" default.
    """
    d = defaults or {}

    field_type: Any = str
    options_selector = None
    if entity_vehicle_status:
        domain = entity_vehicle_status.split(".", 1)[0]
        state = hass.states.get(entity_vehicle_status)
        if domain in ("select", "input_select") and state is not None:
            opts = state.attributes.get("options") or []
            if opts:
                options_selector = selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(value=o, label=o)
                            for o in opts
                        ],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                )
                field_type = options_selector

    def _field(key: str, hint_default: str = "") -> Any:
        default = d.get(key) or hint_default
        if default:
            return vol.Optional(key, default=default)
        return vol.Optional(key)

    return vol.Schema({
        _field(CONF_VEHICLE_STATUS_VALUE_PLUGGED): field_type,
        _field(CONF_VEHICLE_STATUS_VALUE_UNPLUGGED): field_type,
        _field(CONF_VEHICLE_STATUS_VALUE_ERROR): field_type,
    })


# ── v2.2: charge-mode-values ternary mapping (wallbox-only) ─────────────────


def _charge_mode_values_schema(
    hass, entity_charge_mode: str, defaults: dict[str, Any] | None = None
) -> vol.Schema:
    """Schema for the three Lademodus mappings — Aus / An / Solaroptimiert
    → the wallbox's HA select-options. Mirrors `_vehicle_status_schema`:
    introspects the select entity's `options` attribute to render a
    dropdown when available, falls back to free-text otherwise. All
    three fields are optional — modes the user leaves blank simply
    don't get a button in the iOS tile.
    """
    d = defaults or {}

    field_type: Any = str
    if entity_charge_mode:
        domain = entity_charge_mode.split(".", 1)[0]
        state = hass.states.get(entity_charge_mode)
        if domain in ("select", "input_select") and state is not None:
            opts = state.attributes.get("options") or []
            if opts:
                field_type = selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(value=o, label=o)
                            for o in opts
                        ],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                )

    def _field(key: str) -> Any:
        default = d.get(key) or ""
        if default:
            return vol.Optional(key, default=default)
        return vol.Optional(key)

    return vol.Schema({
        _field(CONF_CHARGE_MODE_VALUE_LOCK): field_type,
        _field(CONF_CHARGE_MODE_VALUE_POWER): field_type,
        _field(CONF_CHARGE_MODE_VALUE_SOLAR): field_type,
        _hold_mode_field(d): _hold_mode_selector(),
    })


# ── v2.3: battery-mode-values ternary mapping (battery-only) ────────────────


def _battery_values_schema(
    hass, entity_charge_mode: str, defaults: dict[str, Any] | None = None
) -> vol.Schema:
    """Schema für die vier Batterie-Modus-Werte. Alle vier sind
    Pflichtfelder wenn ein entity_charge_mode gemapped ist —
    der „Passiv"-Wert ist **kritisch** weil er bei AI-Aus immer in
    die HA-Entity geschrieben wird (definierter Safe-Default,
    Inverter übernimmt PV-Self-Consumption autonom). Ohne Pflicht-
    feld blieb die Battery nach AI-Aus im letzten aktiven Mode
    hängen → User-Report 2026-05-31 Battery wurde unerwartet
    weiter ge-Halten."""
    d = defaults or {}

    field_type: Any = str
    if entity_charge_mode:
        domain = entity_charge_mode.split(".", 1)[0]
        state = hass.states.get(entity_charge_mode)
        if domain in ("select", "input_select") and state is not None:
            opts = state.attributes.get("options") or []
            if opts:
                field_type = selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(value=o, label=o)
                            for o in opts
                        ],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                )

    def _required(key: str) -> Any:
        default = d.get(key)
        if default:
            return vol.Required(key, default=default)
        return vol.Required(key)

    return vol.Schema({
        _required(CONF_BATTERY_VALUE_CHARGE): field_type,
        _required(CONF_BATTERY_VALUE_DISCHARGE): field_type,
        _required(CONF_BATTERY_VALUE_IDLE): field_type,
        _required(CONF_BATTERY_VALUE_PASSIVE): field_type,
        _hold_mode_field(d): _hold_mode_selector(),
    })


# ── v2.0: shares-hardware picker (warmwater-only) ──────────────────────────


def _shares_hardware_schema(
    heating_devices: list[dict[str, str]],
    defaults: dict[str, Any] | None = None,
) -> vol.Schema:
    """Optional picker letting a warmwater device declare it sits on
    the same compressor as an existing heating device. Options are
    (backend_device_id, device_name) pairs from the user's already-
    registered heating devices. Empty selection = standalone (no
    joint-power coupling)."""
    d = defaults or {}
    options = [
        selector.SelectOptionDict(value=h["id"], label=h["name"])
        for h in heating_devices
    ]
    # Sentinel for "no sibling" so the user can explicitly clear a
    # previous selection in the edit flow.
    options.insert(
        0, selector.SelectOptionDict(value="", label="— keine Kopplung —")
    )
    picker = selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=options,
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )
    default = d.get(CONF_SHARES_HARDWARE_WITH, "")
    return vol.Schema({
        vol.Optional(CONF_SHARES_HARDWARE_WITH, default=default): picker,
    })


# ── Persistence helpers ─────────────────────────────────────────────────────


def _build_device_record(
    backend_device_id: str,
    device_type: str,
    device_name: str,
    entity_input: dict[str, Any],
) -> dict[str, Any]:
    """Map a submitted form into the dict we persist on the config entry.

    Entities the device-type doesn't expose are written as empty strings so
    a later type change can drop stale mappings cleanly.
    """
    return {
        CONF_DEVICE_ID: backend_device_id,
        CONF_DEVICE_NAME: device_name,
        CONF_DEVICE_TYPE: device_type,
        # v3.0 KonfigMode (manual / climate). Default manual für Legacy
        # v2.x Geräte ohne Feld — sicher, weil die Manuell-Flow-Logik
        # genau das alte Verhalten ist.
        CONF_DEVICE_CONFIG_MODE: entity_input.get(
            CONF_DEVICE_CONFIG_MODE, CONFIG_MODE_MANUAL
        ),
        CONF_ENTITY_POWER: entity_input.get(CONF_ENTITY_POWER, ""),
        CONF_ENTITY_POWER_2: entity_input.get(CONF_ENTITY_POWER_2, ""),
        CONF_INVERT_POWER_SIGN: bool(entity_input.get(CONF_INVERT_POWER_SIGN, False)),
        CONF_ENTITY_SOC: entity_input.get(CONF_ENTITY_SOC, ""),
        CONF_ENTITY_VEHICLE_STATUS: entity_input.get(CONF_ENTITY_VEHICLE_STATUS, ""),
        CONF_ENTITY_CLIMATE: entity_input.get(CONF_ENTITY_CLIMATE, ""),
        CONF_ENTITY_WATER_HEATER: entity_input.get(
            CONF_ENTITY_WATER_HEATER, ""
        ),
        CONF_ENTITY_CURRENT_TEMP: entity_input.get(CONF_ENTITY_CURRENT_TEMP, ""),
        CONF_ENTITY_VORLAUF_TEMP: entity_input.get(CONF_ENTITY_VORLAUF_TEMP, ""),
        CONF_ENTITY_ENERGY_TOTAL: entity_input.get(CONF_ENTITY_ENERGY_TOTAL, ""),
        CONF_ENTITY_ENERGY_DISCHARGED_TOTAL: entity_input.get(
            CONF_ENTITY_ENERGY_DISCHARGED_TOTAL, ""
        ),
        CONF_ENTITY_CONTROL: entity_input.get(CONF_ENTITY_CONTROL, ""),
        CONF_VALUE_ON: entity_input.get(CONF_VALUE_ON, ""),
        CONF_VALUE_OFF: entity_input.get(CONF_VALUE_OFF, ""),
        CONF_ENTITY_CONTROL_HOLD: entity_input.get(
            CONF_ENTITY_CONTROL_HOLD, ENTITY_CONTROL_HOLD_AUTO
        ),
        CONF_ENTITY_CHARGE_MODE: entity_input.get(CONF_ENTITY_CHARGE_MODE, ""),
        # v2.0: ternary vehicle-status mapping (wallbox-only). Empty
        # strings on non-wallbox types — they get filtered out before
        # coordinator's mapping lookup.
        CONF_VEHICLE_STATUS_VALUE_PLUGGED: entity_input.get(
            CONF_VEHICLE_STATUS_VALUE_PLUGGED, ""
        ),
        CONF_VEHICLE_STATUS_VALUE_UNPLUGGED: entity_input.get(
            CONF_VEHICLE_STATUS_VALUE_UNPLUGGED, ""
        ),
        CONF_VEHICLE_STATUS_VALUE_ERROR: entity_input.get(
            CONF_VEHICLE_STATUS_VALUE_ERROR, ""
        ),
        # v2.2: wallbox-only Lademodus-Werte (Aus / An / Solaroptimiert
        # → wallbox HA select-options). Persisted here AND POSTed to
        # the backend so iOS knows which mode buttons to render.
        CONF_CHARGE_MODE_VALUE_LOCK: entity_input.get(
            CONF_CHARGE_MODE_VALUE_LOCK, ""
        ),
        CONF_CHARGE_MODE_VALUE_POWER: entity_input.get(
            CONF_CHARGE_MODE_VALUE_POWER, ""
        ),
        CONF_CHARGE_MODE_VALUE_SOLAR: entity_input.get(
            CONF_CHARGE_MODE_VALUE_SOLAR, ""
        ),
        # v2.3: battery-only mode mapping (Laden / Aus / Entladen → the
        # entity-value the connector writes). Empty strings on non-
        # battery types — worker dispatch suppresses any mode whose
        # mapping is empty.
        CONF_BATTERY_VALUE_CHARGE: entity_input.get(
            CONF_BATTERY_VALUE_CHARGE, ""
        ),
        CONF_BATTERY_VALUE_IDLE: entity_input.get(
            CONF_BATTERY_VALUE_IDLE, ""
        ),
        CONF_BATTERY_VALUE_DISCHARGE: entity_input.get(
            CONF_BATTERY_VALUE_DISCHARGE, ""
        ),
        CONF_BATTERY_VALUE_PASSIVE: entity_input.get(
            CONF_BATTERY_VALUE_PASSIVE, ""
        ),
        # v2.0: warmwater-only. The backend device-id of the heating
        # device sharing this compressor. Coordinator does nothing
        # with this — it's POSTed once at device-register time so the
        # backend can wire up the joint-power constraint.
        CONF_SHARES_HARDWARE_WITH: entity_input.get(
            CONF_SHARES_HARDWARE_WITH, ""
        ),
        # v2.4: classic-consumer flag for the haushalt double-counting
        # fix. Defaults to False on types that don't carry it (solar /
        # grid / battery / haushalt) — the backend ignores it anyway
        # for those types.
        CONF_INCLUDED_IN_HAUSHALT: bool(
            entity_input.get(CONF_INCLUDED_IN_HAUSHALT, False)
        ),
        # v3.0: supports_cooling abgeleitet aus bool(value_cool_on).
        # User-spec "leer = kein cooling" — kein separater Toggle mehr.
        # Falls die Cooling-Werte im entity_input fehlen (z.B. nicht-
        # heating Devices), fällt's auf bool('') = False.
        CONF_SUPPORTS_COOLING: bool(
            entity_input.get(CONF_VALUE_COOL_ON, "")
            or entity_input.get(CONF_SUPPORTS_COOLING, False)
        ),
        CONF_ENTITY_COOL_CONTROL: entity_input.get(
            CONF_ENTITY_COOL_CONTROL, ""
        ),
        CONF_VALUE_COOL_ON: entity_input.get(CONF_VALUE_COOL_ON, ""),
        CONF_VALUE_COOL_OFF: entity_input.get(CONF_VALUE_COOL_OFF, ""),
    }


async def _register_device(
    api_url: str,
    token: str,
    device_type: str,
    device_name: str,
    entity_input: dict[str, Any],
    location: dict[str, str],
) -> dict[str, Any]:
    """Register a device on the backend and return the full device dict."""
    device_config: dict[str, Any] = {
        "name": device_name,
        "type": device_type,
        "district": location.get(CONF_DISTRICT, ""),
        "city": location.get(CONF_CITY, ""),
        "region": location.get(CONF_REGION, ""),
    }
    # Warmwater only: tell the backend which heating device shares
    # this compressor so the joint solver can couple them. Skipped
    # when empty (standalone WW heater).
    shares_with = entity_input.get(CONF_SHARES_HARDWARE_WITH, "")
    if device_type == "warmwater" and shares_with:
        device_config["shares_hardware_with_device_id"] = shares_with
    # Wallbox only (v2.2+): push the Lademodus mappings so iOS knows
    # which mode buttons to render (Aus / An / Solaroptimiert). Each
    # field is independent — omit blanks rather than overwriting with
    # empty strings the backend would treat as "user cleared this".
    if device_type == "wallbox":
        for key, api_field in (
            (CONF_CHARGE_MODE_VALUE_LOCK, "charge_mode_value_lock"),
            (CONF_CHARGE_MODE_VALUE_POWER, "charge_mode_value_power"),
            (CONF_CHARGE_MODE_VALUE_SOLAR, "charge_mode_value_solar"),
        ):
            value = entity_input.get(key, "")
            if value:
                device_config[api_field] = value
    # Battery only (v2.3+): push the 4-mode mappings so the worker
    # can resolve solver decisions to entity-write values without
    # asking the connector each tick.
    if device_type == "battery":
        for key, api_field in (
            (CONF_BATTERY_VALUE_CHARGE, "battery_value_charge"),
            (CONF_BATTERY_VALUE_IDLE, "battery_value_idle"),
            (CONF_BATTERY_VALUE_DISCHARGE, "battery_value_discharge"),
            (CONF_BATTERY_VALUE_PASSIVE, "battery_value_passive"),
        ):
            value = entity_input.get(key, "")
            if value:
                device_config[api_field] = value
    # Classic-consumer haushalt-Flag — nur für die Typen wo's Sinn
    # macht. Solar / Grid / Battery / Haushalt selbst kennen's nicht.
    if device_type in {
        "heating", "warmwater", "wallbox", "generic",
    }:
        device_config["included_in_haushalt"] = bool(
            entity_input.get(CONF_INCLUDED_IN_HAUSHALT, False)
        )
    # Cooling-Capability — nur für heating POSTen. v3.0: aus
    # bool(value_cool_on) abgeleitet (oder fallback auf den alten
    # supports_cooling-Bool für Legacy-v2.x edit-paths). value_cool_off
    # ist im neuen Schema nicht mehr separat — fällt auf value_off
    # zurück, weil climate.set_hvac_mode("off") für beide Richtungen
    # derselbe Aufruf ist.
    if device_type == "heating":
        cool_on = entity_input.get(CONF_VALUE_COOL_ON, "") or ""
        cool_off = entity_input.get(CONF_VALUE_COOL_OFF, "") or ""
        if cool_on and not cool_off:
            cool_off = entity_input.get(CONF_VALUE_OFF, "") or ""
        device_config["supports_cooling"] = bool(
            cool_on or entity_input.get(CONF_SUPPORTS_COOLING, False)
        )
        cool_entity = entity_input.get(CONF_ENTITY_COOL_CONTROL, "")
        if cool_entity:
            device_config["entity_cool_control"] = cool_entity
        if cool_on:
            device_config["value_cool_on"] = cool_on
        if cool_off:
            device_config["value_cool_off"] = cool_off
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            f"{api_url}/api/v1/devices",
            headers={"Authorization": f"Bearer {token}"},
            json=device_config,
        )
        response.raise_for_status()
        result = response.json()

    return _build_device_record(result["id"], device_type, device_name, entity_input)


async def _update_device_backend(
    api_url: str,
    token: str,
    device_id: str,
    device_type: str,
    device_name: str,
    entity_input: dict[str, Any] | None = None,
) -> None:
    """PUT a device's mutable fields (name, type, and v2.2+ wallbox
    Lademodus-Werte) to the backend. The mode-values are sent as
    plain strings — backend treats empty string as "user cleared this"
    and renders the button accordingly in iOS."""
    payload: dict[str, Any] = {"name": device_name, "type": device_type}
    if entity_input is not None and device_type == "wallbox":
        for key, api_field in (
            (CONF_CHARGE_MODE_VALUE_LOCK, "charge_mode_value_lock"),
            (CONF_CHARGE_MODE_VALUE_POWER, "charge_mode_value_power"),
            (CONF_CHARGE_MODE_VALUE_SOLAR, "charge_mode_value_solar"),
        ):
            # Send unconditionally (even empty) so clearing a mapping
            # in the connector propagates to iOS — the connector is
            # authoritative for these values.
            payload[api_field] = entity_input.get(key, "")
    if entity_input is not None and device_type == "battery":
        for key, api_field in (
            (CONF_BATTERY_VALUE_CHARGE, "battery_value_charge"),
            (CONF_BATTERY_VALUE_IDLE, "battery_value_idle"),
            (CONF_BATTERY_VALUE_DISCHARGE, "battery_value_discharge"),
            (CONF_BATTERY_VALUE_PASSIVE, "battery_value_passive"),
        ):
            payload[api_field] = entity_input.get(key, "")
    # v2.4: classic-consumer haushalt flag — mirror the edit flow
    # selection so toggling it in the connector propagates to iOS.
    if (
        entity_input is not None
        and device_type in {
            "heating", "warmwater", "wallbox", "generic",
        }
    ):
        payload["included_in_haushalt"] = bool(
            entity_input.get(CONF_INCLUDED_IN_HAUSHALT, False)
        )
    # Cooling-Capability — Edit-Flow spiegelt die Auswahl. v3.0:
    # leitet supports_cooling aus bool(value_cool_on) ab. value_cool_off
    # ist im neuen Schema nicht mehr separat — fällt auf value_off
    # zurück, weil climate.set_hvac_mode("off") für beide Richtungen
    # derselbe Aufruf ist.
    if entity_input is not None and device_type == "heating":
        cool_on_val = entity_input.get(CONF_VALUE_COOL_ON, "") or ""
        cool_off_val = entity_input.get(CONF_VALUE_COOL_OFF, "") or ""
        if cool_on_val and not cool_off_val:
            cool_off_val = entity_input.get(CONF_VALUE_OFF, "") or ""
        payload["supports_cooling"] = bool(
            cool_on_val or entity_input.get(CONF_SUPPORTS_COOLING, False)
        )
        payload["entity_cool_control"] = entity_input.get(
            CONF_ENTITY_COOL_CONTROL, ""
        )
        payload["value_cool_on"] = cool_on_val
        payload["value_cool_off"] = cool_off_val
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.put(
            f"{api_url}/api/v1/devices/{device_id}",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
        )
        response.raise_for_status()


async def _resolve_location_defaults(hass) -> dict[str, str]:
    """Best-effort defaults for the district/city/region form fields.

    Strategy:
      1. Read `hass.config.latitude` / `longitude`. If both are usable,
         reverse-geocode via Nominatim and map the returned address to
         our Stadtteil/Stadt/Region buckets.
      2. On any failure (network, rate-limit, missing coords) fall back
         to `hass.config.location_name` as the city default. District
         and region stay empty.
    """
    fallback_city = (getattr(hass.config, "location_name", "") or "").strip()
    fallback = {CONF_DISTRICT: "", CONF_CITY: fallback_city, CONF_REGION: ""}

    lat = getattr(hass.config, "latitude", 0.0) or 0.0
    lon = getattr(hass.config, "longitude", 0.0) or 0.0
    if abs(lat) < 0.01 and abs(lon) < 0.01:
        return fallback

    url = "https://nominatim.openstreetmap.org/reverse"
    params = {
        "format": "json",
        "lat": f"{lat}",
        "lon": f"{lon}",
        "addressdetails": "1",
        "accept-language": "de",
    }
    headers = {
        # Nominatim's usage policy requires a descriptive UA on every
        # request. USER_AGENT is built from manifest.json so the version
        # tag and the User-Agent never drift apart.
        "User-Agent": USER_AGENT,
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as err:
        _LOGGER.debug("Reverse-geocode failed (%s); using location_name fallback", err)
        return fallback

    address = data.get("address", {}) if isinstance(data, dict) else {}

    # Take the first non-empty value across the candidate keys for each bucket.
    def _pick(keys: list[str]) -> str:
        for key in keys:
            value = address.get(key, "")
            if value:
                return str(value)
        return ""

    district = _pick(["suburb", "city_district", "borough", "quarter", "neighbourhood", "residential"])
    city = _pick(["city", "town", "village", "municipality"])
    region = _pick(["state", "region"])

    return {
        CONF_DISTRICT: district,
        CONF_CITY: city or fallback_city,
        CONF_REGION: region,
    }


async def _refresh_token(
    api_url: str, refresh_token: str
) -> tuple[str, str] | None:
    """One-shot token refresh for config-flow HTTP calls. The
    coordinator has its own refresh path on `_authenticated_request`;
    config-flow paths (register / update / delete) are too rare to
    justify the same machinery, so this small helper covers them.
    Returns (access_token, refresh_token) on success, None otherwise."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{api_url}/api/v1/auth/refresh",
                json={"refresh_token": refresh_token},
            )
        if response.status_code == 200:
            tokens = response.json()
            return tokens["access_token"], tokens["refresh_token"]
    except (httpx.HTTPStatusError, httpx.RequestError) as err:
        _LOGGER.error("Token refresh failed in config flow: %s", err)
    return None


async def _authenticated_config_request(
    hass,
    entry,
    method: str,
    path: str,
    **kwargs,
) -> httpx.Response:
    """Run an authenticated HTTP call against the backend from the
    config / options flow. Retries once with a fresh access token
    when the first attempt comes back 401 — same behaviour as the
    coordinator's `_authenticated_request`, just without the
    persistent `httpx.AsyncClient` (config-flow calls are rare and
    short-lived). Persists rotated tokens back into the config
    entry so the next call starts from the new pair."""
    api_url = entry.data[CONF_API_URL]
    access = entry.data[CONF_ACCESS_TOKEN]
    refresh = entry.data[CONF_REFRESH_TOKEN]

    async def _do(token: str) -> httpx.Response:
        async with httpx.AsyncClient(timeout=15.0) as client:
            return await client.request(
                method,
                f"{api_url}{path}",
                headers={"Authorization": f"Bearer {token}"},
                **kwargs,
            )

    response = await _do(access)
    if response.status_code == 401:
        rotated = await _refresh_token(api_url, refresh)
        if rotated is not None:
            new_access, new_refresh = rotated
            new_data = {
                **entry.data,
                CONF_ACCESS_TOKEN: new_access,
                CONF_REFRESH_TOKEN: new_refresh,
            }
            hass.config_entries.async_update_entry(entry, data=new_data)
            response = await _do(new_access)
    return response


async def _delete_device_backend(api_url: str, token: str, device_id: str) -> None:
    """Delete a device from the backend.

    Legacy single-token signature kept for the (small) number of
    call sites that haven't been routed through
    `_authenticated_config_request` yet. New call sites should use
    the entry-aware variant so 401s auto-refresh.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.delete(
            f"{api_url}/api/v1/devices/{device_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()


def _remove_ha_device(hass, device_id: str) -> None:
    """Drop the HA device-registry entry tied to this Crowdergy device.

    Without this, deleting via the options flow leaves orphaned devices in
    Home Assistant (entities go unavailable but the device card stays).
    """
    from homeassistant.helpers import device_registry as dr

    registry = dr.async_get(hass)
    ha_device = registry.async_get_device(identifiers={(DOMAIN, device_id)})
    if ha_device is not None:
        registry.async_remove_device(ha_device.id)


# ── Initial Config Flow ─────────────────────────────────────────────────────


class CrowdergyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Crowdergy Connector."""

    VERSION = 2

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._devices: list[dict[str, Any]] = []
        # Carry the device-type/name picked in step 1 into step 2.
        self._pending_type: str | None = None
        self._pending_name: str | None = None
        # v3.0 KonfigMode-Wahl aus Step 1b — entscheidet ob entity_*
        # einzeln (manual) oder via climate.* gebündelt (climate) sind.
        self._pending_config_mode: str = CONFIG_MODE_MANUAL
        # Entity-mapping kept between step 2 and step 3 (values).
        self._pending_entity_input: dict[str, Any] | None = None
        # v3.1 Auto-Setup state. Erkannte Geräte-Gruppen aus dem
        # entity_mapper-Scan + die FIFO der vom User confirm'd-Devices
        # die noch durch den klassischen Value-Step-Pfad müssen.
        self._auto_groups: list[DeviceGroup] = []
        self._auto_queue: list[dict[str, Any]] = []

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> CrowdergyOptionsFlow:
        return CrowdergyOptionsFlow(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            api_url = DEFAULT_API_URL
            email = user_input[CONF_EMAIL]
            password = user_input[CONF_PASSWORD]

            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    response = await client.post(
                        f"{api_url}/api/v1/auth/login",
                        json={"email": email, "password": password},
                    )
                    if response.status_code == 401:
                        errors["base"] = "invalid_auth"
                    elif response.status_code >= 400:
                        errors["base"] = "cannot_connect"
                    else:
                        tokens = response.json()
                        self._data[CONF_API_URL] = api_url
                        self._data[CONF_EMAIL] = email
                        self._data[CONF_ACCESS_TOKEN] = tokens["access_token"]
                        self._data[CONF_REFRESH_TOKEN] = tokens["refresh_token"]
                        self._data[CONF_USER_ID] = tokens.get("user_id", "")
            except httpx.RequestError:
                errors["base"] = "cannot_connect"

            if not errors:
                return await self.async_step_location()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    async def async_step_location(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data[CONF_DISTRICT] = user_input.get(CONF_DISTRICT, "")
            self._data[CONF_CITY] = user_input.get(CONF_CITY, "")
            self._data[CONF_REGION] = user_input.get(CONF_REGION, "")
            self._data[CONF_ENTITY_OUTDOOR_TEMP] = user_input.get(
                CONF_ENTITY_OUTDOOR_TEMP, ""
            )
            return await self.async_step_setup_mode()

        # Pre-fill from HA's configured coordinates so the user usually
        # just hits Submit. Resolved once per fresh location step; if the
        # user goes back and edits we keep whatever they typed.
        defaults = await _resolve_location_defaults(self.hass)
        return self.async_show_form(
            step_id="location",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_DISTRICT, default=defaults[CONF_DISTRICT]): str,
                    vol.Optional(CONF_CITY, default=defaults[CONF_CITY]): str,
                    vol.Optional(CONF_REGION, default=defaults[CONF_REGION]): str,
                    vol.Optional(CONF_ENTITY_OUTDOOR_TEMP): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="sensor")
                    ),
                }
            ),
        )

    # ── v3.1 Auto-Setup-Pfad ───────────────────────────────────────

    async def async_step_setup_mode(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Erster Schritt nach Login + Standort: Manuell vs Auto.

        Per ConfigEntry fix gewählt — wer den Modus später wechseln
        will, entfernt die Integration und legt sie neu an. Default
        ist Auto, weil das die deutlich angenehmere UX ist; Manuell
        bleibt als Escape-Hatch für komplexe Setups (Modbus-Custom-
        Sensoren etc.) wo die Heuristik nichts erkennt.
        """
        if user_input is not None:
            mode = user_input.get(CONF_SETUP_MODE, SETUP_MODE_AUTO)
            self._data[CONF_SETUP_MODE] = mode
            if mode == SETUP_MODE_AUTO:
                return await self.async_step_auto_discover()
            return await self.async_step_device_type()

        return self.async_show_form(
            step_id="setup_mode",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SETUP_MODE, default=SETUP_MODE_AUTO): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                {"value": SETUP_MODE_AUTO, "label": "Auto-Setup (Crowdergy erkennt deine Geräte)"},
                                {"value": SETUP_MODE_MANUAL, "label": "Manuelles Setup (alles selbst auswählen)"},
                            ],
                            mode=selector.SelectSelectorMode.LIST,
                            translation_key=CONF_SETUP_MODE,
                        )
                    ),
                }
            ),
        )

    async def async_step_auto_discover(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Scant die HA-Instanz, klassifiziert + gruppiert Entities zu
        Crowdergy-Geräte-Vorschlägen. Findet die Heuristik nichts,
        fallen wir auf den manuellen Pfad zurück — sonst weiter zur
        Confirm-Page.
        """
        try:
            if MAPPING_LLM_ENABLED:
                groups = await discover_devices_with_llm(
                    self.hass,
                    self._data[CONF_API_URL],
                    self._data[CONF_ACCESS_TOKEN],
                    USER_AGENT,
                )
            else:
                groups = await discover_devices(self.hass)
        except Exception:   # noqa: BLE001 — Heuristik darf den Flow nie blockieren
            _LOGGER.exception("Auto-Discover failed, falling back to manual flow")
            groups = []

        # Sicherheits-Filter: nur Gruppen mit min. einem Slot anzeigen,
        # alles andere ist nur Rauschen.
        groups = [g for g in groups if g.slot_map()]
        self._auto_groups = groups

        if not groups:
            return await self.async_step_device_type()
        return await self.async_step_auto_confirm()

    # Slot-Set das die Auto-Confirm-Form rendert + parsed. Eine Quelle
    # — sonst stiftet ein Schema-Loop ohne match im Default-Loop einen
    # „extra keys not allowed"-Voluptuous-Fehler. Power-2 + Discharged
    # sind drin damit Hersteller-spezifische Zweit-Sensoren (Sonnen-
    # Charge/Discharge, Grid-Einspeisung) im UI sichtbar sind.
    _AUTO_SLOTS = (
        CONF_ENTITY_POWER,
        CONF_ENTITY_POWER_2,
        CONF_ENTITY_ENERGY_TOTAL,
        CONF_ENTITY_ENERGY_DISCHARGED_TOTAL,
        CONF_ENTITY_SOC,
        CONF_ENTITY_CURRENT_TEMP,
        CONF_ENTITY_VEHICLE_STATUS,
        CONF_ENTITY_CONTROL,
        CONF_ENTITY_CHARGE_MODE,
        CONF_ENTITY_CLIMATE,
        CONF_ENTITY_WATER_HEATER,
    )

    async def async_step_auto_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Listet alle erkannten Gruppen mit pre-filled Entity-Slots.
        User editiert / bestätigt; auf Submit wandern alle aktivierten
        Gruppen in `_auto_queue`. Anschließend läuft pro Gruppe der
        klassische `_dispatch_post_entities`-Pfad — so kommen die Mode-
        Values-Steps (charge_mode, battery, vehicle_status, value_on)
        gewohnt durch, nur die Entity-Auswahl ist eben vorbelegt.
        """
        # Stable Section-Keys: nach v3.1.1 max. 1 Gruppe pro Crowdergy-
        # Typ — die Sections heißen einfach nach dem Typ. Das matched
        # die statisch in strings.json hinterlegten Section-Labels.
        groups_by_type: dict[str, DeviceGroup] = {
            g.suggested_type: g for g in self._auto_groups
        }

        if user_input is not None:
            for dtype, group in groups_by_type.items():
                section_data = user_input.get(dtype, {})
                if not section_data:
                    continue
                if not section_data.get(f"{dtype}_include", True):
                    continue
                entity_input: dict[str, Any] = {}
                for slot in self._AUTO_SLOTS:
                    val = section_data.get(f"{dtype}_{slot}", "")
                    if val:
                        entity_input[slot] = val
                config_mode = (
                    CONFIG_MODE_CLIMATE
                    if entity_input.get(CONF_ENTITY_CLIMATE)
                       or entity_input.get(CONF_ENTITY_WATER_HEATER)
                    else CONFIG_MODE_MANUAL
                )
                self._auto_queue.append(
                    {
                        "device_type": section_data.get(
                            f"{dtype}_type", group.suggested_type
                        ),
                        "device_name": section_data.get(
                            f"{dtype}_name", group.suggested_name
                        ),
                        "config_mode": config_mode,
                        "entity_input": entity_input,
                    }
                )
            return await self._auto_process_next()

        # Form-Schema bauen: eine vol.section pro Crowdergy-Typ. Die
        # Section-Header kommen aus strings.json (statisch pro Typ);
        # die Confidence + der HA-Device-Name landen oben im Form-
        # Description-Block via `{summary}`-Placeholder. So sieht der
        # User auf einen Blick welche Gruppe mit welcher Confidence
        # erkannt wurde, bevor er ins Detail klappt.
        schema_dict: dict[Any, Any] = {}
        summary_lines: list[str] = []
        # Pro-Section-Description-Placeholder. `solar_conf` / `battery_
        # _conf` etc. werden in strings.json sections.<type>.description
        # referenziert, sodass die Confidence direkt unter der Section-
        # Überschrift steht (sichtbarer als nur oben in der Summary).
        section_placeholders: dict[str, str] = {
            f"{dtype}_conf": "—" for dtype in DEVICE_TYPES
        }

        for dtype, group in groups_by_type.items():
            slot_map = group.slot_map()
            section_schema: dict[Any, Any] = {
                vol.Optional(f"{dtype}_include", default=True): bool,
                vol.Required(
                    f"{dtype}_name", default=group.suggested_name
                ): str,
                vol.Required(
                    f"{dtype}_type", default=group.suggested_type
                ): vol.In(DEVICE_TYPES),
            }
            section_defaults: dict[str, Any] = {
                f"{dtype}_include": True,
                f"{dtype}_name": group.suggested_name,
                f"{dtype}_type": group.suggested_type,
            }
            for slot in self._AUTO_SLOTS:
                slot_key = f"{dtype}_{slot}"
                heuristic_pick = slot_map.get(slot)
                # vol.Optional ohne default — voluptuous-EntitySelector
                # akzeptiert leere Strings nicht als „nicht gewählt".
                # Wenn die Heuristik einen Pick hat, geht der ins
                # section_defaults; sonst bleibt der Slot leer und
                # taucht erst gar nicht im default-Dict auf.
                section_schema[vol.Optional(slot_key)] = selector.EntitySelector(
                    selector.EntitySelectorConfig(multiple=False)
                )
                if heuristic_pick:
                    section_defaults[slot_key] = heuristic_pick
            schema_dict[
                vol.Optional(dtype, default=section_defaults)
            ] = section(vol.Schema(section_schema), {"collapsed": False})

            confidence_pct = int(round(group.avg_confidence * 100))
            marker = "✓" if group.avg_confidence >= HEURISTIC_ACCEPT else "?"
            type_label = DEVICE_TYPE_LABELS_DE.get(dtype, dtype)
            summary_lines.append(
                f"{marker} **{type_label}** — {group.suggested_name} "
                f"({confidence_pct}%)"
            )
            section_placeholders[f"{dtype}_conf"] = (
                f"{marker} {group.suggested_name} · {confidence_pct}%"
            )

        return self.async_show_form(
            step_id="auto_confirm",
            data_schema=vol.Schema(schema_dict),
            description_placeholders={
                "device_count": str(len(self._auto_groups)),
                "summary": "\n\n".join(summary_lines),
                **section_placeholders,
            },
        )

    async def _auto_process_next(self) -> ConfigFlowResult:
        """Pop des nächsten Auto-Devices aus der Queue und weiter durch
        den klassischen Per-Device-Value-Step-Pfad. Queue leer →
        finish.
        """
        if not self._auto_queue:
            return await self.async_step_finish()
        pending = self._auto_queue.pop(0)
        self._pending_type = pending["device_type"]
        self._pending_name = pending["device_name"]
        self._pending_config_mode = pending.get("config_mode", CONFIG_MODE_MANUAL)
        entity_input = pending.get("entity_input", {})
        # Dispatch geht durch dieselben Value-Steps wie der Manuell-
        # Flow — Mode-Werte (Lock/Solar/Power, charge/idle/discharge,
        # plugged/unplugged, value_on/value_off) werden gewohnt
        # abgefragt; nur die Entity-Auswahl ist aus der Heuristik
        # vorbelegt. Nach _register_with_entities landet der Flow in
        # `async_step_add_more` — den hooken wir unten so dass Auto-
        # Mode dort direkt das nächste Queue-Element nachzieht.
        return await self._dispatch_post_entities(entity_input)

    async def async_step_device_type(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: pick the device type + name."""
        if user_input is not None:
            self._pending_type = user_input[CONF_DEVICE_TYPE]
            self._pending_name = user_input[CONF_DEVICE_NAME]
            # v3.0: WP-Typen (heating, warmwater) bekommen einen
            # KonfigMode-Step danach. Andere Typen skippen direkt zu
            # device_entities mit implizitem config_mode = manual.
            if self._pending_type in {"heating", "warmwater"}:
                return await self.async_step_device_config_mode()
            self._pending_config_mode = CONFIG_MODE_MANUAL
            return await self.async_step_device_entities()

        return self.async_show_form(
            step_id="device_type",
            data_schema=_type_name_schema(),
            description_placeholders={"device_number": str(len(self._devices) + 1)},
        )

    async def async_step_device_config_mode(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1b (heating/warmwater only): KonfigMode-Picker.
        Manuell = klassisch alle Entities einzeln. Climate-Entity =
        moderne climate.* Integration, Steuerung + Modi + Ist-Temp
        kommen automatisch. Edit-Flow skippt diesen Step — wer den
        Modus wechseln will, entfernt das Gerät und legt's neu an.
        """
        if user_input is not None:
            self._pending_config_mode = user_input.get(
                CONF_DEVICE_CONFIG_MODE, CONFIG_MODE_MANUAL
            )
            return await self.async_step_device_entities()

        return self.async_show_form(
            step_id="device_config_mode",
            data_schema=_config_mode_schema(),
            description_placeholders={
                "device_type": DEVICE_TYPE_LABELS_DE.get(
                    self._pending_type or "", self._pending_type or ""
                ),
                "device_name": self._pending_name or "",
            },
        )

    async def async_step_device_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: type-specific entity mapping."""
        device_type = self._pending_type or "generic"
        device_name = self._pending_name or ""

        if user_input is not None:
            entity_input = _apply_climate_first(
                _flatten_sections(user_input)
            )
            return await self._dispatch_post_entities(entity_input)

        return self.async_show_form(
            step_id="device_entities",
            data_schema=_entities_schema(device_type, config_mode=self._pending_config_mode),
            description_placeholders={
                "device_number": str(len(self._devices) + 1),
                "device_type": DEVICE_TYPE_LABELS_DE.get(device_type, device_type),
                "device_name": device_name,
            },
        )

    async def _dispatch_post_entities(
        self, entity_input: dict[str, Any]
    ) -> ConfigFlowResult:
        """Decide which follow-up step (if any) to render next.

        Order:
          1. Charge-mode-values ternary mapping (Aus / An / Solaroptimiert)
                                            — wallbox + entity_charge_mode set
          2. Vehicle-status ternary mapping  — wallbox + entity_vehicle_status set
          3. value_on / value_off           — controllable + non-binary control
          4. Shares-hardware picker         — warmwater
          5. Otherwise: register the device

        value_on / value_off is entity-level config (defines what the
        connector writes to `entity_control`) and is a precondition
        for ANY on/off automation. Shares-hardware is a household
        coupling that only affects the joint MILP. Asking values
        first keeps setup-flow semantics top-down (entity → coupling
        → register) and surfaces the values step for warmwater
        devices, which used to be skipped over by the shares step's
        early return on its second visit.

        Each branch stashes the (in-progress) entity_input on
        `self._pending_entity_input` so the next step can pick up
        where this one left off.
        """
        device_type = self._pending_type or "generic"

        if (
            device_type == "wallbox"
            and entity_input.get(CONF_ENTITY_CHARGE_MODE)
            and CONF_CHARGE_MODE_VALUE_LOCK not in entity_input
        ):
            self._pending_entity_input = entity_input
            return await self.async_step_device_charge_mode_values()

        if (
            device_type == "battery"
            and entity_input.get(CONF_ENTITY_CHARGE_MODE)
            and CONF_BATTERY_VALUE_CHARGE not in entity_input
        ):
            self._pending_entity_input = entity_input
            return await self.async_step_device_battery_values()

        if (
            device_type == "wallbox"
            and entity_input.get(CONF_ENTITY_VEHICLE_STATUS)
            and CONF_VEHICLE_STATUS_VALUE_PLUGGED not in entity_input
        ):
            # Binary-Sensor-Pfad: on/off ist die einzige sinnvolle
            # Belegung — auto-mappen und Step skippen.
            _auto_fill_binary_vehicle_status(entity_input)
            if CONF_VEHICLE_STATUS_VALUE_PLUGGED not in entity_input:
                self._pending_entity_input = entity_input
                return await self.async_step_device_vehicle_status()

        entity_control = entity_input.get(CONF_ENTITY_CONTROL, "")
        if (
            device_type in _CONTROLLABLE_TYPES
            and entity_control
            and not _is_binary_entity(entity_control)
            and CONF_VALUE_ON not in entity_input
        ):
            self._pending_entity_input = entity_input
            return await self.async_step_device_values()

        if (
            device_type == "warmwater"
            and CONF_SHARES_HARDWARE_WITH not in entity_input
        ):
            self._pending_entity_input = entity_input
            return await self.async_step_device_shares_hardware()

        # v3.0: legacy included_in_haushalt + cooling steps werden nicht
        # mehr dispatched — beide Felder werden inline im device_entities
        # bzw. device_values Step erfasst. Wenn die Schlüssel nicht in
        # entity_input liegen, ist das v3.0-konformes "kein cooling" /
        # "default-haushalt-flag" — nicht Anlass für einen extra Step.
        return await self._register_with_entities(entity_input)

    async def async_step_device_charge_mode_values(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """v2.2 step: map the wallbox's HA charge-mode select-options
        to the three Crowdergy modes (Aus / An / Solaroptimiert).
        Each field is optional — modes left blank simply don't get a
        button in the iOS tile.
        """
        device_type = self._pending_type or "generic"
        device_name = self._pending_name or ""
        entity_input = dict(self._pending_entity_input or {})
        entity_charge_mode = entity_input.get(CONF_ENTITY_CHARGE_MODE, "")

        if user_input is not None:
            entity_input[CONF_CHARGE_MODE_VALUE_LOCK] = user_input.get(
                CONF_CHARGE_MODE_VALUE_LOCK, ""
            )
            entity_input[CONF_CHARGE_MODE_VALUE_POWER] = user_input.get(
                CONF_CHARGE_MODE_VALUE_POWER, ""
            )
            entity_input[CONF_CHARGE_MODE_VALUE_SOLAR] = user_input.get(
                CONF_CHARGE_MODE_VALUE_SOLAR, ""
            )
            entity_input[CONF_ENTITY_CONTROL_HOLD] = user_input.get(
                CONF_ENTITY_CONTROL_HOLD, ENTITY_CONTROL_HOLD_AUTO
            )
            return await self._dispatch_post_entities(entity_input)

        return self.async_show_form(
            step_id="device_charge_mode_values",
            data_schema=_charge_mode_values_schema(
                self.hass, entity_charge_mode, {}
            ),
            description_placeholders={
                "device_type": DEVICE_TYPE_LABELS_DE.get(device_type, device_type),
                "device_name": device_name,
                "entity_charge_mode": entity_charge_mode,
            },
        )

    async def async_step_device_battery_values(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """v2.3 step: map the battery's 4-mode dispatch to the values
        the connector writes to its entity_charge_mode entity. Three
        fields (Laden / Aus / Entladen) — modes left blank get
        suppressed in the worker dispatch (battery stays passive for
        that branch).
        """
        device_type = self._pending_type or "generic"
        device_name = self._pending_name or ""
        entity_input = dict(self._pending_entity_input or {})
        entity_charge_mode = entity_input.get(CONF_ENTITY_CHARGE_MODE, "")

        if user_input is not None:
            entity_input[CONF_BATTERY_VALUE_CHARGE] = user_input.get(
                CONF_BATTERY_VALUE_CHARGE, ""
            )
            entity_input[CONF_BATTERY_VALUE_IDLE] = user_input.get(
                CONF_BATTERY_VALUE_IDLE, ""
            )
            entity_input[CONF_BATTERY_VALUE_DISCHARGE] = user_input.get(
                CONF_BATTERY_VALUE_DISCHARGE, ""
            )
            entity_input[CONF_BATTERY_VALUE_PASSIVE] = user_input.get(
                CONF_BATTERY_VALUE_PASSIVE, ""
            )
            entity_input[CONF_ENTITY_CONTROL_HOLD] = user_input.get(
                CONF_ENTITY_CONTROL_HOLD, ENTITY_CONTROL_HOLD_AUTO
            )
            return await self._dispatch_post_entities(entity_input)

        return self.async_show_form(
            step_id="device_battery_values",
            data_schema=_battery_values_schema(
                self.hass, entity_charge_mode, {}
            ),
            description_placeholders={
                "device_type": DEVICE_TYPE_LABELS_DE.get(device_type, device_type),
                "device_name": device_name,
                "entity_charge_mode": entity_charge_mode,
            },
        )

    async def async_step_device_vehicle_status(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """v2.0 step: map the wallbox's vehicle-status sensor states
        to the normalised plugged / unplugged / error trio."""
        device_type = self._pending_type or "generic"
        device_name = self._pending_name or ""
        entity_input = dict(self._pending_entity_input or {})
        entity_vehicle_status = entity_input.get(CONF_ENTITY_VEHICLE_STATUS, "")

        if user_input is not None:
            entity_input[CONF_VEHICLE_STATUS_VALUE_PLUGGED] = user_input.get(
                CONF_VEHICLE_STATUS_VALUE_PLUGGED, ""
            )
            entity_input[CONF_VEHICLE_STATUS_VALUE_UNPLUGGED] = user_input.get(
                CONF_VEHICLE_STATUS_VALUE_UNPLUGGED, ""
            )
            entity_input[CONF_VEHICLE_STATUS_VALUE_ERROR] = user_input.get(
                CONF_VEHICLE_STATUS_VALUE_ERROR, ""
            )
            return await self._dispatch_post_entities(entity_input)

        return self.async_show_form(
            step_id="device_vehicle_status",
            data_schema=_vehicle_status_schema(
                self.hass, entity_vehicle_status, {}
            ),
            description_placeholders={
                "device_type": DEVICE_TYPE_LABELS_DE.get(device_type, device_type),
                "device_name": device_name,
                "entity_vehicle_status": entity_vehicle_status,
            },
        )

    async def async_step_device_shares_hardware(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """v2.0 step: link a warmwater device to its sibling heating
        device on the same compressor (optional)."""
        device_type = self._pending_type or "generic"
        device_name = self._pending_name or ""
        entity_input = dict(self._pending_entity_input or {})

        heating_devices = [
            {"id": d.get(CONF_DEVICE_ID, ""), "name": d.get(CONF_DEVICE_NAME, "")}
            for d in self._devices
            if d.get(CONF_DEVICE_TYPE) == "heating"
        ]

        if user_input is not None:
            entity_input[CONF_SHARES_HARDWARE_WITH] = user_input.get(
                CONF_SHARES_HARDWARE_WITH, ""
            )
            return await self._dispatch_post_entities(entity_input)

        return self.async_show_form(
            step_id="device_shares_hardware",
            data_schema=_shares_hardware_schema(heating_devices, {}),
            description_placeholders={
                "device_type": DEVICE_TYPE_LABELS_DE.get(device_type, device_type),
                "device_name": device_name,
            },
        )

    async def async_step_device_values(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 3: typ-bewusste value_on / value_off für entity_control.
        v3.0 heating + climate-Mode: value_cool_on/off direkt inline
        statt eigenem Cooling-Step. Leer = kein Cooling.
        """
        device_type = self._pending_type or "generic"
        device_name = self._pending_name or ""
        entity_input = dict(self._pending_entity_input or {})
        entity_control = entity_input.get(CONF_ENTITY_CONTROL, "")
        include_cooling = (
            device_type == "heating"
            and self._pending_config_mode == CONFIG_MODE_CLIMATE
        )

        if user_input is not None:
            entity_input[CONF_VALUE_ON] = user_input.get(CONF_VALUE_ON, "")
            entity_input[CONF_VALUE_OFF] = user_input.get(CONF_VALUE_OFF, "")
            if include_cooling:
                entity_input[CONF_VALUE_COOL_ON] = user_input.get(
                    CONF_VALUE_COOL_ON, ""
                )
            entity_input[CONF_ENTITY_CONTROL_HOLD] = user_input.get(
                CONF_ENTITY_CONTROL_HOLD, ENTITY_CONTROL_HOLD_AUTO
            )
            return await self._dispatch_post_entities(entity_input)

        return self.async_show_form(
            step_id="device_values",
            data_schema=_values_schema(
                self.hass, entity_control, {},
                include_cooling=include_cooling,
            ),
            description_placeholders={
                "device_type": DEVICE_TYPE_LABELS_DE.get(device_type, device_type),
                "device_name": device_name,
                "entity_control": entity_control,
            },
        )

    async def _register_with_entities(
        self, entity_input: dict[str, Any]
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        device_type = self._pending_type or "generic"
        device_name = self._pending_name or ""
        # v3.0: ConfigMode aus dem Pending-State ins entity_input
        # einbringen, sodass _build_device_record es persistiert.
        entity_input.setdefault(
            CONF_DEVICE_CONFIG_MODE, self._pending_config_mode
        )
        try:
            dev = await _register_device(
                self._data[CONF_API_URL],
                self._data[CONF_ACCESS_TOKEN],
                device_type,
                device_name,
                entity_input,
                self._data,
            )
            self._devices.append(dev)
            self._pending_type = None
            self._pending_name = None
            self._pending_config_mode = CONFIG_MODE_MANUAL
            self._pending_entity_input = None
            return await self.async_step_add_more()
        except (httpx.HTTPStatusError, httpx.RequestError) as err:
            _LOGGER.error("Failed to register device: %s", err)
            errors["base"] = "cannot_connect"
            return self.async_show_form(
                step_id="device_entities",
                data_schema=_entities_schema(device_type, config_mode=self._pending_config_mode),
                description_placeholders={
                    "device_number": str(len(self._devices) + 1),
                    "device_type": DEVICE_TYPE_LABELS_DE.get(device_type, device_type),
                    "device_name": device_name,
                },
                errors=errors,
            )

    async def async_step_add_more(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        # Auto-Mode walking durch die Queue — solange noch was offen
        # ist, sofort weiter mit dem nächsten Device statt den Menu-
        # Dialog zu zeigen. Erst wenn die Queue leer ist gibt's das
        # gewohnte „weiter hinzufügen / fertig" Menü.
        if self._auto_queue:
            return await self._auto_process_next()
        return self.async_show_menu(
            step_id="add_more",
            menu_options=["device_type", "finish"],
        )

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._data[CONF_DEVICES] = self._devices
        title = f"Crowdergy ({len(self._devices)} Geräte)"
        return self.async_create_entry(title=title, data=self._data)


# ── Options Flow (add / edit / remove devices after setup) ──────────────────


class CrowdergyOptionsFlow(OptionsFlow):
    """Handle options for Crowdergy Connector."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry
        self._devices: list[dict[str, Any]] = list(
            config_entry.data.get(CONF_DEVICES, [])
        )
        # Add-flow scratch state.
        self._pending_type: str | None = None
        self._pending_name: str | None = None
        self._pending_config_mode: str = CONFIG_MODE_MANUAL
        self._pending_entity_input: dict[str, Any] | None = None
        # Edit-flow scratch state.
        self._edit_target_id: str | None = None
        self._edit_pending_type: str | None = None
        self._edit_pending_name: str | None = None
        self._edit_pending_entity_input: dict[str, Any] | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "add_device",
                "edit_device",
                "remove_device",
                "edit_outdoor_temp",
                "done",
            ],
        )

    async def async_step_edit_outdoor_temp(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add or change the integration-wide outdoor-temperature sensor.
        Stored at the top of entry.data, persisted by async_step_done.
        Leaving the field empty clears the mapping → backend falls
        back to Open-Meteo for this user.
        """
        if user_input is not None:
            new_value = user_input.get(CONF_ENTITY_OUTDOOR_TEMP, "")
            new_data = {**self._entry.data, CONF_ENTITY_OUTDOOR_TEMP: new_value}
            self.hass.config_entries.async_update_entry(self._entry, data=new_data)
            return await self.async_step_init()

        current = self._entry.data.get(CONF_ENTITY_OUTDOOR_TEMP, "")
        field: Any = (
            vol.Optional(CONF_ENTITY_OUTDOOR_TEMP, default=current)
            if current
            else vol.Optional(CONF_ENTITY_OUTDOOR_TEMP)
        )
        return self.async_show_form(
            step_id="edit_outdoor_temp",
            data_schema=vol.Schema(
                {
                    field: selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="sensor")
                    ),
                }
            ),
        )

    # ── Add-Device (two-step) ───────────────────────────────────────────────

    async def async_step_add_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1 (add): pick type + name."""
        if user_input is not None:
            self._pending_type = user_input[CONF_DEVICE_TYPE]
            self._pending_name = user_input[CONF_DEVICE_NAME]
            if self._pending_type in {"heating", "warmwater"}:
                return await self.async_step_add_device_config_mode()
            self._pending_config_mode = CONFIG_MODE_MANUAL
            return await self.async_step_add_device_entities()

        return self.async_show_form(
            step_id="add_device",
            data_schema=_type_name_schema(),
        )

    async def async_step_add_device_config_mode(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """v3.0 add-flow Variante des KonfigMode-Pickers."""
        if user_input is not None:
            self._pending_config_mode = user_input.get(
                CONF_DEVICE_CONFIG_MODE, CONFIG_MODE_MANUAL
            )
            return await self.async_step_add_device_entities()

        return self.async_show_form(
            step_id="add_device_config_mode",
            data_schema=_config_mode_schema(),
            description_placeholders={
                "device_type": DEVICE_TYPE_LABELS_DE.get(
                    self._pending_type or "", self._pending_type or ""
                ),
                "device_name": self._pending_name or "",
            },
        )

    async def async_step_add_device_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2 (add): type-specific entity mapping."""
        device_type = self._pending_type or "generic"
        device_name = self._pending_name or ""

        if user_input is not None:
            entity_input = _apply_climate_first(
                _flatten_sections(user_input)
            )
            return await self._dispatch_add_post_entities(entity_input)

        return self.async_show_form(
            step_id="add_device_entities",
            data_schema=_entities_schema(device_type, config_mode=self._pending_config_mode),
            description_placeholders={
                "device_type": DEVICE_TYPE_LABELS_DE.get(device_type, device_type),
                "device_name": device_name,
            },
        )

    async def _dispatch_add_post_entities(
        self, entity_input: dict[str, Any]
    ) -> ConfigFlowResult:
        """Options-flow add path: same routing logic as the main
        config flow's `_dispatch_post_entities` (charge-mode-values →
        vehicle-status → values → shares-hardware → register).
        Values runs before shares-hardware because it's entity-level
        config, while shares-hardware is a household-level coupling."""
        device_type = self._pending_type or "generic"

        if (
            device_type == "wallbox"
            and entity_input.get(CONF_ENTITY_CHARGE_MODE)
            and CONF_CHARGE_MODE_VALUE_LOCK not in entity_input
        ):
            self._pending_entity_input = entity_input
            return await self.async_step_add_device_charge_mode_values()

        if (
            device_type == "battery"
            and entity_input.get(CONF_ENTITY_CHARGE_MODE)
            and CONF_BATTERY_VALUE_CHARGE not in entity_input
        ):
            self._pending_entity_input = entity_input
            return await self.async_step_add_device_battery_values()

        if (
            device_type == "wallbox"
            and entity_input.get(CONF_ENTITY_VEHICLE_STATUS)
            and CONF_VEHICLE_STATUS_VALUE_PLUGGED not in entity_input
        ):
            _auto_fill_binary_vehicle_status(entity_input)
            if CONF_VEHICLE_STATUS_VALUE_PLUGGED not in entity_input:
                self._pending_entity_input = entity_input
                return await self.async_step_add_device_vehicle_status()

        entity_control = entity_input.get(CONF_ENTITY_CONTROL, "")
        if (
            device_type in _CONTROLLABLE_TYPES
            and entity_control
            and not _is_binary_entity(entity_control)
            and CONF_VALUE_ON not in entity_input
        ):
            self._pending_entity_input = entity_input
            return await self.async_step_add_device_values()

        if (
            device_type == "warmwater"
            and CONF_SHARES_HARDWARE_WITH not in entity_input
        ):
            self._pending_entity_input = entity_input
            return await self.async_step_add_device_shares_hardware()

        # v3.0: legacy included_in_haushalt + cooling steps entfernt —
        # beide Felder werden inline erfasst (siehe initial-Setup-Flow).
        return await self._options_register(entity_input)

    async def async_step_add_device_charge_mode_values(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Options-flow variant of the wallbox Lademodus-Werte mapping
        (Aus / An / Solaroptimiert → wallbox HA select-options)."""
        device_type = self._pending_type or "generic"
        device_name = self._pending_name or ""
        entity_input = dict(self._pending_entity_input or {})
        entity_charge_mode = entity_input.get(CONF_ENTITY_CHARGE_MODE, "")

        if user_input is not None:
            entity_input[CONF_CHARGE_MODE_VALUE_LOCK] = user_input.get(
                CONF_CHARGE_MODE_VALUE_LOCK, ""
            )
            entity_input[CONF_CHARGE_MODE_VALUE_POWER] = user_input.get(
                CONF_CHARGE_MODE_VALUE_POWER, ""
            )
            entity_input[CONF_CHARGE_MODE_VALUE_SOLAR] = user_input.get(
                CONF_CHARGE_MODE_VALUE_SOLAR, ""
            )
            entity_input[CONF_ENTITY_CONTROL_HOLD] = user_input.get(
                CONF_ENTITY_CONTROL_HOLD, ENTITY_CONTROL_HOLD_AUTO
            )
            return await self._dispatch_add_post_entities(entity_input)

        return self.async_show_form(
            step_id="add_device_charge_mode_values",
            data_schema=_charge_mode_values_schema(
                self.hass, entity_charge_mode, {}
            ),
            description_placeholders={
                "device_type": DEVICE_TYPE_LABELS_DE.get(device_type, device_type),
                "device_name": device_name,
                "entity_charge_mode": entity_charge_mode,
            },
        )

    async def async_step_add_device_battery_values(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Options-flow variant of the battery 4-mode mapping
        (Laden / Aus / Entladen → entity-write values)."""
        device_type = self._pending_type or "generic"
        device_name = self._pending_name or ""
        entity_input = dict(self._pending_entity_input or {})
        entity_charge_mode = entity_input.get(CONF_ENTITY_CHARGE_MODE, "")

        if user_input is not None:
            entity_input[CONF_BATTERY_VALUE_CHARGE] = user_input.get(
                CONF_BATTERY_VALUE_CHARGE, ""
            )
            entity_input[CONF_BATTERY_VALUE_IDLE] = user_input.get(
                CONF_BATTERY_VALUE_IDLE, ""
            )
            entity_input[CONF_BATTERY_VALUE_DISCHARGE] = user_input.get(
                CONF_BATTERY_VALUE_DISCHARGE, ""
            )
            entity_input[CONF_BATTERY_VALUE_PASSIVE] = user_input.get(
                CONF_BATTERY_VALUE_PASSIVE, ""
            )
            entity_input[CONF_ENTITY_CONTROL_HOLD] = user_input.get(
                CONF_ENTITY_CONTROL_HOLD, ENTITY_CONTROL_HOLD_AUTO
            )
            return await self._dispatch_add_post_entities(entity_input)

        return self.async_show_form(
            step_id="add_device_battery_values",
            data_schema=_battery_values_schema(
                self.hass, entity_charge_mode, {}
            ),
            description_placeholders={
                "device_type": DEVICE_TYPE_LABELS_DE.get(device_type, device_type),
                "device_name": device_name,
                "entity_charge_mode": entity_charge_mode,
            },
        )

    async def async_step_add_device_vehicle_status(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Options-flow variant of the wallbox vehicle-status mapping."""
        device_type = self._pending_type or "generic"
        device_name = self._pending_name or ""
        entity_input = dict(self._pending_entity_input or {})
        entity_vehicle_status = entity_input.get(CONF_ENTITY_VEHICLE_STATUS, "")

        if user_input is not None:
            entity_input[CONF_VEHICLE_STATUS_VALUE_PLUGGED] = user_input.get(
                CONF_VEHICLE_STATUS_VALUE_PLUGGED, ""
            )
            entity_input[CONF_VEHICLE_STATUS_VALUE_UNPLUGGED] = user_input.get(
                CONF_VEHICLE_STATUS_VALUE_UNPLUGGED, ""
            )
            entity_input[CONF_VEHICLE_STATUS_VALUE_ERROR] = user_input.get(
                CONF_VEHICLE_STATUS_VALUE_ERROR, ""
            )
            return await self._dispatch_add_post_entities(entity_input)

        return self.async_show_form(
            step_id="add_device_vehicle_status",
            data_schema=_vehicle_status_schema(
                self.hass, entity_vehicle_status, {}
            ),
            description_placeholders={
                "device_type": DEVICE_TYPE_LABELS_DE.get(device_type, device_type),
                "device_name": device_name,
                "entity_vehicle_status": entity_vehicle_status,
            },
        )

    async def async_step_add_device_shares_hardware(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Options-flow variant of the warmwater shares-hardware picker."""
        device_type = self._pending_type or "generic"
        device_name = self._pending_name or ""
        entity_input = dict(self._pending_entity_input or {})

        heating_devices = [
            {"id": d.get(CONF_DEVICE_ID, ""), "name": d.get(CONF_DEVICE_NAME, "")}
            for d in self._devices
            if d.get(CONF_DEVICE_TYPE) == "heating"
        ]

        if user_input is not None:
            entity_input[CONF_SHARES_HARDWARE_WITH] = user_input.get(
                CONF_SHARES_HARDWARE_WITH, ""
            )
            return await self._dispatch_add_post_entities(entity_input)

        return self.async_show_form(
            step_id="add_device_shares_hardware",
            data_schema=_shares_hardware_schema(heating_devices, {}),
            description_placeholders={
                "device_type": DEVICE_TYPE_LABELS_DE.get(device_type, device_type),
                "device_name": device_name,
            },
        )

    async def async_step_add_device_values(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 3 (add): typ-bewusste value_on / value_off. v3.0 inline
        Cooling für heating+climate (siehe device_values)."""
        device_type = self._pending_type or "generic"
        device_name = self._pending_name or ""
        entity_input = dict(self._pending_entity_input or {})
        entity_control = entity_input.get(CONF_ENTITY_CONTROL, "")
        include_cooling = (
            device_type == "heating"
            and self._pending_config_mode == CONFIG_MODE_CLIMATE
        )

        if user_input is not None:
            entity_input[CONF_VALUE_ON] = user_input.get(CONF_VALUE_ON, "")
            entity_input[CONF_VALUE_OFF] = user_input.get(CONF_VALUE_OFF, "")
            if include_cooling:
                entity_input[CONF_VALUE_COOL_ON] = user_input.get(
                    CONF_VALUE_COOL_ON, ""
                )
            entity_input[CONF_ENTITY_CONTROL_HOLD] = user_input.get(
                CONF_ENTITY_CONTROL_HOLD, ENTITY_CONTROL_HOLD_AUTO
            )
            return await self._dispatch_add_post_entities(entity_input)

        return self.async_show_form(
            step_id="add_device_values",
            data_schema=_values_schema(
                self.hass, entity_control, {},
                include_cooling=include_cooling,
            ),
            description_placeholders={
                "device_type": DEVICE_TYPE_LABELS_DE.get(device_type, device_type),
                "device_name": device_name,
                "entity_control": entity_control,
            },
        )

    async def _options_register(
        self, entity_input: dict[str, Any]
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        device_type = self._pending_type or "generic"
        device_name = self._pending_name or ""
        entity_input.setdefault(
            CONF_DEVICE_CONFIG_MODE, self._pending_config_mode
        )
        try:
            dev = await _register_device(
                self._entry.data[CONF_API_URL],
                self._entry.data[CONF_ACCESS_TOKEN],
                device_type,
                device_name,
                entity_input,
                self._entry.data,
            )
            self._devices.append(dev)
            self._pending_type = None
            self._pending_name = None
            self._pending_config_mode = CONFIG_MODE_MANUAL
            self._pending_entity_input = None
            return await self.async_step_init()
        except (httpx.HTTPStatusError, httpx.RequestError) as err:
            _LOGGER.error("Failed to register device: %s", err)
            errors["base"] = "cannot_connect"
            return self.async_show_form(
                step_id="add_device_entities",
                data_schema=_entities_schema(device_type, config_mode=self._pending_config_mode),
                description_placeholders={
                    "device_type": DEVICE_TYPE_LABELS_DE.get(device_type, device_type),
                    "device_name": device_name,
                },
                errors=errors,
            )

    # ── Edit-Device (two-step, defaults pre-filled) ─────────────────────────

    async def async_step_edit_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick which device to edit."""
        if not self._devices:
            return await self.async_step_init()

        if user_input is not None:
            self._edit_target_id = user_input["device_to_edit"]
            # v3.0: kein type-name Step mehr auf Edit. Typ ändert sich
            # eh nicht; Name ist auf der Entities-Seite editierbar.
            target = next(
                (d for d in self._devices
                 if d.get(CONF_DEVICE_ID) == self._edit_target_id),
                None,
            )
            if target is not None:
                self._edit_pending_type = target.get(CONF_DEVICE_TYPE)
                self._edit_pending_name = target.get(CONF_DEVICE_NAME)
            return await self.async_step_edit_device_entities()

        options = [
            selector.SelectOptionDict(
                value=d[CONF_DEVICE_ID],
                label=f"{d[CONF_DEVICE_NAME]} ({DEVICE_TYPE_LABELS_DE.get(d[CONF_DEVICE_TYPE], d[CONF_DEVICE_TYPE])})",
            )
            for d in self._devices
        ]
        return self.async_show_form(
            step_id="edit_device",
            data_schema=vol.Schema(
                {
                    vol.Required("device_to_edit"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=options,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
        )

    async def async_step_edit_device_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit-Step 2: type-specific entity mapping, pre-filled.

        Entity-Felder, die der neue Typ nicht mehr exponiert, fallen
        beim Speichern automatisch weg (siehe `_build_device_record`),
        damit kein stale-Mapping liegen bleibt.
        """
        target = next(
            (d for d in self._devices if d.get(CONF_DEVICE_ID) == self._edit_target_id),
            None,
        )
        if target is None:
            return await self.async_step_init()

        device_type = self._edit_pending_type or target[CONF_DEVICE_TYPE]
        device_name = self._edit_pending_name or target[CONF_DEVICE_NAME]

        if user_input is not None:
            # Device-name comes via this step in v3.0 — type-step is
            # skipped on edit (type-changes are rare and lossy), but
            # rename is common and belongs right next to the entity
            # mapping anyway.
            new_name = (
                user_input.pop(CONF_DEVICE_NAME, "")
                or device_name
            )
            self._edit_pending_name = new_name
            entity_input = _apply_climate_first(
                _flatten_sections(user_input)
            )
            # Hold-mode is invisible in the user-facing flow (every
            # device gets `always` since v1.20.0). Carry it forward
            # so dispatch never reasons about it.
            entity_input.setdefault(
                CONF_ENTITY_CONTROL_HOLD,
                target.get(CONF_ENTITY_CONTROL_HOLD, ENTITY_CONTROL_HOLD_AUTO),
            )
            # v3.4.8 — Edit-Flow überschreibt sonst CONF_DEVICE_CONFIG_MODE
            # auf MANUAL (Default in _build_device_record) weil der Mode
            # in der Edit-Flow nicht erneut abgefragt wird (config_mode-
            # Step ist bewusst geskippt). Vor v3.4.8 hat das jedes Save
            # einen CLIMATE-Mode auf MANUAL umgepatcht → nächster Edit
            # sah include_cooling=False → Cool-Wert war weg.
            entity_input.setdefault(
                CONF_DEVICE_CONFIG_MODE,
                target.get(CONF_DEVICE_CONFIG_MODE) or CONFIG_MODE_MANUAL,
            )
            # value_on / value_off are intentionally NOT carried
            # forward. Pre-fix the carry caused the dispatcher's
            # "step already filled → skip" logic to bypass the
            # values step on every edit, even when the user wanted
            # to adjust the mapping. The values step's
            # `_values_schema(defaults=target)` pre-fills the input
            # fields from the stored device record, so leaving the
            # keys absent shows the step with the existing values
            # already typed in — best of both worlds.
            return await self._dispatch_edit_post_entities(target, entity_input)

        return self.async_show_form(
            step_id="edit_device_entities",
            data_schema=_entities_schema(
                device_type,
                defaults={**target, CONF_DEVICE_NAME: device_name},
                config_mode=target.get(CONF_DEVICE_CONFIG_MODE) or CONFIG_MODE_MANUAL,
                include_name=True,
            ),
            description_placeholders={
                "device_type": DEVICE_TYPE_LABELS_DE.get(device_type, device_type),
                "device_name": device_name,
            },
        )

    async def _dispatch_edit_post_entities(
        self, target: dict[str, Any], entity_input: dict[str, Any]
    ) -> ConfigFlowResult:
        """Edit-flow dispatcher mirrors `_dispatch_post_entities` /
        `_dispatch_add_post_entities`: charge-mode-values →
        vehicle-status → values → shares-hardware → save. Values
        runs before shares-hardware because it's entity-level
        config. Same skip logic — when the relevant key is already
        populated the step is silently skipped, so users only see
        the step when they actually need to fill it in."""
        device_type = self._edit_pending_type or target[CONF_DEVICE_TYPE]

        if (
            device_type == "wallbox"
            and entity_input.get(CONF_ENTITY_CHARGE_MODE)
            and CONF_CHARGE_MODE_VALUE_LOCK not in entity_input
        ):
            self._edit_pending_entity_input = entity_input
            return await self.async_step_edit_device_charge_mode_values()

        if (
            device_type == "battery"
            and entity_input.get(CONF_ENTITY_CHARGE_MODE)
            and CONF_BATTERY_VALUE_CHARGE not in entity_input
        ):
            self._edit_pending_entity_input = entity_input
            return await self.async_step_edit_device_battery_values()

        if (
            device_type == "wallbox"
            and entity_input.get(CONF_ENTITY_VEHICLE_STATUS)
            and CONF_VEHICLE_STATUS_VALUE_PLUGGED not in entity_input
        ):
            _auto_fill_binary_vehicle_status(entity_input)
            if CONF_VEHICLE_STATUS_VALUE_PLUGGED not in entity_input:
                self._edit_pending_entity_input = entity_input
                return await self.async_step_edit_device_vehicle_status()

        entity_control = entity_input.get(CONF_ENTITY_CONTROL, "")
        needs_values = (
            device_type in _CONTROLLABLE_TYPES
            and entity_control
            and not _is_binary_entity(entity_control)
            # Re-entry guard: after the values step submits, it adds
            # CONF_VALUE_ON to entity_input, so subsequent dispatches
            # skip the step. The values step's defaults pre-fill from
            # `target` on first show.
            and CONF_VALUE_ON not in entity_input
        )
        if needs_values:
            self._edit_pending_entity_input = entity_input
            return await self.async_step_edit_device_values()

        if (
            device_type == "warmwater"
            and CONF_SHARES_HARDWARE_WITH not in entity_input
        ):
            self._edit_pending_entity_input = entity_input
            return await self.async_step_edit_device_shares_hardware()

        # v3.0: legacy included_in_haushalt + cooling steps entfernt —
        # beide Felder werden inline erfasst.
        return await self._edit_save(target, entity_input)

    async def async_step_edit_device_charge_mode_values(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit-flow variant of the wallbox Lademodus-Werte mapping
        (Aus / An / Solaroptimiert → wallbox HA select-options)."""
        target = next(
            (d for d in self._devices if d.get(CONF_DEVICE_ID) == self._edit_target_id),
            None,
        )
        if target is None:
            return await self.async_step_init()
        device_type = self._edit_pending_type or target[CONF_DEVICE_TYPE]
        device_name = self._edit_pending_name or target[CONF_DEVICE_NAME]
        entity_input = dict(self._edit_pending_entity_input or {})
        entity_charge_mode = entity_input.get(CONF_ENTITY_CHARGE_MODE, "")

        if user_input is not None:
            entity_input[CONF_CHARGE_MODE_VALUE_LOCK] = user_input.get(
                CONF_CHARGE_MODE_VALUE_LOCK, ""
            )
            entity_input[CONF_CHARGE_MODE_VALUE_POWER] = user_input.get(
                CONF_CHARGE_MODE_VALUE_POWER, ""
            )
            entity_input[CONF_CHARGE_MODE_VALUE_SOLAR] = user_input.get(
                CONF_CHARGE_MODE_VALUE_SOLAR, ""
            )
            entity_input[CONF_ENTITY_CONTROL_HOLD] = user_input.get(
                CONF_ENTITY_CONTROL_HOLD, ENTITY_CONTROL_HOLD_AUTO
            )
            return await self._dispatch_edit_post_entities(target, entity_input)

        return self.async_show_form(
            step_id="edit_device_charge_mode_values",
            data_schema=_charge_mode_values_schema(
                self.hass, entity_charge_mode, target
            ),
            description_placeholders={
                "device_type": DEVICE_TYPE_LABELS_DE.get(device_type, device_type),
                "device_name": device_name,
                "entity_charge_mode": entity_charge_mode,
            },
        )

    async def async_step_edit_device_battery_values(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit-flow variant of the battery 4-mode mapping
        (Laden / Aus / Entladen → entity-write values)."""
        target = next(
            (d for d in self._devices if d.get(CONF_DEVICE_ID) == self._edit_target_id),
            None,
        )
        if target is None:
            return await self.async_step_init()
        device_type = self._edit_pending_type or target[CONF_DEVICE_TYPE]
        device_name = self._edit_pending_name or target[CONF_DEVICE_NAME]
        entity_input = dict(self._edit_pending_entity_input or {})
        entity_charge_mode = entity_input.get(CONF_ENTITY_CHARGE_MODE, "")

        if user_input is not None:
            entity_input[CONF_BATTERY_VALUE_CHARGE] = user_input.get(
                CONF_BATTERY_VALUE_CHARGE, ""
            )
            entity_input[CONF_BATTERY_VALUE_IDLE] = user_input.get(
                CONF_BATTERY_VALUE_IDLE, ""
            )
            entity_input[CONF_BATTERY_VALUE_DISCHARGE] = user_input.get(
                CONF_BATTERY_VALUE_DISCHARGE, ""
            )
            entity_input[CONF_BATTERY_VALUE_PASSIVE] = user_input.get(
                CONF_BATTERY_VALUE_PASSIVE, ""
            )
            entity_input[CONF_ENTITY_CONTROL_HOLD] = user_input.get(
                CONF_ENTITY_CONTROL_HOLD, ENTITY_CONTROL_HOLD_AUTO
            )
            return await self._dispatch_edit_post_entities(target, entity_input)

        return self.async_show_form(
            step_id="edit_device_battery_values",
            data_schema=_battery_values_schema(
                self.hass, entity_charge_mode, target
            ),
            description_placeholders={
                "device_type": DEVICE_TYPE_LABELS_DE.get(device_type, device_type),
                "device_name": device_name,
                "entity_charge_mode": entity_charge_mode,
            },
        )

    async def async_step_edit_device_vehicle_status(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit-flow variant of the wallbox vehicle-status mapping."""
        target = next(
            (d for d in self._devices if d.get(CONF_DEVICE_ID) == self._edit_target_id),
            None,
        )
        if target is None:
            return await self.async_step_init()
        device_type = self._edit_pending_type or target[CONF_DEVICE_TYPE]
        device_name = self._edit_pending_name or target[CONF_DEVICE_NAME]
        entity_input = dict(self._edit_pending_entity_input or {})
        entity_vehicle_status = entity_input.get(CONF_ENTITY_VEHICLE_STATUS, "")

        if user_input is not None:
            entity_input[CONF_VEHICLE_STATUS_VALUE_PLUGGED] = user_input.get(
                CONF_VEHICLE_STATUS_VALUE_PLUGGED, ""
            )
            entity_input[CONF_VEHICLE_STATUS_VALUE_UNPLUGGED] = user_input.get(
                CONF_VEHICLE_STATUS_VALUE_UNPLUGGED, ""
            )
            entity_input[CONF_VEHICLE_STATUS_VALUE_ERROR] = user_input.get(
                CONF_VEHICLE_STATUS_VALUE_ERROR, ""
            )
            return await self._dispatch_edit_post_entities(target, entity_input)

        return self.async_show_form(
            step_id="edit_device_vehicle_status",
            data_schema=_vehicle_status_schema(
                self.hass, entity_vehicle_status, target
            ),
            description_placeholders={
                "device_type": DEVICE_TYPE_LABELS_DE.get(device_type, device_type),
                "device_name": device_name,
                "entity_vehicle_status": entity_vehicle_status,
            },
        )

    async def async_step_edit_device_shares_hardware(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit-flow variant of the warmwater shares-hardware picker.
        Only the user's OTHER heating devices appear as candidates —
        a device can't share hardware with itself."""
        target = next(
            (d for d in self._devices if d.get(CONF_DEVICE_ID) == self._edit_target_id),
            None,
        )
        if target is None:
            return await self.async_step_init()
        device_type = self._edit_pending_type or target[CONF_DEVICE_TYPE]
        device_name = self._edit_pending_name or target[CONF_DEVICE_NAME]
        entity_input = dict(self._edit_pending_entity_input or {})

        heating_devices = [
            {"id": d.get(CONF_DEVICE_ID, ""), "name": d.get(CONF_DEVICE_NAME, "")}
            for d in self._devices
            if d.get(CONF_DEVICE_TYPE) == "heating"
                and d.get(CONF_DEVICE_ID) != self._edit_target_id
        ]

        if user_input is not None:
            entity_input[CONF_SHARES_HARDWARE_WITH] = user_input.get(
                CONF_SHARES_HARDWARE_WITH, ""
            )
            return await self._dispatch_edit_post_entities(target, entity_input)

        return self.async_show_form(
            step_id="edit_device_shares_hardware",
            data_schema=_shares_hardware_schema(heating_devices, target),
            description_placeholders={
                "device_type": DEVICE_TYPE_LABELS_DE.get(device_type, device_type),
                "device_name": device_name,
            },
        )

    async def async_step_edit_device_values(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit-Step 3: typ-bewusste value_on / value_off."""
        target = next(
            (d for d in self._devices if d.get(CONF_DEVICE_ID) == self._edit_target_id),
            None,
        )
        if target is None:
            return await self.async_step_init()

        device_type = self._edit_pending_type or target[CONF_DEVICE_TYPE]
        device_name = self._edit_pending_name or target[CONF_DEVICE_NAME]
        entity_input = dict(self._edit_pending_entity_input or {})
        entity_control = entity_input.get(CONF_ENTITY_CONTROL, "")
        config_mode = (
            target.get(CONF_DEVICE_CONFIG_MODE) or CONFIG_MODE_MANUAL
        )
        # v3.4.7 + v3.4.8 Legacy-/Reparatur-Auto-Migration: zwei Fälle
        # sind hier dasselbe Symptom:
        #   1) pre-v3.0-Entries hatten kein CONF_DEVICE_CONFIG_MODE
        #      gespeichert → falsy
        #   2) post-v3.0 Bug bis v3.4.7: Edit-Save überschrieb MODE auf
        #      "manual" weil entity_input das Feld nicht trug → truthy
        #      aber semantisch falsch
        # In beiden Fällen: wenn entity_control auf climate.* zeigt, ist
        # das Device tatsächlich climate-mode → cool-Feld muss in den
        # Values-Step rein. _entities_schema macht die identische
        # Migration für den Entity-Step (line 442-461).
        if config_mode != CONFIG_MODE_CLIMATE:
            legacy_ctrl = entity_control or target.get(CONF_ENTITY_CONTROL, "")
            if isinstance(legacy_ctrl, str) and legacy_ctrl.startswith(
                "climate."
            ):
                config_mode = CONFIG_MODE_CLIMATE
        include_cooling = (
            device_type == "heating" and config_mode == CONFIG_MODE_CLIMATE
        )

        if user_input is not None:
            entity_input[CONF_VALUE_ON] = (
                user_input.get(CONF_VALUE_ON)
                or target.get(CONF_VALUE_ON, "")
            )
            entity_input[CONF_VALUE_OFF] = (
                user_input.get(CONF_VALUE_OFF)
                or target.get(CONF_VALUE_OFF, "")
            )
            if include_cooling:
                entity_input[CONF_VALUE_COOL_ON] = (
                    user_input.get(CONF_VALUE_COOL_ON)
                    or target.get(CONF_VALUE_COOL_ON, "")
                )
            # v3.4.9: stille Resets auf der Cool-Familie verhindern.
            # `value_cool_off` + `entity_cool_control` werden vom Form
            # nie abgefragt (climate.set_hvac_mode reicht für die meisten
            # Setups), aber für SG-Ready / Legacy-v2.x mit explizitem
            # Cool-Pfad muss der gespeicherte Wert beim Edit erhalten
            # bleiben. Ohne carry-forward würde `_build_device_record`
            # die Felder auf `""` zurücksetzen → falscher Hold-Loop-Wert.
            entity_input.setdefault(
                CONF_VALUE_COOL_OFF,
                target.get(CONF_VALUE_COOL_OFF, ""),
            )
            entity_input.setdefault(
                CONF_ENTITY_COOL_CONTROL,
                target.get(CONF_ENTITY_COOL_CONTROL, ""),
            )
            entity_input[CONF_ENTITY_CONTROL_HOLD] = user_input.get(
                CONF_ENTITY_CONTROL_HOLD,
                target.get(CONF_ENTITY_CONTROL_HOLD, ENTITY_CONTROL_HOLD_AUTO),
            )
            return await self._dispatch_edit_post_entities(target, entity_input)

        # Pre-fill from `target` (the stored device record) so the
        # user sees their existing mapping when they open the edit
        # flow. Earlier code passed `entity_input` here, but that
        # dict deliberately doesn't carry value_on/value_off (the
        # v2.2.2 fix in async_step_edit_device_entities strips them
        # so the dispatcher's "skip-when-filled" guard always re-runs
        # this step). Result: form showed empty, user re-typed or hit
        # Next, values got cleared.
        return self.async_show_form(
            step_id="edit_device_values",
            data_schema=_values_schema(
                self.hass, entity_control, target,
                include_cooling=include_cooling,
            ),
            description_placeholders={
                "device_type": DEVICE_TYPE_LABELS_DE.get(device_type, device_type),
                "device_name": device_name,
                "entity_control": entity_control,
            },
        )

    async def _edit_save(
        self, target: dict[str, Any], entity_input: dict[str, Any]
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        device_type = self._edit_pending_type or target[CONF_DEVICE_TYPE]
        device_name = self._edit_pending_name or target[CONF_DEVICE_NAME]
        try:
            await _update_device_backend(
                self._entry.data[CONF_API_URL],
                self._entry.data[CONF_ACCESS_TOKEN],
                target[CONF_DEVICE_ID],
                device_type,
                device_name,
                entity_input,
            )
            updated = _build_device_record(
                target[CONF_DEVICE_ID], device_type, device_name, entity_input
            )
            self._devices = [
                updated if d[CONF_DEVICE_ID] == target[CONF_DEVICE_ID] else d
                for d in self._devices
            ]
            self._edit_target_id = None
            self._edit_pending_type = None
            self._edit_pending_name = None
            self._edit_pending_entity_input = None
            return await self.async_step_init()
        except (httpx.HTTPStatusError, httpx.RequestError) as err:
            _LOGGER.error("Failed to update device: %s", err)
            errors["base"] = "cannot_connect"
            return self.async_show_form(
                step_id="edit_device_entities",
                data_schema=_entities_schema(device_type, defaults=target, config_mode=target.get(CONF_DEVICE_CONFIG_MODE) or CONFIG_MODE_MANUAL),
                description_placeholders={
                    "device_type": DEVICE_TYPE_LABELS_DE.get(device_type, device_type),
                    "device_name": device_name,
                },
                errors=errors,
            )

    # ── Remove ──────────────────────────────────────────────────────────────

    async def async_step_remove_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            device_id = user_input["device_to_remove"]
            try:
                response = await _authenticated_config_request(
                    self.hass,
                    self._entry,
                    "DELETE",
                    f"/api/v1/devices/{device_id}",
                )
                # 404 is a benign "already gone" — treat as success
                # so the user can clean up an orphaned HA-side device
                # even after the backend row vanished (e.g. test
                # cleanup or another client deleted it concurrently).
                if response.status_code != 404:
                    response.raise_for_status()
            except (httpx.HTTPStatusError, httpx.RequestError) as err:
                _LOGGER.error("Failed to delete device: %s", err)
                errors["base"] = "cannot_connect"

            if not errors:
                _remove_ha_device(self.hass, device_id)
                self._devices = [
                    d for d in self._devices if d[CONF_DEVICE_ID] != device_id
                ]
                return await self.async_step_init()

        if not self._devices:
            return await self.async_step_init()

        options = [
            selector.SelectOptionDict(
                value=d[CONF_DEVICE_ID],
                label=f"{d[CONF_DEVICE_NAME]} ({DEVICE_TYPE_LABELS_DE.get(d[CONF_DEVICE_TYPE], d[CONF_DEVICE_TYPE])})",
            )
            for d in self._devices
        ]

        return self.async_show_form(
            step_id="remove_device",
            data_schema=vol.Schema(
                {
                    vol.Required("device_to_remove"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=options,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_done(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        new_data = {**self._entry.data, CONF_DEVICES: self._devices}
        self.hass.config_entries.async_update_entry(self._entry, data=new_data)
        await self.hass.config_entries.async_reload(self._entry.entry_id)
        return self.async_create_entry(title="", data={})
