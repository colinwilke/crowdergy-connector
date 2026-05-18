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
# "auto" (default)  — write once, check after 10 s. If the value
#                     reverted, write again and start re-writing every
#                     30 s. Three stable checks in a row → assume the
#                     device holds and stop polling. Crowdergize OFF
#                     stops it unconditionally.
# "always"          — write once, then re-write every 30 s for as long
#                     as Crowdergize is active. No check, no early stop.
# "never"           — write once, leave it alone. For devices that
#                     reliably hold and where Modbus traffic should be
#                     minimised.
CONF_ENTITY_CONTROL_HOLD = "entity_control_hold"
ENTITY_CONTROL_HOLD_AUTO = "auto"
ENTITY_CONTROL_HOLD_ALWAYS = "always"
ENTITY_CONTROL_HOLD_NEVER = "never"
ENTITY_CONTROL_HOLD_MODES = (
    ENTITY_CONTROL_HOLD_AUTO,
    ENTITY_CONTROL_HOLD_ALWAYS,
    ENTITY_CONTROL_HOLD_NEVER,
)

# Hold loop timing. Initial 10 s gives the device time to apply the
# first write before we start checking. 30 s is fast enough to react
# to most auto-revert cycles (Kostal's is 60 s).
HOLD_INITIAL_DELAY = 10
HOLD_POLL_INTERVAL = 30
HOLD_AUTO_STABLE_HITS = 3

PLATFORMS = ["sensor", "switch"]
