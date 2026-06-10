"""Box-Services (Crowdergy Box, Phase 3).

Zwei Services für den box-manager der Crowdergy-Appliance, damit er
nach der KOSTAL-Provisionierung (offizieller `kostal_plenticore`-
Config-Flow, von der Box über HAs REST-API getrieben) Geräte headless
anlegen kann:

* `box_discover_devices` (response-only): führt exakt die Discovery
  aus, die auch das interaktive Auto-Setup nutzt — Heuristik +
  Backend-LLM-Fallback (`entity_mapper.discover_devices_with_llm`) —
  und liefert die Vorschläge als Service-Response. Ändert nichts.
* `box_add_device`: registriert EIN Gerät am Backend (zentrale
  `device_field_spec`-Payload, derselbe Pfad wie der interaktive
  Flow) und hängt es an den Config-Entry; Entry wird neu geladen.

Beide setzen einen bestehenden Config-Entry voraus (Box-Pairing,
Phase 2). Wie `provision_box` sind sie nur aktiv, wenn die Appliance
`theothergas:` in ihrer configuration.yaml lädt.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_API_URL,
    CONF_CITY,
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    CONF_DEVICE_TYPE,
    CONF_DEVICES,
    CONF_DISTRICT,
    CONF_REGION,
    DEVICE_TYPES,
    DOMAIN,
    MAPPING_LLM_ENABLED,
    USER_AGENT,
)
from .device_field_spec import build_payload
from .entity_mapper import discover_devices, discover_devices_with_llm

_LOGGER = logging.getLogger(__name__)

SERVICE_BOX_DISCOVER = "box_discover_devices"
SERVICE_BOX_ADD_DEVICE = "box_add_device"

BOX_ADD_DEVICE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_DEVICE_TYPE): vol.In(DEVICE_TYPES),
        vol.Required(CONF_DEVICE_NAME): cv.string,
        # slot -> entity_id; Slots = CONF_ENTITY_*/CONF_VALUE_*-Keys,
        # unbekannte Keys ignoriert _build_device_record ohnehin.
        vol.Required("entities"): vol.Schema({cv.string: cv.string}),
    }
)


def _get_entry(hass: HomeAssistant) -> ConfigEntry:
    entries = hass.config_entries.async_entries(DOMAIN)
    if not entries:
        raise HomeAssistantError(
            "no Crowdergy config entry — pair the box first (provision_box)"
        )
    return entries[0]


async def _discover(hass: HomeAssistant, entry: ConfigEntry):
    if MAPPING_LLM_ENABLED:
        return await discover_devices_with_llm(
            hass,
            entry.data[CONF_API_URL],
            entry.data[CONF_ACCESS_TOKEN],
            USER_AGENT,
        )
    return await discover_devices(hass)


def async_register_box_services(hass: HomeAssistant) -> None:
    async def _handle_discover(call: ServiceCall) -> ServiceResponse:
        entry = _get_entry(hass)
        groups = await _discover(hass, entry)
        already_mapped = {
            d.get(CONF_DEVICE_NAME) for d in entry.data.get(CONF_DEVICES, [])
        }
        return {
            "devices": [
                {
                    CONF_DEVICE_TYPE: g.suggested_type,
                    CONF_DEVICE_NAME: g.suggested_name,
                    "confidence": round(g.avg_confidence, 3),
                    "entities": g.slot_map(),
                    "already_added": g.suggested_name in already_mapped,
                }
                for g in groups
            ]
        }

    async def _handle_add_device(call: ServiceCall) -> ServiceResponse:
        # Import hier statt Modulkopf: config_flow ist groß und wird
        # sonst bei jedem HA-Boot mitgeladen, obwohl die Box-Services
        # auf normalen Installationen nie aufgerufen werden.
        from .config_flow import _authenticated_config_request, _build_device_record

        entry = _get_entry(hass)
        device_type: str = call.data[CONF_DEVICE_TYPE]
        device_name: str = call.data[CONF_DEVICE_NAME]
        entity_input: dict[str, Any] = dict(call.data["entities"])

        payload = build_payload(
            mode="create",
            dtype=device_type,
            name=device_name,
            entity_input=entity_input,
            extra={
                CONF_DISTRICT: entry.data.get(CONF_DISTRICT, ""),
                CONF_CITY: entry.data.get(CONF_CITY, ""),
                CONF_REGION: entry.data.get(CONF_REGION, ""),
            },
        )
        response = await _authenticated_config_request(
            hass, entry, "POST", "/api/v1/devices", json=payload
        )
        if response.status_code >= 400:
            raise HomeAssistantError(
                f"backend device registration failed: {response.status_code}"
            )

        dev = _build_device_record(
            response.json()["id"], device_type, device_name, entity_input
        )
        devices = [*entry.data.get(CONF_DEVICES, []), dev]
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_DEVICES: devices}
        )
        await hass.config_entries.async_reload(entry.entry_id)
        _LOGGER.info(
            "box_add_device: registered %s (%s)", device_name, device_type
        )
        return {
            CONF_DEVICE_ID: dev[CONF_DEVICE_ID],
            CONF_DEVICE_TYPE: device_type,
            CONF_DEVICE_NAME: device_name,
        }

    hass.services.async_register(
        DOMAIN,
        SERVICE_BOX_DISCOVER,
        _handle_discover,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_BOX_ADD_DEVICE,
        _handle_add_device,
        schema=BOX_ADD_DEVICE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
