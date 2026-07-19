"""Entity-mapping / device-record assembly helpers for the config flow.

Extracted from config_flow.py (#50 god-file split); imported back by
the flow classes in config_flow.py. Pure helpers — no dependency on the
ConfigFlow/OptionsFlow classes.
"""
from __future__ import annotations

from typing import Any


from .const import (
    CONF_DEVICE_ID,
    CONF_DEVICE_CONFIG_MODE,
    CONF_DEVICE_NAME,
    CONF_DEVICE_TYPE,
    CONFIG_MODE_MANUAL,
    CONF_ENTITY_CHARGE_MODE,
    CONF_ENTITY_WALLBOX_CHARGE_CURRENT,
    CONF_ENTITY_WALLBOX_PHASE_MODE,
    CONF_VALUE_WALLBOX_PHASE_1,
    CONF_VALUE_WALLBOX_PHASE_3,
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
    CONF_SUPPORTS_COOLING,
    CONF_ENTITY_COOL_CONTROL,
    CONF_VALUE_COOL_ON,
    CONF_VALUE_COOL_OFF,
    ENTITY_CONTROL_HOLD_AUTO,
    DOMAIN,
)


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


def _apply_climate_first(
    entity_input: dict[str, Any], device_type: str = ""
) -> dict[str, Any]:
    """Climate-/Water-Heater-first: wenn entity_climate ODER
    entity_water_heater gesetzt ist, kopieren wir den Wert auf
    entity_control. Damit weiß der Connector im Dispatch ob er
    `climate.set_hvac_mode` (climate.*) oder
    `water_heater.set_operation_mode` (water_heater.*) ruft —
    die Domain-Auswahl ist im Service-Adapter.

    **Warmwasser** (water_heater.*): die `current_temperature` der
    primary entity IST die Tank-Temp — auto-copy nach
    `entity_current_temp` ist semantisch korrekt.

    **Klima/aircon** (climate.*): bei Split-AC ist
    `climate.current_temperature` die echte Raumtemp →
    auto-copy nach `entity_current_temp` ist hier semantisch
    ebenfalls korrekt.

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
        # Auto-copy nach entity_current_temp für die Fälle wo die
        # primary-entity `current_temperature` semantisch zur Raumtemp/
        # Tanktemp passt: water_heater (Tank), aircon (Raum).
        is_water_heater = primary.startswith("water_heater.")
        is_aircon = device_type == "aircon"
        if (
            (is_water_heater or is_aircon)
            and not entity_input.get(CONF_ENTITY_CURRENT_TEMP, "")
        ):
            entity_input[CONF_ENTITY_CURRENT_TEMP] = primary
    return entity_input


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
        CONF_ENTITY_VORLAUF_SETPOINT: entity_input.get(
            CONF_ENTITY_VORLAUF_SETPOINT, ""
        ),
        CONF_ENTITY_ENERGY_TOTAL: entity_input.get(CONF_ENTITY_ENERGY_TOTAL, ""),
        CONF_ENTITY_ENERGY_DISCHARGED_TOTAL: entity_input.get(
            CONF_ENTITY_ENERGY_DISCHARGED_TOTAL, ""
        ),
        # #42 HC-Triade (Hausverbrauchs-Chart, 2026-06-16 fix): die vier
        # optionalen Read-Slots werden vom Schema gesammelt (read_fields +
        # _ENTITY_SELECTORS) und vom Coordinator (_SOLVER_EXTRA_FIELDS) als
        # Telemetrie-Extra gelesen — sie MÜSSEN hier in den persistierten
        # Record, sonst droppt der Submit sie und der Vendor-Wahrheit-Pfad
        # bleibt leer (v3.28.0-Defekt: Schema angefasst, Record vergessen).
        CONF_ENTITY_HC_PV_POWER: entity_input.get(CONF_ENTITY_HC_PV_POWER, ""),
        CONF_ENTITY_HC_BATTERY_POWER: entity_input.get(
            CONF_ENTITY_HC_BATTERY_POWER, ""
        ),
        CONF_ENTITY_HC_GRID_POWER: entity_input.get(
            CONF_ENTITY_HC_GRID_POWER, ""
        ),
        CONF_ENTITY_PV_TO_BATTERY_POWER: entity_input.get(
            CONF_ENTITY_PV_TO_BATTERY_POWER, ""
        ),
        CONF_ENTITY_CONTROL: entity_input.get(CONF_ENTITY_CONTROL, ""),
        CONF_VALUE_ON: entity_input.get(CONF_VALUE_ON, ""),
        CONF_VALUE_OFF: entity_input.get(CONF_VALUE_OFF, ""),
        CONF_ENTITY_CONTROL_HOLD: entity_input.get(
            CONF_ENTITY_CONTROL_HOLD, ENTITY_CONTROL_HOLD_AUTO
        ),
        CONF_ENTITY_CHARGE_MODE: entity_input.get(CONF_ENTITY_CHARGE_MODE, ""),
        # Wallbox variabler Ladestrom (2026-06-20): die optionale Ampere-
        # Number-Entity bleibt Connector-lokal (nur das abgeleitete Bool
        # geht ans Backend) und MUSS hier in den persistierten Record —
        # sonst droppt der Submit sie stumm (exakt der v3.28.0-Defekt oben:
        # Schema angefasst, Record vergessen), die Entity verschwindet beim
        # Re-Open und der Dispatcher (`command_dispatcher`) liest leer →
        # kein variabler Strom, supports_charge_current fällt beim Edit auf
        # False zurück.
        CONF_ENTITY_WALLBOX_CHARGE_CURRENT: entity_input.get(
            CONF_ENTITY_WALLBOX_CHARGE_CURRENT, ""
        ),
        # 2026-07-19: 1/3-Phasen-Umschaltung (Entity + zwei Options-
        # Strings, alle Connector-lokal). Gleiche Regel wie beim
        # Ladestrom: fehlt der Key hier, droppt der Submit ihn stumm →
        # Dispatcher liest leer, Capability fällt beim Edit auf False.
        CONF_ENTITY_WALLBOX_PHASE_MODE: entity_input.get(
            CONF_ENTITY_WALLBOX_PHASE_MODE, ""
        ),
        CONF_VALUE_WALLBOX_PHASE_1: entity_input.get(
            CONF_VALUE_WALLBOX_PHASE_1, ""
        ),
        CONF_VALUE_WALLBOX_PHASE_3: entity_input.get(
            CONF_VALUE_WALLBOX_PHASE_3, ""
        ),
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
        # v3.8.0 (Phase 3 Option D, 2026-06-02): Battery-Dispatch via
        # Lademodus-Select + signed Power-Setpoint-Number. Backend
        # liefert pro Tick continuous setpoint_kw + mode-tag.
        CONF_ENTITY_BATTERY_MODE: entity_input.get(CONF_ENTITY_BATTERY_MODE, ""),
        CONF_VALUE_BATTERY_MODE_ACTIVE: entity_input.get(
            CONF_VALUE_BATTERY_MODE_ACTIVE, ""
        ),
        CONF_VALUE_BATTERY_MODE_PASSIVE: entity_input.get(
            CONF_VALUE_BATTERY_MODE_PASSIVE, ""
        ),
        CONF_ENTITY_BATTERY_POWER_SETPOINT: entity_input.get(
            CONF_ENTITY_BATTERY_POWER_SETPOINT, ""
        ),
        CONF_BATTERY_SETPOINT_INVERT_SIGN: bool(
            entity_input.get(CONF_BATTERY_SETPOINT_INVERT_SIGN, False)
        ),
        # v2.0: warmwater-only. The backend device-id of the heating
        # device sharing this compressor. Coordinator does nothing
        # with this — it's POSTed once at device-register time so the
        # backend can wire up the joint-power constraint.
        CONF_SHARES_HARDWARE_WITH: entity_input.get(
            CONF_SHARES_HARDWARE_WITH, ""
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


# CN-12 (2026-06-11): das frühere `_delete_device_backend` (Single-
# Token, ohne 401-Refresh) ist entfernt — der letzte Call-Site
# (remove_device) läuft längst über `_authenticated_config_request`.


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
