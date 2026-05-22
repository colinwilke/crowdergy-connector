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
    CONF_DEVICE_NAME,
    CONF_DEVICE_TYPE,
    CONF_DEVICES,
    CONF_DISTRICT,
    CONF_ENTITY_OUTDOOR_TEMP,
    CONF_EMAIL,
    CONF_ENTITY_CHARGE_MODE,
    CONF_ENTITY_CONTROL,
    CONF_ENTITY_POWER,
    CONF_ENTITY_SOC,
    CONF_ENTITY_VEHICLE_STATUS,
    CONF_ENTITY_CURRENT_TEMP,
    CONF_ENTITY_TARGET_TEMP,
    CONF_ENTITY_ENERGY_TOTAL,
    CONF_PASSWORD,
    CONF_REFRESH_TOKEN,
    CONF_REGION,
    CONF_USER_ID,
    CONF_ENTITY_CONTROL_HOLD,
    CONF_VALUE_OFF,
    CONF_VALUE_ON,
    ENTITY_CONTROL_HOLD_AUTO,
    ENTITY_CONTROL_HOLD_MODES,
    DEFAULT_API_URL,
    DEVICE_TYPES,
    DOMAIN,
    USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)

# German labels for the device-type picker.
DEVICE_TYPE_LABELS_DE = {
    "solar": "Solar",
    "battery": "Batterie",
    "wallbox": "Wallbox",
    "grid": "Netz",
    "heatpump": "Wärmepumpe",
    "generic": "Sonstiges",
    "haushalt": "Haushalt",
}

# Device types that the Crowdergy app can switch on/off through the
# user-mapped entity_control. Solar / Grid / Haushalt are read-only.
_CONTROLLABLE_TYPES = {"battery", "wallbox", "heatpump", "generic"}

# Entity domains where on/off is implicit (turn_on / turn_off services) —
# no value_on / value_off needs to be typed by the user.
_BINARY_DOMAINS = {"switch", "input_boolean", "light", "fan"}


def _is_binary_entity(entity_id: str) -> bool:
    if not entity_id:
        return False
    return entity_id.split(".", 1)[0] in _BINARY_DOMAINS

# Which read-side telemetry fields each device type exposes. Crowdergize-
# capable types additionally get the control trio (entity_control +
# value_on + value_off) rendered as a separate section.
_READ_FIELDS: dict[str, list[str]] = {
    "solar":     [CONF_ENTITY_POWER, CONF_ENTITY_ENERGY_TOTAL],
    "grid":      [CONF_ENTITY_POWER, CONF_ENTITY_ENERGY_TOTAL],
    "heatpump":  [
        CONF_ENTITY_POWER, CONF_ENTITY_CURRENT_TEMP, CONF_ENTITY_TARGET_TEMP,
        CONF_ENTITY_ENERGY_TOTAL,
    ],
    "haushalt":  [CONF_ENTITY_POWER, CONF_ENTITY_ENERGY_TOTAL],
    "battery":   [CONF_ENTITY_POWER, CONF_ENTITY_SOC, CONF_ENTITY_ENERGY_TOTAL],
    "wallbox":   [
        CONF_ENTITY_POWER, CONF_ENTITY_SOC, CONF_ENTITY_VEHICLE_STATUS,
        CONF_ENTITY_ENERGY_TOTAL,
    ],
    "generic":   [CONF_ENTITY_POWER, CONF_ENTITY_ENERGY_TOTAL],
}

# Entity-selector configs keyed by the CONF_ENTITY_* name.
_ENTITY_SELECTORS: dict[str, selector.EntitySelector] = {
    CONF_ENTITY_POWER: selector.EntitySelector(
        selector.EntitySelectorConfig(domain="sensor")
    ),
    CONF_ENTITY_SOC: selector.EntitySelector(
        selector.EntitySelectorConfig(domain="sensor")
    ),
    CONF_ENTITY_VEHICLE_STATUS: selector.EntitySelector(
        selector.EntitySelectorConfig(domain=["sensor", "binary_sensor"])
    ),
    CONF_ENTITY_CURRENT_TEMP: selector.EntitySelector(
        selector.EntitySelectorConfig(domain="sensor")
    ),
    CONF_ENTITY_TARGET_TEMP: selector.EntitySelector(
        selector.EntitySelectorConfig(domain=["sensor", "number", "input_number"])
    ),
    # Any settable HA entity — connector adapts the service call to the
    # entity's domain at runtime (switch.turn_on/off, number.set_value,
    # select.select_option, climate.set_hvac_mode, …).
    CONF_ENTITY_CONTROL: selector.EntitySelector(
        selector.EntitySelectorConfig(domain=[
            "switch", "input_boolean", "number", "select",
            "light", "fan", "climate", "input_number", "input_select",
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


def _entity_field(key: str, defaults: dict[str, Any]) -> Any:
    if defaults.get(key):
        return vol.Optional(key, default=defaults[key])
    return vol.Optional(key)


def _entities_schema(
    device_type: str, defaults: dict[str, Any] | None = None
) -> vol.Schema:
    """Step 2: entity-selector schema customised for the chosen device type.

    Layout:
      - Read-side telemetry fields at top (power, soc, vehicle_status as
        applicable) — always present at least with `entity_current_power_kw`.
      - For controllable types (battery / wallbox / heatpump / generic) a
        "Steuerung (Crowdergize)" section with the entity_control + value_on
        + value_off trio. The user picks any settable HA entity and the two
        string values Crowdergy should write to it for "Gerät an" / "Gerät aus".
    """
    d = defaults or {}
    read_fields = _READ_FIELDS.get(device_type, [CONF_ENTITY_POWER])
    schema_dict: dict[Any, Any] = {}

    if len(read_fields) == 1 and device_type not in _CONTROLLABLE_TYPES:
        # Single-purpose read-only types (solar/grid/haushalt): no
        # section wrapping — looks silly with one field.
        for key in read_fields:
            schema_dict[_entity_field(key, d)] = _ENTITY_SELECTORS[key]
    else:
        read_schema = vol.Schema(
            {_entity_field(key, d): _ENTITY_SELECTORS[key] for key in read_fields}
        )
        schema_dict[vol.Required("read_section")] = section(
            read_schema, {"collapsed": False}
        )

    if device_type == "wallbox":
        # Wallbox uses BOTH:
        #  - entity_charge_mode: user-driven Lademodus picker in the iOS app
        #    (manual: Lock / Power / Solar Pure / …)
        #  - entity_control + value_on/value_off (next step): future smart
        #    on/off when Crowdergize is active.
        # Either / both can stay empty if the user only wants one path.
        control_schema = vol.Schema({
            _entity_field(CONF_ENTITY_CHARGE_MODE, d):
                _ENTITY_SELECTORS[CONF_ENTITY_CHARGE_MODE],
            _entity_field(CONF_ENTITY_CONTROL, d): _ENTITY_SELECTORS[CONF_ENTITY_CONTROL],
        })
        schema_dict[vol.Required("control_section")] = section(
            control_schema, {"collapsed": False}
        )
    elif device_type in _CONTROLLABLE_TYPES:
        # Other controllable types (battery / heatpump / generic):
        # universal entity_control here, value_on/off in the follow-up
        # step where the schema can adapt to the entity domain.
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
    return None


def _values_schema(
    hass, entity_control: str, defaults: dict[str, Any]
) -> vol.Schema:
    """Step C schema: value_on + value_off, typed if entity_control supports it."""
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

    # Hold-mode is no longer exposed in the config flow (v1.20.0+).
    # All entity_control writes are kept fresh via the 30 s "always"
    # rewrite loop in the coordinator — the harmless extra HA write
    # rescues devices with hysteresis from getting stuck on first
    # apply (Warmwasser-WP with 7.5 °C hysteresis was the trigger).
    # Existing config entries still carry CONF_ENTITY_CONTROL_HOLD;
    # the coordinator treats `auto` the same as `always` so legacy
    # values keep working without a config-flow re-run.
    return vol.Schema({
        _field(CONF_VALUE_ON): field_type,
        _field(CONF_VALUE_OFF): field_type,
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
        CONF_ENTITY_POWER: entity_input.get(CONF_ENTITY_POWER, ""),
        CONF_ENTITY_SOC: entity_input.get(CONF_ENTITY_SOC, ""),
        CONF_ENTITY_VEHICLE_STATUS: entity_input.get(CONF_ENTITY_VEHICLE_STATUS, ""),
        CONF_ENTITY_CURRENT_TEMP: entity_input.get(CONF_ENTITY_CURRENT_TEMP, ""),
        CONF_ENTITY_TARGET_TEMP: entity_input.get(CONF_ENTITY_TARGET_TEMP, ""),
        CONF_ENTITY_ENERGY_TOTAL: entity_input.get(CONF_ENTITY_ENERGY_TOTAL, ""),
        CONF_ENTITY_CONTROL: entity_input.get(CONF_ENTITY_CONTROL, ""),
        CONF_VALUE_ON: entity_input.get(CONF_VALUE_ON, ""),
        CONF_VALUE_OFF: entity_input.get(CONF_VALUE_OFF, ""),
        CONF_ENTITY_CONTROL_HOLD: entity_input.get(
            CONF_ENTITY_CONTROL_HOLD, ENTITY_CONTROL_HOLD_ALWAYS
        ),
        CONF_ENTITY_CHARGE_MODE: entity_input.get(CONF_ENTITY_CHARGE_MODE, ""),
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
    device_config = {
        "name": device_name,
        "type": device_type,
        "district": location.get(CONF_DISTRICT, ""),
        "city": location.get(CONF_CITY, ""),
        "region": location.get(CONF_REGION, ""),
    }
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
) -> None:
    """PUT a device's mutable fields (name, type) to the backend."""
    payload = {"name": device_name, "type": device_type}
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


async def _delete_device_backend(api_url: str, token: str, device_id: str) -> None:
    """Delete a device from the backend."""
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
        # Entity-mapping kept between step 2 and step 3 (values).
        self._pending_entity_input: dict[str, Any] | None = None

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
            return await self.async_step_device_type()

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

    async def async_step_device_type(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: pick the device type + name."""
        if user_input is not None:
            self._pending_type = user_input[CONF_DEVICE_TYPE]
            self._pending_name = user_input[CONF_DEVICE_NAME]
            return await self.async_step_device_entities()

        return self.async_show_form(
            step_id="device_type",
            data_schema=_type_name_schema(),
            description_placeholders={"device_number": str(len(self._devices) + 1)},
        )

    async def async_step_device_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: type-specific entity mapping."""
        device_type = self._pending_type or "generic"
        device_name = self._pending_name or ""

        if user_input is not None:
            entity_input = _flatten_sections(user_input)
            # Step 3 only when entity_control is mapped AND it's not a
            # binary on/off entity (switch / input_boolean / light / fan)
            # — for those the connector uses turn_on / turn_off implicitly,
            # no value_on / value_off needs typing.
            entity_control = entity_input.get(CONF_ENTITY_CONTROL, "")
            needs_values = (
                device_type in _CONTROLLABLE_TYPES
                and entity_control
                and not _is_binary_entity(entity_control)
            )
            if needs_values:
                self._pending_entity_input = entity_input
                return await self.async_step_device_values()
            return await self._register_with_entities(entity_input)

        return self.async_show_form(
            step_id="device_entities",
            data_schema=_entities_schema(device_type),
            description_placeholders={
                "device_number": str(len(self._devices) + 1),
                "device_type": DEVICE_TYPE_LABELS_DE.get(device_type, device_type),
                "device_name": device_name,
            },
        )

    async def async_step_device_values(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 3: typ-bewusste value_on / value_off für entity_control."""
        device_type = self._pending_type or "generic"
        device_name = self._pending_name or ""
        entity_input = dict(self._pending_entity_input or {})
        entity_control = entity_input.get(CONF_ENTITY_CONTROL, "")

        if user_input is not None:
            entity_input[CONF_VALUE_ON] = user_input.get(CONF_VALUE_ON, "")
            entity_input[CONF_VALUE_OFF] = user_input.get(CONF_VALUE_OFF, "")
            # Hold-mode picker is no longer in the form (v1.20.0+) —
            # every device gets `always` so hysteresis-laden hardware
            # gets nudged every 30 s. Legacy entries with `auto` keep
            # working: the coordinator collapses both to `always`.
            entity_input[CONF_ENTITY_CONTROL_HOLD] = ENTITY_CONTROL_HOLD_ALWAYS
            return await self._register_with_entities(entity_input)

        return self.async_show_form(
            step_id="device_values",
            data_schema=_values_schema(self.hass, entity_control, {}),
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
            self._pending_entity_input = None
            return await self.async_step_add_more()
        except (httpx.HTTPStatusError, httpx.RequestError) as err:
            _LOGGER.error("Failed to register device: %s", err)
            errors["base"] = "cannot_connect"
            return self.async_show_form(
                step_id="device_entities",
                data_schema=_entities_schema(device_type),
                errors=errors,
            )

    async def async_step_add_more(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
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
            return await self.async_step_add_device_entities()

        return self.async_show_form(
            step_id="add_device",
            data_schema=_type_name_schema(),
        )

    async def async_step_add_device_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2 (add): type-specific entity mapping."""
        device_type = self._pending_type or "generic"
        device_name = self._pending_name or ""

        if user_input is not None:
            entity_input = _flatten_sections(user_input)
            entity_control = entity_input.get(CONF_ENTITY_CONTROL, "")
            needs_values = (
                device_type in _CONTROLLABLE_TYPES
                and entity_control
                and not _is_binary_entity(entity_control)
            )
            if needs_values:
                self._pending_entity_input = entity_input
                return await self.async_step_add_device_values()
            return await self._options_register(entity_input)

        return self.async_show_form(
            step_id="add_device_entities",
            data_schema=_entities_schema(device_type),
            description_placeholders={
                "device_type": DEVICE_TYPE_LABELS_DE.get(device_type, device_type),
                "device_name": device_name,
            },
        )

    async def async_step_add_device_values(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 3 (add): typ-bewusste value_on / value_off."""
        device_type = self._pending_type or "generic"
        device_name = self._pending_name or ""
        entity_input = dict(self._pending_entity_input or {})
        entity_control = entity_input.get(CONF_ENTITY_CONTROL, "")

        if user_input is not None:
            entity_input[CONF_VALUE_ON] = user_input.get(CONF_VALUE_ON, "")
            entity_input[CONF_VALUE_OFF] = user_input.get(CONF_VALUE_OFF, "")
            # Hold-mode picker is no longer in the form (v1.20.0+) —
            # every device gets `always` so hysteresis-laden hardware
            # gets nudged every 30 s. Legacy entries with `auto` keep
            # working: the coordinator collapses both to `always`.
            entity_input[CONF_ENTITY_CONTROL_HOLD] = ENTITY_CONTROL_HOLD_ALWAYS
            return await self._options_register(entity_input)

        return self.async_show_form(
            step_id="add_device_values",
            data_schema=_values_schema(self.hass, entity_control, {}),
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
            self._pending_entity_input = None
            return await self.async_step_init()
        except (httpx.HTTPStatusError, httpx.RequestError) as err:
            _LOGGER.error("Failed to register device: %s", err)
            errors["base"] = "cannot_connect"
            return self.async_show_form(
                step_id="add_device_entities",
                data_schema=_entities_schema(device_type),
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
            return await self.async_step_edit_device_type()

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

    async def async_step_edit_device_type(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit-Step 1: type + name with the stored values pre-filled."""
        target = next(
            (d for d in self._devices if d.get(CONF_DEVICE_ID) == self._edit_target_id),
            None,
        )
        if target is None:
            return await self.async_step_init()

        if user_input is not None:
            self._edit_pending_type = user_input[CONF_DEVICE_TYPE]
            self._edit_pending_name = user_input[CONF_DEVICE_NAME]
            return await self.async_step_edit_device_entities()

        return self.async_show_form(
            step_id="edit_device_type",
            data_schema=_type_name_schema(defaults=target),
            description_placeholders={
                "device_name": target.get(CONF_DEVICE_NAME, ""),
            },
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
            entity_input = _flatten_sections(user_input)
            new_entity_control = entity_input.get(CONF_ENTITY_CONTROL, "")
            old_entity_control = target.get(CONF_ENTITY_CONTROL, "")
            # Step 3 only when entity_control is mapped AND non-binary.
            # Switch/Light/Fan/Input-Boolean skip the values step since
            # turn_on / turn_off is implicit.
            needs_values = (
                device_type in _CONTROLLABLE_TYPES
                and new_entity_control
                and not _is_binary_entity(new_entity_control)
            )
            if needs_values:
                if new_entity_control != old_entity_control:
                    # Remapped — drop old values, force step 3 to start fresh.
                    entity_input.pop(CONF_VALUE_ON, None)
                    entity_input.pop(CONF_VALUE_OFF, None)
                else:
                    # Same entity — carry stored values into step 3 defaults.
                    entity_input[CONF_VALUE_ON] = target.get(CONF_VALUE_ON, "")
                    entity_input[CONF_VALUE_OFF] = target.get(CONF_VALUE_OFF, "")
                # Hold-mode survives a remap (it's about the device, not
                # the specific entity), so always carry it forward.
                entity_input[CONF_ENTITY_CONTROL_HOLD] = target.get(
                    CONF_ENTITY_CONTROL_HOLD, ENTITY_CONTROL_HOLD_ALWAYS
                )
                self._edit_pending_entity_input = entity_input
                return await self.async_step_edit_device_values()
            return await self._edit_save(target, entity_input)

        return self.async_show_form(
            step_id="edit_device_entities",
            data_schema=_entities_schema(device_type, defaults=target),
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

        if user_input is not None:
            entity_input[CONF_VALUE_ON] = user_input.get(CONF_VALUE_ON, "")
            entity_input[CONF_VALUE_OFF] = user_input.get(CONF_VALUE_OFF, "")
            # Hold-mode picker is no longer in the form (v1.20.0+) —
            # every device gets `always` so hysteresis-laden hardware
            # gets nudged every 30 s. Legacy entries with `auto` keep
            # working: the coordinator collapses both to `always`.
            entity_input[CONF_ENTITY_CONTROL_HOLD] = ENTITY_CONTROL_HOLD_ALWAYS
            return await self._edit_save(target, entity_input)

        return self.async_show_form(
            step_id="edit_device_values",
            data_schema=_values_schema(self.hass, entity_control, entity_input),
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
                data_schema=_entities_schema(device_type, defaults=target),
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
                await _delete_device_backend(
                    self._entry.data[CONF_API_URL],
                    self._entry.data[CONF_ACCESS_TOKEN],
                    device_id,
                )
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
