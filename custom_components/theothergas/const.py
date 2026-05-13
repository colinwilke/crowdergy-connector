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

PLATFORMS = ["sensor", "switch"]
