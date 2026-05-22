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
DEVICE_TYPES = ["solar", "battery", "wallbox", "grid", "heatpump", "generic", "haushalt"]
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
# Battery-only optional second energy meter for the discharge side.
# Batteries are bidirectional and HA typically exposes the two flows
# as two distinct `total_increasing` sensors — the existing
# `entity_energy_total` carries one (the user picks which: charged
# vs. discharged depending on their integration), and this new
# field carries the OTHER so the backend can split the streams and
# the iOS chart can render charge / discharge separately. NOT
# offered for non-battery types.
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

# Hold loop timing. Initial 10 s lets the apply call's effect
# propagate before the first rewrite. 30 s catches most auto-revert
# cycles (Kostal's is 60 s).
HOLD_INITIAL_DELAY = 10
HOLD_POLL_INTERVAL = 30

PLATFORMS = ["sensor", "switch"]
