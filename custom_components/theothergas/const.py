"""Constants for the Crowdergy Connector integration."""

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
DEVICE_TYPES = ["solar", "battery", "wallbox", "grid", "heatpump", "generic"]
UPDATE_INTERVAL = 30

CONF_DEVICE_NAME = "device_name"
CONF_DEVICE_TYPE = "device_type"
CONF_DISTRICT = "district"
CONF_CITY = "city"
CONF_REGION = "region"
CONF_ENTITY_POWER = "entity_current_power_kw"
CONF_ENTITY_SOC = "entity_soc_percent"
CONF_ENTITY_ACTIVE = "entity_is_active"
# Settable number entities that the Crowdergy app writes to via commands.
CONF_ENTITY_SOC_MIN = "entity_soc_min_percent"
CONF_ENTITY_SOC_MAX = "entity_soc_max_percent"
# Wallbox-only: read-only vehicle status string + settable charge mode (select).
CONF_ENTITY_VEHICLE_STATUS = "entity_vehicle_status"
CONF_ENTITY_CHARGE_MODE = "entity_charge_mode"

# Allowed charge mode values (must match the options exposed by the HA
# select entity the user maps to CONF_ENTITY_CHARGE_MODE).
CHARGE_MODE_OPTIONS = ["Lock Mode", "Power Mode", "Solar Pure Mode"]

PLATFORMS = ["sensor", "switch"]
