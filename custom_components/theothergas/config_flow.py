"""Config flow for Crowdergy Connector integration."""
from __future__ import annotations

import logging
from typing import Any

import httpx
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
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


def _device_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Build the add/edit device form schema.

    Order (top-to-bottom):
      1. Gerätetyp
      2. Name
      3. Leistungsdaten (read-only):
           - aktuelle Leistung in Watt
           - aktueller Ladezustand (battery / wallbox)
           - Fahrzeugstatus (wallbox)
           - aktiv-Sensor / Schalter
      4. Steuerungsparameter (battery / wallbox — written by Crowdergy):
           - minimaler Ladezustand
           - maximaler Ladezustand
           - Lademodus (wallbox)
    """
    d = defaults or {}

    def required_key(key: str) -> Any:
        if d.get(key) is not None:
            return vol.Required(key, default=d[key])
        return vol.Required(key)

    def optional_key(key: str) -> Any:
        if d.get(key):
            return vol.Optional(key, default=d[key])
        return vol.Optional(key)

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

    return vol.Schema(
        {
            required_key(CONF_DEVICE_TYPE): type_selector,
            required_key(CONF_DEVICE_NAME): str,
            # ── Leistungsdaten (nur lesend) ─────────────────────────
            optional_key(CONF_ENTITY_POWER): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            ),
            optional_key(CONF_ENTITY_SOC): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            ),
            optional_key(CONF_ENTITY_VEHICLE_STATUS): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["sensor", "binary_sensor"])
            ),
            optional_key(CONF_ENTITY_ACTIVE): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain=["switch", "binary_sensor", "input_boolean"]
                )
            ),
            # ── Steuerungsparameter (Batterie + Wallbox) ───────────
            optional_key(CONF_ENTITY_SOC_MIN): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="number")
            ),
            optional_key(CONF_ENTITY_SOC_MAX): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="number")
            ),
            optional_key(CONF_ENTITY_CHARGE_MODE): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="select")
            ),
        }
    )


def _build_device_record(
    backend_device_id: str, device_input: dict[str, Any]
) -> dict[str, Any]:
    """Map a submitted form into the dict we persist on the config entry."""
    return {
        CONF_DEVICE_ID: backend_device_id,
        CONF_DEVICE_NAME: device_input[CONF_DEVICE_NAME],
        CONF_DEVICE_TYPE: device_input[CONF_DEVICE_TYPE],
        CONF_ENTITY_POWER: device_input.get(CONF_ENTITY_POWER, ""),
        CONF_ENTITY_SOC: device_input.get(CONF_ENTITY_SOC, ""),
        CONF_ENTITY_ACTIVE: device_input.get(CONF_ENTITY_ACTIVE, ""),
        CONF_ENTITY_SOC_MIN: device_input.get(CONF_ENTITY_SOC_MIN, ""),
        CONF_ENTITY_SOC_MAX: device_input.get(CONF_ENTITY_SOC_MAX, ""),
        CONF_ENTITY_VEHICLE_STATUS: device_input.get(CONF_ENTITY_VEHICLE_STATUS, ""),
        CONF_ENTITY_CHARGE_MODE: device_input.get(CONF_ENTITY_CHARGE_MODE, ""),
    }


async def _register_device(
    api_url: str, token: str, device_input: dict[str, Any], location: dict[str, str]
) -> dict[str, Any]:
    """Register a device on the backend and return the full device dict."""
    device_config = {
        "name": device_input[CONF_DEVICE_NAME],
        "type": device_input[CONF_DEVICE_TYPE],
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

    return _build_device_record(result["id"], device_input)


async def _update_device_backend(
    api_url: str, token: str, device_id: str, device_input: dict[str, Any]
) -> None:
    """PUT a device's mutable fields (name, type) to the backend."""
    payload = {
        "name": device_input[CONF_DEVICE_NAME],
        "type": device_input[CONF_DEVICE_TYPE],
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.put(
            f"{api_url}/api/v1/devices/{device_id}",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
        )
        response.raise_for_status()


async def _delete_device_backend(api_url: str, token: str, device_id: str) -> None:
    """Delete a device from the backend."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.delete(
            f"{api_url}/api/v1/devices/{device_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()


# ── Initial Config Flow ─────────────────────────────────────────────────────


class TheOtherGasConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Crowdergy Connector."""

    VERSION = 2

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._devices: list[dict[str, Any]] = []

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
            return await self.async_step_device()

        return self.async_show_form(
            step_id="location",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_DISTRICT, default=""): str,
                    vol.Optional(CONF_CITY, default=""): str,
                    vol.Optional(CONF_REGION, default=""): str,
                }
            ),
        )

    async def async_step_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                dev = await _register_device(
                    self._data[CONF_API_URL],
                    self._data[CONF_ACCESS_TOKEN],
                    user_input,
                    self._data,
                )
                self._devices.append(dev)
                return await self.async_step_add_more()
            except (httpx.HTTPStatusError, httpx.RequestError) as err:
                _LOGGER.error("Failed to register device: %s", err)
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="device",
            data_schema=_device_schema(),
            errors=errors,
            description_placeholders={"device_number": str(len(self._devices) + 1)},
        )

    async def async_step_add_more(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="add_more",
            menu_options=["device", "finish"],
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
        # Which device the user is currently editing.
        self._edit_target_id: str | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=["add_device", "edit_device", "remove_device", "done"],
        )

    async def async_step_add_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                dev = await _register_device(
                    self._entry.data[CONF_API_URL],
                    self._entry.data[CONF_ACCESS_TOKEN],
                    user_input,
                    self._entry.data,
                )
                self._devices.append(dev)
                return await self.async_step_init()
            except (httpx.HTTPStatusError, httpx.RequestError) as err:
                _LOGGER.error("Failed to register device: %s", err)
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="add_device",
            data_schema=_device_schema(),
            errors=errors,
        )

    async def async_step_edit_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick which device to edit."""
        if not self._devices:
            return await self.async_step_init()

        if user_input is not None:
            self._edit_target_id = user_input["device_to_edit"]
            return await self.async_step_edit_device_form()

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

    async def async_step_edit_device_form(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit the entity mappings + name + type of the previously chosen device."""
        errors: dict[str, str] = {}
        target = next(
            (d for d in self._devices if d.get(CONF_DEVICE_ID) == self._edit_target_id),
            None,
        )
        if target is None:
            return await self.async_step_init()

        if user_input is not None:
            try:
                await _update_device_backend(
                    self._entry.data[CONF_API_URL],
                    self._entry.data[CONF_ACCESS_TOKEN],
                    target[CONF_DEVICE_ID],
                    user_input,
                )
                # Replace the device record in-place, preserving its backend id.
                updated = _build_device_record(target[CONF_DEVICE_ID], user_input)
                self._devices = [
                    updated if d[CONF_DEVICE_ID] == target[CONF_DEVICE_ID] else d
                    for d in self._devices
                ]
                self._edit_target_id = None
                return await self.async_step_init()
            except (httpx.HTTPStatusError, httpx.RequestError) as err:
                _LOGGER.error("Failed to update device: %s", err)
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="edit_device_form",
            data_schema=_device_schema(defaults=target),
            errors=errors,
            description_placeholders={
                "device_name": target.get(CONF_DEVICE_NAME, ""),
            },
        )

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
