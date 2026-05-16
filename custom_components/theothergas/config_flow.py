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
    CHARGE_MODE_OPTIONS,
    CONF_ACCESS_TOKEN,
    CONF_API_URL,
    CONF_CITY,
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    CONF_DEVICE_TYPE,
    CONF_DEVICES,
    CONF_DISTRICT,
    CONF_EMAIL,
    CONF_ENTITY_ACTIVE,
    CONF_ENTITY_CHARGE_MODE,
    CONF_ENTITY_POWER,
    CONF_ENTITY_SOC,
    CONF_ENTITY_SOC_MAX,
    CONF_ENTITY_SOC_MIN,
    CONF_ENTITY_VEHICLE_STATUS,
    CONF_PASSWORD,
    CONF_REFRESH_TOKEN,
    CONF_REGION,
    CONF_USER_ID,
    DEFAULT_API_URL,
    DEVICE_TYPES,
    DOMAIN,
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
}

# Which fields each device type exposes in the entity-mapping step.
# `read_section`  → Felder unter "Leistungsdaten (nur lesend)"
# `write_section` → Felder unter "Steuerungsparameter (werden von Crowdergy gesetzt)"
# `flat`          → Felder, die ohne Sektions-Wrapper angezeigt werden
#                   (für Typen mit nur einem Feld, wo eine einsame Sektion albern wäre)
_TYPE_FIELDS: dict[str, dict[str, list[str]]] = {
    "solar": {
        "flat": [CONF_ENTITY_POWER],
    },
    "grid": {
        "flat": [CONF_ENTITY_POWER],
    },
    "heatpump": {
        "flat": [CONF_ENTITY_POWER],
    },
    "battery": {
        "read_section": [CONF_ENTITY_POWER, CONF_ENTITY_SOC],
        "write_section": [CONF_ENTITY_SOC_MIN, CONF_ENTITY_SOC_MAX],
    },
    "wallbox": {
        "read_section": [
            CONF_ENTITY_POWER,
            CONF_ENTITY_SOC,
            CONF_ENTITY_VEHICLE_STATUS,
        ],
        "write_section": [
            CONF_ENTITY_SOC_MIN,
            CONF_ENTITY_SOC_MAX,
            CONF_ENTITY_CHARGE_MODE,
        ],
    },
    "generic": {
        "flat": [CONF_ENTITY_POWER, CONF_ENTITY_ACTIVE],
    },
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
    CONF_ENTITY_ACTIVE: selector.EntitySelector(
        selector.EntitySelectorConfig(domain=["switch", "input_boolean"])
    ),
    CONF_ENTITY_SOC_MIN: selector.EntitySelector(
        selector.EntitySelectorConfig(domain="number")
    ),
    CONF_ENTITY_SOC_MAX: selector.EntitySelector(
        selector.EntitySelectorConfig(domain="number")
    ),
    CONF_ENTITY_CHARGE_MODE: selector.EntitySelector(
        selector.EntitySelectorConfig(domain="select")
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

    Wraps fields in `section()` blocks when both Leistungs- AND
    Steuerungs-Felder vorhanden sind (Batterie + Wallbox).
    For single-purpose types (Solar/Grid/Heatpump/Sonstiges) the fields
    sit at the top level — eine einsame Sektion wäre nur Lärm.
    """
    d = defaults or {}
    fields_map = _TYPE_FIELDS.get(device_type, {})
    schema_dict: dict[Any, Any] = {}

    if "flat" in fields_map:
        for key in fields_map["flat"]:
            schema_dict[_entity_field(key, d)] = _ENTITY_SELECTORS[key]

    if "read_section" in fields_map:
        read_schema = vol.Schema(
            {
                _entity_field(key, d): _ENTITY_SELECTORS[key]
                for key in fields_map["read_section"]
            }
        )
        schema_dict[vol.Required("read_section")] = section(
            read_schema, {"collapsed": False}
        )

    if "write_section" in fields_map:
        write_schema = vol.Schema(
            {
                _entity_field(key, d): _ENTITY_SELECTORS[key]
                for key in fields_map["write_section"]
            }
        )
        schema_dict[vol.Required("write_section")] = section(
            write_schema, {"collapsed": False}
        )

    return vol.Schema(schema_dict)


def _flatten_sections(user_input: dict[str, Any]) -> dict[str, Any]:
    """Lift nested section dicts back into a flat mapping the persistence layer expects.

    `section(...)` wraps its fields under the section's key in the result;
    everything else (and any flat fields) is passed through unchanged.
    """
    flat: dict[str, Any] = {}
    for key, value in user_input.items():
        if key in ("read_section", "write_section") and isinstance(value, dict):
            flat.update(value)
        else:
            flat[key] = value
    return flat


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
        CONF_ENTITY_ACTIVE: entity_input.get(CONF_ENTITY_ACTIVE, ""),
        CONF_ENTITY_SOC_MIN: entity_input.get(CONF_ENTITY_SOC_MIN, ""),
        CONF_ENTITY_SOC_MAX: entity_input.get(CONF_ENTITY_SOC_MAX, ""),
        CONF_ENTITY_VEHICLE_STATUS: entity_input.get(CONF_ENTITY_VEHICLE_STATUS, ""),
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
        # Nominatim's usage policy requires a descriptive UA on every request.
        "User-Agent": "crowdergy-connector/1.6.0 (+https://github.com/colinwilke/crowdergy-connector)"
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


class TheOtherGasConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Crowdergy Connector."""

    VERSION = 2

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._devices: list[dict[str, Any]] = []
        # Carry the device-type/name picked in step 1 into step 2.
        self._pending_type: str | None = None
        self._pending_name: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> TheOtherGasOptionsFlow:
        return TheOtherGasOptionsFlow(config_entry)

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
        errors: dict[str, str] = {}
        device_type = self._pending_type or "generic"
        device_name = self._pending_name or ""

        if user_input is not None:
            try:
                entity_input = _flatten_sections(user_input)
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
                return await self.async_step_add_more()
            except (httpx.HTTPStatusError, httpx.RequestError) as err:
                _LOGGER.error("Failed to register device: %s", err)
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="device_entities",
            data_schema=_entities_schema(device_type),
            errors=errors,
            description_placeholders={
                "device_number": str(len(self._devices) + 1),
                "device_type": DEVICE_TYPE_LABELS_DE.get(device_type, device_type),
                "device_name": device_name,
            },
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


class TheOtherGasOptionsFlow(OptionsFlow):
    """Handle options for Crowdergy Connector."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry
        self._devices: list[dict[str, Any]] = list(
            config_entry.data.get(CONF_DEVICES, [])
        )
        # Add-flow scratch state.
        self._pending_type: str | None = None
        self._pending_name: str | None = None
        # Edit-flow scratch state.
        self._edit_target_id: str | None = None
        self._edit_pending_type: str | None = None
        self._edit_pending_name: str | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=["add_device", "edit_device", "remove_device", "done"],
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
        errors: dict[str, str] = {}
        device_type = self._pending_type or "generic"
        device_name = self._pending_name or ""

        if user_input is not None:
            try:
                entity_input = _flatten_sections(user_input)
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
                return await self.async_step_init()
            except (httpx.HTTPStatusError, httpx.RequestError) as err:
                _LOGGER.error("Failed to register device: %s", err)
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="add_device_entities",
            data_schema=_entities_schema(device_type),
            errors=errors,
            description_placeholders={
                "device_type": DEVICE_TYPE_LABELS_DE.get(device_type, device_type),
                "device_name": device_name,
            },
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
        errors: dict[str, str] = {}
        target = next(
            (d for d in self._devices if d.get(CONF_DEVICE_ID) == self._edit_target_id),
            None,
        )
        if target is None:
            return await self.async_step_init()

        device_type = self._edit_pending_type or target[CONF_DEVICE_TYPE]
        device_name = self._edit_pending_name or target[CONF_DEVICE_NAME]

        if user_input is not None:
            try:
                entity_input = _flatten_sections(user_input)
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
                return await self.async_step_init()
            except (httpx.HTTPStatusError, httpx.RequestError) as err:
                _LOGGER.error("Failed to update device: %s", err)
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="edit_device_entities",
            data_schema=_entities_schema(device_type, defaults=target),
            errors=errors,
            description_placeholders={
                "device_type": DEVICE_TYPE_LABELS_DE.get(device_type, device_type),
                "device_name": device_name,
            },
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
