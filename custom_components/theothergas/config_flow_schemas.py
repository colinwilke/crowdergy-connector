"""Voluptuous schema builders + entity-selector specs for the config flow.

Extracted from config_flow.py (#50 god-file split); imported back by
the flow classes in config_flow.py. Pure helpers — no dependency on the
ConfigFlow/OptionsFlow classes.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector

from .const import (
    CONF_DEVICE_CONFIG_MODE,
    CONF_DEVICE_NAME,
    CONF_DEVICE_TYPE,
    CONFIG_MODE_CLIMATE,
    CONFIG_MODE_MANUAL,
    CONF_ENTITY_CHARGE_MODE,
    CONF_ENTITY_WALLBOX_CHARGE_CURRENT,
    CONF_ENTITY_CLIMATE,
    CONF_ENTITY_WATER_HEATER,
    CONF_ENTITY_CONTROL,
    CONF_ENTITY_POWER,
    CONF_ENTITY_POWER_2,
    CONF_ENTITY_SOC,
    CONF_ENTITY_VEHICLE_STATUS,
    CONF_ENTITY_CURRENT_TEMP,
    CONF_ENTITY_VORLAUF_SETPOINT,
    CONF_ENTITY_VORLAUF_TEMP,
    CONF_ENTITY_ENERGY_TOTAL,
    CONF_ENTITY_ENERGY_DISCHARGED_TOTAL,
    CONF_ENTITY_HC_PV_POWER,
    CONF_ENTITY_HC_BATTERY_POWER,
    CONF_ENTITY_HC_GRID_POWER,
    CONF_ENTITY_PV_TO_BATTERY_POWER,
    CONF_INVERT_POWER_SIGN,
    CONF_ENTITY_CONTROL_HOLD,
    CONF_VALUE_OFF,
    CONF_VALUE_ON,
    CONF_VEHICLE_STATUS_VALUE_PLUGGED,
    CONF_VEHICLE_STATUS_VALUE_UNPLUGGED,
    CONF_VEHICLE_STATUS_VALUE_ERROR,
    CONF_CHARGE_MODE_VALUE_LOCK,
    CONF_CHARGE_MODE_VALUE_POWER,
    CONF_CHARGE_MODE_VALUE_SOLAR,
    CONF_ENTITY_BATTERY_MODE,
    CONF_VALUE_BATTERY_MODE_ACTIVE,
    CONF_VALUE_BATTERY_MODE_PASSIVE,
    CONF_ENTITY_BATTERY_POWER_SETPOINT,
    CONF_BATTERY_SETPOINT_INVERT_SIGN,
    CONF_SHARES_HARDWARE_WITH,
    CONF_VALUE_COOL_ON,
    ENTITY_CONTROL_HOLD_ALWAYS,
    ENTITY_CONTROL_HOLD_AUTO,
    ENTITY_CONTROL_HOLD_NEVER,
    CONTROLLABLE_TYPES,
    DEVICE_TYPES,
    TEMPERATURE_CONTROL_DOMAINS,
    TEMPERATURE_CONTROL_TYPES,
)


_CONTROLLABLE_TYPES = CONTROLLABLE_TYPES

# German labels for the device-type picker.
DEVICE_TYPE_LABELS_DE = {
    "solar": "Solar",
    "battery": "Batterie",
    "wallbox": "Wallbox",
    "grid": "Netz",
    "heating": "Heizung (Wärmepumpe)",
    "warmwater": "Warmwasser (Wärmepumpe)",
    "aircon": "Klimaanlage",
    "generic": "Sonstiges",
    "haushalt": "Haushalt",
}

# Device-Typen die einen Kühl-Modus konfigurieren können — heating-
# family only. Warmwater nie (DHW-Tank ist monotonic-heat), wallbox /
# battery haben eigene Mode-Controls, solar / grid / haushalt sind
# read-only, generic ist Catch-all ohne Thermo-Modell.
_COOLING_CAPABLE_TYPES = {"heating"}

# Which read-side telemetry fields each device type exposes. Crowdergize-
# capable types additionally get the control trio (entity_control +
# value_on + value_off) rendered as a separate section.
_READ_FIELDS: dict[str, list[str]] = {
    # #42: optionaler HC-from-PV-Sensor (Hausverbrauch aus PV, W ≥0) —
    # speist den Hausverbrauchs-Stack-Chart (Backend #41).
    "solar":     [
        CONF_ENTITY_POWER, CONF_ENTITY_ENERGY_TOTAL,
        CONF_ENTITY_HC_PV_POWER,
    ],
    # v3.0 bidirektional: zweites Power-Sensor-Feld neben dem zweiten
    # Energie-Zähler. power_1 = Bezug (Energie raus aus Netz, ins Haus),
    # power_2 = Einspeisung. Coordinator computet signed power_1 - power_2.
    # #42: optionaler HC-from-Grid-Sensor (Hausverbrauch aus dem Netz).
    "grid":      [
        CONF_ENTITY_POWER, CONF_ENTITY_ENERGY_TOTAL,
        CONF_ENTITY_POWER_2, CONF_ENTITY_ENERGY_DISCHARGED_TOTAL,
        CONF_ENTITY_HC_GRID_POWER,
    ],
    "heating":   [
        CONF_ENTITY_POWER, CONF_ENTITY_ENERGY_TOTAL,
    ],
    "warmwater": [
        CONF_ENTITY_POWER, CONF_ENTITY_ENERGY_TOTAL,
    ],
    # aircon teilt das heating/warmwater-Schema (control_section unten),
    # braucht aber wie sie den optionalen kWh-Zähler im read_section — sonst
    # fällt der Picker via _READ_FIELDS.get(..., [POWER]) auf Power-only
    # zurück und der Energiezähler-Slot fehlt im Klima-Mapping.
    "aircon":    [
        CONF_ENTITY_POWER, CONF_ENTITY_ENERGY_TOTAL,
    ],
    "haushalt":  [CONF_ENTITY_POWER, CONF_ENTITY_ENERGY_TOTAL],
    # Batterie: power_1 = Entladung, power_2 = Ladung, energy_1 +
    # energy_2 die zugehörigen kWh-Zähler. SoC zwischen den Power-
    # Paaren damit visuell zusammengehört. #42: HC-from-Battery (Pflicht
    # für Vendor-Wahrheit-Pfad) + optional der direkt gemessene
    # PV→Batterie-Ladestrom (4. Sensor, nice-to-have).
    "battery":   [
        CONF_ENTITY_POWER, CONF_ENTITY_ENERGY_TOTAL,
        CONF_ENTITY_POWER_2, CONF_ENTITY_ENERGY_DISCHARGED_TOTAL,
        CONF_ENTITY_SOC,
        CONF_ENTITY_HC_BATTERY_POWER, CONF_ENTITY_PV_TO_BATTERY_POWER,
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
#
# kW/kWh-Typsicherheit (#46): eindeutige Read-Slots tragen zusätzlich
# einen device_class-Filter, damit der Picker je Slot NUR den passenden
# Mess-Typ anbietet — Leistungs-Slots (W/kW → device_class="power") zeigen
# nur Leistungs-Entities, Energie-Zähler-Slots (kWh → "energy") nur
# Energie-Entities. Das verhindert die häufige kW/kWh-Verwechslung beim
# Mapping (z. B. ein Leistungssensor in einen kWh-Slot). Der HA-Filter ist
# HART (Entities ohne passende device_class werden ausgeblendet) — bewusst
# nur an eindeutigen Read-Slots, NICHT an Multi-Domain-/Control-Slots
# (climate/water_heater tragen keine sensor-device_class). Moderne
# Integrationen inkl. kostal_plenticore (verifizierte Hardware) setzen
# device_class auf ihren Power-/Energy-Sensoren.
_ENTITY_SELECTORS: dict[str, selector.EntitySelector] = {
    CONF_ENTITY_POWER: selector.EntitySelector(
        selector.EntitySelectorConfig(domain="sensor", device_class="power")
    ),
    # v3.0 zweites Power-Sensor-Feld (bidirektional) — selber
    # Selector-Typ wie CONF_ENTITY_POWER.
    CONF_ENTITY_POWER_2: selector.EntitySelector(
        selector.EntitySelectorConfig(domain="sensor", device_class="power")
    ),
    # SoC % → HA device_class "battery".
    CONF_ENTITY_SOC: selector.EntitySelector(
        selector.EntitySelectorConfig(domain="sensor", device_class="battery")
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
    # device_class="temperature" möglich, da reiner Sensor-Slot (anders als
    # CONF_ENTITY_CURRENT_TEMP, das auch climate/water_heater zulässt).
    CONF_ENTITY_VORLAUF_TEMP: selector.EntitySelector(
        selector.EntitySelectorConfig(
            domain="sensor", device_class="temperature"
        )
    ),
    # Phase 2b (2026-06-02): Write-Side Vorlauf-Setpoint. Bei
    # modulierenden Heizungen sendet der Solver pro Tick °C, der
    # Connector dispatcht via climate.set_temperature gegen diese
    # Entity. Domain umfasst climate (modulierende WPs mit
    # climate.set_temperature-Service) sowie number / input_number
    # für SG-Ready-WPs mit eigenem Setpoint-Register.
    CONF_ENTITY_VORLAUF_SETPOINT: selector.EntitySelector(
        selector.EntitySelectorConfig(
            domain=["climate", "number", "input_number"]
        )
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
    # Wallbox-only OPTIONAL Ladestrom-Steuerung (2026-06-20). Number-
    # Entity die den Ladestrom in Ampere setzt (typisch 6–16 A). Kein
    # device_class-Filter (Control-Slot — #46-Regel: nur eindeutige
    # Read-Slots filtern; current-device_class ist nicht überall
    # gesetzt). Wenn gemappt, lädt der Solver variabel im „An"-Modus.
    CONF_ENTITY_WALLBOX_CHARGE_CURRENT: selector.EntitySelector(
        selector.EntitySelectorConfig(domain=["number", "input_number"])
    ),
    # Energy meter — HA `total_increasing` kWh sensor (lifetime
    # cumulative). Restricted to plain sensor entities; the backend
    # rejects non-monotonic data via a delta clamp.
    CONF_ENTITY_ENERGY_TOTAL: selector.EntitySelector(
        selector.EntitySelectorConfig(domain="sensor", device_class="energy")
    ),
    # Battery-only: second `total_increasing` kWh sensor for the
    # discharge counter — splits charge / discharge into separate
    # streams server-side.
    CONF_ENTITY_ENERGY_DISCHARGED_TOTAL: selector.EntitySelector(
        selector.EntitySelectorConfig(domain="sensor", device_class="energy")
    ),
    # Hausverbrauchs-Flow-Sensoren (#42). Live-Power-Sensoren (W ≥0),
    # die der Vendor direkt misst — wieviel des Hausverbrauchs aktuell
    # aus PV / Batterie / Netz gedeckt wird, plus optional die
    # PV→Batterie-Ladeleistung. Nur Sensor-Domain mit device_class="power"
    # (W-Live-Werte → kW/kWh-Typsicherheit, #46).
    CONF_ENTITY_HC_PV_POWER: selector.EntitySelector(
        selector.EntitySelectorConfig(domain="sensor", device_class="power")
    ),
    CONF_ENTITY_HC_BATTERY_POWER: selector.EntitySelector(
        selector.EntitySelectorConfig(domain="sensor", device_class="power")
    ),
    CONF_ENTITY_HC_GRID_POWER: selector.EntitySelector(
        selector.EntitySelectorConfig(domain="sensor", device_class="power")
    ),
    CONF_ENTITY_PV_TO_BATTERY_POWER: selector.EntitySelector(
        selector.EntitySelectorConfig(domain="sensor", device_class="power")
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


def _vendor_preset_pick_schema(presets: list[dict[str, Any]]) -> vol.Schema:
    """Picker für Vendor-Presets. Option `__manual__` skipt das Preset
    und führt zum klassischen manuellen Entity-Mapping. Pro Preset
    eine Option im Format `<vendor>::<model>` als Key. Presets im
    Staging (Backend-`status` ≠ approved, Store-Vertrag) werden
    gekennzeichnet — sie werden bewusst mit angeboten, der
    Promotion-/Kurations-Threshold kuratiert nur das Label; fehlt
    das Feld (Alt-Backend) gilt approved."""
    options = []
    for p in presets:
        label = (
            f"{p['vendor']} {p['model']} "
            f"(Anzahl Beiträge: {p.get('contribution_count', 1)})"
        )
        if p.get("status") not in (None, "approved"):
            label += " — Community, noch unbestätigt"
        options.append({"value": f"{p['vendor']}::{p['model']}", "label": label})
    options.append({"value": "__manual__", "label": "Manuell konfigurieren"})
    return vol.Schema(
        {
            vol.Required("preset_choice"): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=options,
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
        }
    )


def _contribute_form_schema(
    vendor: str | None, model: str | None, notes: str | None,
) -> vol.Schema:
    """Schema für den Vendor-Preset-Submit-Step (FEAT-1, 2026-06-09).

    Vendor + Model required; Notes optional ≤ 280 chars (Backend lehnt
    sonst ab). `suggested_value` damit der User Fehler-Resubmits ohne
    Re-Eingabe machen kann.
    """
    def _field(key: str, value: str | None, required: bool) -> Any:
        cls = vol.Required if required else vol.Optional
        if value:
            return cls(key, description={"suggested_value": value})
        return cls(key)
    return vol.Schema(
        {
            _field("vendor", vendor, True): selector.TextSelector(),
            _field("model", model, True): selector.TextSelector(),
            _field("notes", notes, False): selector.TextSelector(
                selector.TextSelectorConfig(multiline=True)
            ),
        }
    )


def _entity_field(key: str, defaults: dict[str, Any]) -> Any:
    # `suggested_value` statt `default`: HA's Form-Engine re-injected
    # `default`-Werte wenn der User das Feld auf leer setzt (X-Klick) —
    # → entity_id taucht beim Re-Open wieder auf. `suggested_value`
    # füllt den Initial-Wert nur in die UI, ohne ihn bei leerem Submit
    # zurückzuschreiben.
    if defaults.get(key):
        return vol.Optional(key, description={"suggested_value": defaults[key]})
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

    # v3.26: Haushalt-Toggle entfernt — die Zuordnung „Messung im
    # übergeordneten Zähler enthalten" ist jetzt der generische
    # parent_device_id-Baum und wird in der Crowdergy-App pro Gerät
    # konfiguriert („Übergeordnetes Gerät"); auf der Box ist der
    # HA-Config-Flow für Kunden ohnehin unerreichbar.

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
        # Optional Ladestrom-Entity (2026-06-20): mappt der User hier
        # eine Number-Entity (Ampere), darf der Solver im „An"-Modus mit
        # variablem Strom (6–16 A) laden statt nur volle Leistung; der
        # Connector schreibt den AI-Strom dann hierher. Leer = „An" =
        # volle Leistung (unverändert). Solar/Lock bleiben stromlos.
        control_schema = vol.Schema({
            _entity_field(CONF_ENTITY_CHARGE_MODE, d):
                _ENTITY_SELECTORS[CONF_ENTITY_CHARGE_MODE],
            _entity_field(CONF_ENTITY_WALLBOX_CHARGE_CURRENT, d):
                _ENTITY_SELECTORS[CONF_ENTITY_WALLBOX_CHARGE_CURRENT],
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
    elif device_type in {"heating", "warmwater", "aircon"}:
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
            # Phase 2b (2026-06-02): Im Climate-Mode wird der Vorlauf-
            # Setpoint NICHT separat abgefragt — die climate.*-Entity
            # hat `set_temperature` bereits eingebaut und der Connector
            # dispatcht direkt gegen `entity_control` (= dieselbe
            # climate-Entity). Skip hier; Code-Pfad in coordinator
            # `_apply_vorlauf_setpoint` fällt automatisch auf
            # entity_control zurück wenn entity_vorlauf_setpoint leer.
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
            # Phase 2b (2026-06-02): Manuell-Mode für Heizung — hier ist
            # entity_control typisch ein SG-Ready-Select oder Boost-
            # Switch ohne eingebauten Setpoint-Service. Der User muss
            # die VL-Setpoint-Entity separat angeben (number /
            # input_number / climate). Solver-Output °C wird via
            # climate.set_temperature bzw. number.set_value
            # dispatched. Leer lassen = kein Setpoint-Dispatch, die
            # Heizung läuft weiter binary on/off.
            if device_type == "heating":
                control_fields[
                    _entity_field(CONF_ENTITY_VORLAUF_SETPOINT, d)
                ] = _ENTITY_SELECTORS[CONF_ENTITY_VORLAUF_SETPOINT]
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


def _temperature_value_selector(hass, entity_id: str):
    """NumberSelector für den WP-Temperatur-Modus (AN = Maximal-, AUS =
    Minimaltemperatur). Range/Step kommen aus den min_temp/max_temp/
    target_temp_step-Attributen der climate-/water_heater-Entity;
    konservativer °C-Fallback, wenn die Entity (noch) keinen State hat.
    """
    min_t, max_t, step = 5.0, 80.0, 0.5
    state = hass.states.get(entity_id) if entity_id else None
    if state is not None:
        try:
            if state.attributes.get("min_temp") is not None:
                min_t = float(state.attributes["min_temp"])
            if state.attributes.get("max_temp") is not None:
                max_t = float(state.attributes["max_temp"])
            if state.attributes.get("target_temp_step") is not None:
                step = float(state.attributes["target_temp_step"])
        except (TypeError, ValueError):
            min_t, max_t, step = 5.0, 80.0, 0.5
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=min_t,
            max=max_t,
            step=step,
            unit_of_measurement="°C",
            mode=selector.NumberSelectorMode.BOX,
        )
    )


def _values_schema(
    hass,
    entity_control: str,
    defaults: dict[str, Any],
    include_cooling: bool = False,
    cooling_first: bool = False,
    device_type: str | None = None,
) -> vol.Schema:
    """Step C schema: value_on + value_off, typed if entity_control supports it.

    v3.0 include_cooling=True (heating type) → zusätzlich value_cool_on
    inline. value_cool_off entfällt im Schema, weil es bei climate-* und
    SG-Ready 1:1 identisch zu value_off ist (climate.set_hvac_mode("off"));
    _register_device leitet es daraus ab.

    v3.6.1 cooling_first=True (aircon) → Reihenfolge Kühlen → Heizen
    (optional) → Aus → Hold, weil bei einer Klimaanlage Kühlen der
    primäre Use-Case ist und Heizen sekundär/optional bleibt.

    Temperatur-Modus (2026-07-02): für heating/warmwater mit climate-/
    water_heater-Steuer-Entity sind value_on / value_off ZIEL-
    TEMPERATUREN (°C-NumberSelector aus den Entity-Attributen) statt
    HVAC-Modus-Dropdowns — WPs werden nie hart an/aus geschaltet,
    sondern zwischen Maximal- (AN) und Minimaltemperatur (AUS) bewegt.
    value_cool_on (heating+cooling) bleibt ein Modus-Mapping.
    """
    value_sel = _value_selector(hass, entity_control)
    temperature_mode = bool(
        device_type in TEMPERATURE_CONTROL_TYPES
        and entity_control
        and entity_control.split(".", 1)[0] in TEMPERATURE_CONTROL_DOMAINS
    )

    def _field(key: str):
        default = defaults.get(key, "")
        # NumberSelector chokes on empty-string defaults; use None there.
        # Temperatur-Modus: value_on/value_off sind numerische Felder —
        # Legacy-Modus-Strings ("heat"/"off") in Bestands-Configs fallen
        # beim float()-Cast durch und rendern leer (User trägt °C ein).
        is_number = bool(
            entity_control
            and entity_control.split(".", 1)[0] in ("number", "input_number")
        ) or (temperature_mode and key in (CONF_VALUE_ON, CONF_VALUE_OFF))
        if default == "":
            return vol.Optional(key)
        # Cast for NumberSelector consistency. `suggested_value` statt
        # `default` — siehe _entity_field für Begründung (Clear-Klick).
        if is_number:
            try:
                return vol.Optional(key, description={"suggested_value": float(default)})
            except (TypeError, ValueError):
                return vol.Optional(key)
        return vol.Optional(key, description={"suggested_value": str(default)})

    field_type: Any = value_sel if value_sel is not None else str
    onoff_type: Any = (
        _temperature_value_selector(hass, entity_control)
        if temperature_mode
        else field_type
    )

    schema: dict[Any, Any] = {}
    if cooling_first and include_cooling:
        schema[_field(CONF_VALUE_COOL_ON)] = field_type
        schema[_field(CONF_VALUE_ON)] = onoff_type
        schema[_field(CONF_VALUE_OFF)] = onoff_type
    else:
        schema[_field(CONF_VALUE_ON)] = onoff_type
        schema[_field(CONF_VALUE_OFF)] = onoff_type
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
            # suggested_value statt default — Clear-Klick (X) soll
            # wirklich clearen, nicht beim Submit den alten Wert
            # re-injecten.
            return vol.Optional(key, description={"suggested_value": default})
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
            # suggested_value statt default — siehe _entity_field.
            # Comment oben („leaves blank simply don't get a button")
            # versprach das User-Verhalten, der alte default-Pfad hat's
            # gebrochen.
            return vol.Optional(key, description={"suggested_value": default})
        return vol.Optional(key)

    return vol.Schema({
        _field(CONF_CHARGE_MODE_VALUE_LOCK): field_type,
        _field(CONF_CHARGE_MODE_VALUE_POWER): field_type,
        _field(CONF_CHARGE_MODE_VALUE_SOLAR): field_type,
        _hold_mode_field(d): _hold_mode_selector(),
    })


# ── v3.8.0: battery setpoint dispatch (Phase 3 Option D) ─────────────────────


def _battery_values_schema(
    hass, defaults: dict[str, Any] | None = None
) -> vol.Schema:
    """Battery-Dispatch-Schema v3.8.0 (Phase 3 Option D, 2026-06-02).

    Statt 4 Mode-String-Werte (charge/discharge/idle/passive)
    dispatchen wir jetzt continuous Power-Setpoint + Lademodus-Toggle:

      * `entity_battery_mode` (Select) — Aktiv vs Passiv. Bei Passiv
        schreibt HA nichts an den WR → Native-PV-Priority übernimmt.
      * `value_battery_mode_active` / `_passive` — die zwei Strings
        die der Select-Entity hält.
      * `entity_battery_power_setpoint_w` (Number) — signed Power-
        Setpoint in Watt. Solver-Konvention: + = laden, − = entladen.
      * `battery_setpoint_invert_sign` (bool) — bei umgekehrtem WR
        (z.B. − = laden) hier aktivieren; Connector schreibt dann
        −Setpoint statt +Setpoint.
    """
    d = defaults or {}

    def _required(key: str, sel: Any) -> Any:
        default = d.get(key)
        if default:
            return (vol.Required(key, default=default), sel)
        return (vol.Required(key), sel)

    def _optional_bool(key: str, default: bool = False) -> Any:
        return vol.Optional(
            key,
            default=bool(d.get(key, default)),
        ), selector.BooleanSelector()

    fields: dict[Any, Any] = {}

    # entity_battery_mode (Select)
    key_em, sel_em = _required(
        CONF_ENTITY_BATTERY_MODE,
        selector.EntitySelector(
            selector.EntitySelectorConfig(domain=["select", "input_select"])
        ),
    )
    fields[key_em] = sel_em

    # value_battery_mode_active + _passive — Free-Text-Strings, oder
    # Dropdown wenn die gewählte Select-Entity ihre Options exposed.
    em_entity = d.get(CONF_ENTITY_BATTERY_MODE, "") or ""
    mode_value_field: Any = str
    if em_entity:
        state = hass.states.get(em_entity)
        if state is not None:
            opts = state.attributes.get("options") or []
            if opts:
                mode_value_field = selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(value=o, label=o)
                            for o in opts
                        ],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                )

    key_va, _ = _required(CONF_VALUE_BATTERY_MODE_ACTIVE, mode_value_field)
    fields[key_va] = mode_value_field
    key_vp, _ = _required(CONF_VALUE_BATTERY_MODE_PASSIVE, mode_value_field)
    fields[key_vp] = mode_value_field

    # entity_battery_power_setpoint_w (Number)
    key_sp, sel_sp = _required(
        CONF_ENTITY_BATTERY_POWER_SETPOINT,
        selector.EntitySelector(
            selector.EntitySelectorConfig(domain=["number", "input_number"])
        ),
    )
    fields[key_sp] = sel_sp

    # battery_setpoint_invert_sign (bool, default False)
    key_inv, sel_inv = _optional_bool(CONF_BATTERY_SETPOINT_INVERT_SIGN)
    fields[key_inv] = sel_inv

    fields[_hold_mode_field(d)] = _hold_mode_selector()
    return vol.Schema(fields)


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
