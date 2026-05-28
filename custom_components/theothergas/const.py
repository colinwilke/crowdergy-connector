"""Constants for the Crowdergy Connector integration."""

import json
from pathlib import Path

# Pull the version from manifest.json so we have a single source of
# truth. Used in HTTP User-Agent strings (Nominatim's TOS asks for a
# descriptive UA; bumping it along with each release keeps drift out).
_MANIFEST = json.loads((Path(__file__).parent / "manifest.json").read_text())
VERSION: str = _MANIFEST["version"]
USER_AGENT: str = (
    f"crowdergy-connector/{VERSION} "
    "(+https://github.com/colinwilke/crowdergy-connector)"
)

DOMAIN = "theothergas"
DEFAULT_API_URL = "https://api.theothergas.de"
CONF_API_URL = "api_url"
CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_USER_ID = "user_id"
CONF_DEVICES = "devices"
CONF_DEVICE_ID = "device_id"
# Connector v2.0 splits the legacy 'heatpump' type into 'heating' +
# 'warmwater'. Each has its own thermal model in the joint MPC; a
# warmwater device can declare it shares a compressor with a heating
# device via CONF_SHARES_HARDWARE_WITH (joint-power constraint server-
# side). Existing 'heatpump' config entries from v1.x are no longer
# recognised — the user must delete and re-add as heating + warmwater.
DEVICE_TYPES = [
    "solar", "battery", "wallbox", "grid",
    "heating", "warmwater",
    "generic", "haushalt",
]
UPDATE_INTERVAL = 30

CONF_DEVICE_NAME = "device_name"
CONF_DEVICE_TYPE = "device_type"
CONF_DISTRICT = "district"
CONF_CITY = "city"
CONF_REGION = "region"
# Integration-wide (not per-device): an HA sensor with the current
# outdoor temperature in °C. Optional — when missing, the backend
# falls back to Open-Meteo for the user's location every 15 min.
CONF_ENTITY_OUTDOOR_TEMP = "entity_outdoor_temp_c"
CONF_ENTITY_POWER = "entity_current_power_kw"
CONF_ENTITY_SOC = "entity_soc_percent"
# Wallbox-only read-only field.
CONF_ENTITY_VEHICLE_STATUS = "entity_vehicle_status"
# Heatpump (and hot-water tank) read-only temperature entities. Both
# optional — devices without a target-temp sensor leave it null.
CONF_ENTITY_CURRENT_TEMP = "entity_current_temp_c"
CONF_ENTITY_TARGET_TEMP = "entity_target_temp_c"
# Optional per-device kWh meter. Must be an HA sensor with
# `state_class: total_increasing` (lifetime cumulative). The
# Crowdergy backend stores the raw value and derives day / month /
# year totals via SQL `date_trunc` deltas — never integrate this
# from `power_kw` (5–10 % drift compounds badly). Devices without
# such a sensor mapped just keep the kW-only display in the app.
CONF_ENTITY_ENERGY_TOTAL = "entity_energy_total"
# Optional second `total_increasing` kWh counter for the OTHER
# direction. Offered for bidirectional device types (battery, grid,
# later V2G wallbox) where HA typically exposes the two flows as
# separate cumulative sensors. Semantically: `entity_energy_total`
# above carries the "consumed by device" counter (battery charge,
# grid import); this field carries the "delivered by device" counter
# (battery discharge, grid export). When both are mapped the
# coordinator computes a signed net Δ so the backend stores a
# single value per row.
# Config key stays `entity_energy_discharged_total` for stored-config
# backward compatibility — pre-v1.24 it was battery-only.
CONF_ENTITY_ENERGY_DISCHARGED_TOTAL = "entity_energy_discharged_total"

# As of v1.8.0 Crowdergy controls each device via a single user-mapped
# entity. When the Crowdergy app flips the device on/off (or, later, the
# smart controller does), the connector writes `value_on` / `value_off`
# to `entity_control`. Replaces the previous typ-specific writable
# entity mappings (entity_is_active / entity_soc_min / entity_soc_max /
# entity_charge_mode) which are no longer used.
CONF_ENTITY_CONTROL = "entity_control"
CONF_VALUE_ON = "value_on"
CONF_VALUE_OFF = "value_off"

# Wallbox-only: separate write-target for the Lademodus picker in the iOS
# app. Settable HA select entity (typically with "Lock Mode" / "Power Mode" /
# "Solar Pure Mode" — or any other options the user has configured on their
# wallbox integration). Distinct from entity_control because the user
# wants three-way manual control rather than a binary on/off mapping.
CONF_ENTITY_CHARGE_MODE = "entity_charge_mode"

# Wallbox-only ternary mapping for the charge-mode select entity (v2.2+).
# The Crowdergy app exposes three explicit modes — "Aus", "An",
# "Solaroptimiert" — plus the AI auto-pilot. Each maps onto one of the
# wallbox's HA select-options (e.g. "Lock Mode" / "Power Mode" /
# "Solar Pure Mode" on a go-eCharger; "Off" / "Boost" / "Eco" on
# others). v2.1 and earlier had the user enter these strings in the
# iOS app — moving the mapping here keeps all infrastructure plumbing
# in one place alongside CONF_VALUE_ON / OFF and CONF_VEHICLE_STATUS_*.
#
# Internal token names track the solver's vocabulary:
#   * lock  → "Aus"             (charging blocked)
#   * power → "An"              (max-power push)
#   * solar → "Solaroptimiert"  (PV-surplus follow)
# Any mode the user leaves blank simply doesn't get a button in iOS.
CONF_CHARGE_MODE_VALUE_LOCK = "charge_mode_value_lock"
CONF_CHARGE_MODE_VALUE_POWER = "charge_mode_value_power"
CONF_CHARGE_MODE_VALUE_SOLAR = "charge_mode_value_solar"

# Battery-only 4-mode dispatch values (v2.3+). Solver picks per slot
# between four modes; three of them map to a written value on the
# battery's `entity_charge_mode` (typically a number-entity taking
# e.g. +5000 / 0 / -5000 W). The fourth (`passive`) is the absence
# of a write — inverter falls back to its native PV-priority
# behaviour. Internal token names track the solver's vocabulary:
#   * charge    → force-charge from grid/PV at max power
#   * idle      → freeze SOC (also called "Aus")
#   * discharge → force-discharge into home/grid at max power
# Any mode the user leaves blank simply makes the worker suppress
# that branch — battery stays passive instead.
CONF_BATTERY_VALUE_CHARGE = "battery_value_charge"
CONF_BATTERY_VALUE_IDLE = "battery_value_idle"
CONF_BATTERY_VALUE_DISCHARGE = "battery_value_discharge"

# Wallbox-only ternary mapping for the vehicle-status sensor (v2.0+).
# Pre-v2.0 the connector forwarded the raw HA state string and the
# backend / iOS tried to interpret it from localized labels — fragile.
# Now the user maps each of their wallbox's possible states to one of
# three normalised values:
#   * 'plugged'    — a car is connected (charging / connected / paused / …)
#   * 'unplugged'  — no car (idle / disconnected / …)
#   * 'error'      — fault / authorisation error / cable error
# Coordinator normalises before pushing; mapping stays local to HA.
# If none of the three values is configured the connector falls back
# to passing the raw string (back-compat with pre-v2.0 config entries).
CONF_VEHICLE_STATUS_VALUE_PLUGGED = "vehicle_status_value_plugged"
CONF_VEHICLE_STATUS_VALUE_UNPLUGGED = "vehicle_status_value_unplugged"
CONF_VEHICLE_STATUS_VALUE_ERROR = "vehicle_status_value_error"

# Warmwater-only: the backend device id of the heating device that
# shares the same compressor as this warmwater device. Sent to the
# backend on POST /devices so the joint solver can enforce
# `P_heat + P_ww ≤ P_compressor_max`. Optional — leave empty for a
# standalone WW heater on a separate appliance.
CONF_SHARES_HARDWARE_WITH = "shares_hardware_with_device_id"

# Hold-mode for entity_control: how the connector handles devices whose
# Modbus registers / OCPP transaction state revert to a default after a
# few seconds (Kostal, SMA, some OCPP wallboxes …).
#
# "always" (default) — write once, then re-write every 30 s for as long
#                      as Crowdergize is active. Harmless on devices
#                      that hold fine; rescues those that don't.
# "never"            — write once, leave it alone. For low-traffic
#                      Modbus setups; assumes the device sticks.
#
# Legacy `auto` values stored in old config entries collapse to
# `always` at load time (see `_start_hold`) — we used to have a
# release-after-stable-checks third mode but it never paid off
# against the simple-and-safe periodic rewrite.
CONF_ENTITY_CONTROL_HOLD = "entity_control_hold"
ENTITY_CONTROL_HOLD_ALWAYS = "always"
ENTITY_CONTROL_HOLD_NEVER = "never"

# v2.4: classic-consumer flag for the haushalt double-counting fix.
# When True, the device's draw is already counted by the user's
# haushalt-sensor — the backend subtracts the device from the
# haushalt's reading on every snapshot fetch. Shown only for
# heating / warmwater / heatpump / wallbox / generic; solar / grid /
# battery / haushalt itself never carry it.
CONF_INCLUDED_IN_HAUSHALT = "included_in_haushalt"

# Hold loop timing. Initial 10 s lets the apply call's effect
# propagate before the first rewrite. 30 s catches most auto-revert
# cycles (Kostal's is 60 s).
HOLD_INITIAL_DELAY = 10
HOLD_POLL_INTERVAL = 30

# v2.4: separate, tighter hold cadence for the charge-mode entity
# (input_select for battery Lademodus + wallbox Lademodus). User-
# reported: some inverters reset the mode after ~15 s if not
# refreshed, so the 30 s entity_control cadence is too slow for
# this path. Initial delay matches HOLD_INITIAL_DELAY so the
# debounce-after-write rhythm stays consistent.
CHARGE_MODE_HOLD_INITIAL_DELAY = 10
CHARGE_MODE_HOLD_INTERVAL = 15

# v2.4.2: safety guard. If no SSE event (ping or data) has arrived in
# this many seconds, the hold-loop assumes Crowdergy is offline or
# unreachable and bails — the inverter's native logic regains control
# rather than being stuck on the last commanded mode. Backend sends
# `{"type":"ping"}` every 15 s, so 60 s = 4 missed pings = honest
# "Crowdergy is gone" signal without false-triggering on a single
# packet loss.
SSE_STALE_THRESHOLD_S = 60

PLATFORMS = ["sensor", "switch", "binary_sensor"]
