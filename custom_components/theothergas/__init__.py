"""The Crowdergy integration."""
from __future__ import annotations

import logging

import httpx
import voluptuous as vol
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_API_URL,
    CONF_DEVICE_ID,
    CONF_DEVICES,
    CONF_EMAIL,
    CONF_REFRESH_TOKEN,
    CONF_USER_ID,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import CrowdergyCoordinator

_LOGGER = logging.getLogger(__name__)

type CrowdergyConfigEntry = ConfigEntry

# Box-Provisioning (Phase 2): der Service existiert, damit der
# box-manager der Crowdergy Box den Import-Flow über HAs normale
# Service-REST-API starten kann (Import-Flows sind über
# /api/config/config_entries/flow nicht erreichbar). Die Box aktiviert
# ihn via `theothergas:` in ihrer configuration.yaml; normale
# HACS-Installationen ohne YAML-Key bleiben unverändert.
SERVICE_PROVISION_BOX = "provision_box"
SERVICE_PROVISION_BOX_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ACCESS_TOKEN): cv.string,
        vol.Required(CONF_REFRESH_TOKEN): cv.string,
        vol.Required(CONF_USER_ID): cv.string,
        vol.Optional(CONF_API_URL): cv.string,
        vol.Optional(CONF_EMAIL): cv.string,
        # Consent-Flags (Phase 4/5): der Box-Wizard erfasst Consent vor
        # dem Pairing; sie landen atomar als Entry-Options (siehe
        # provisioning.extract_consent_options).
        vol.Optional("consent_telemetry"): cv.boolean,
        vol.Optional("consent_remote_control"): cv.boolean,
    }
)

CONFIG_SCHEMA = vol.Schema(
    {vol.Optional(DOMAIN): vol.Schema({})}, extra=vol.ALLOW_EXTRA
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """YAML-loses Setup — registriert nur den Provisioning-Service."""

    async def _handle_provision_box(call: ServiceCall) -> None:
        # Tokens nie loggen — auch nicht im Fehlerfall (das Schema
        # oben hat vorher validiert; Abort-Reasons enthalten keine
        # Token-Werte).
        _LOGGER.info("provision_box service called, starting import flow")
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_IMPORT}, data=dict(call.data)
        )
        # already_configured = Re-Pairing, Tokens wurden im bestehenden
        # Entry aktualisiert — für den Aufrufer ein Erfolg.
        if result.get("type") == "abort" and result.get("reason") != (
            "already_configured"
        ):
            raise HomeAssistantError(
                f"provisioning aborted: {result.get('reason')}"
            )

    hass.services.async_register(
        DOMAIN,
        SERVICE_PROVISION_BOX,
        _handle_provision_box,
        schema=SERVICE_PROVISION_BOX_SCHEMA,
    )

    # Phase 3: Discovery + Geräteanlage für die Box (box_services.py).
    from .box_services import async_register_box_services

    async_register_box_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: CrowdergyConfigEntry) -> bool:
    """Set up Crowdergy from a config entry."""
    coordinator = CrowdergyCoordinator(hass, entry)
    # v3.5.1: blocking I/O (httpx-SSL-Cert-Load + manifest-Read) wird
    # ins Executor verlagert — HA 2024.x meckert sonst ueber blocking
    # calls im event loop.
    await coordinator.async_init()

    await coordinator.async_config_entry_first_refresh()
    coordinator.setup_listeners()
    coordinator.start_sse_listener()
    coordinator.start_heartbeat()
    coordinator.start_device_mirror()
    coordinator.start_state_resync()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: CrowdergyConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        coordinator: CrowdergyCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()

    return unload_ok


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    config_entry: CrowdergyConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Allow the user to delete a device card directly in the HA UI.

    Deletes the device on the Crowdergy backend, drops it from the config
    entry's devices list, then lets HA finish removing the registry entry.
    Returning True signals HA to proceed with the removal.
    """
    # Pull the Crowdergy device-id from the (DOMAIN, …) identifier tuple.
    crowdergy_device_id: str | None = None
    for domain, identifier in device_entry.identifiers:
        if domain == DOMAIN:
            crowdergy_device_id = identifier
            break

    if crowdergy_device_id is None:
        # No Crowdergy identifier — nothing to talk to the backend about.
        return True

    # Cluster B Connector (2026-06-09): vorher hatte dieser Pfad einen
    # eigenen httpx-Client ohne 401-Refresh — bei abgelaufenem Token
    # blieb das Device backend-seitig als Orphan zurück. Jetzt
    # delegiert er an `coordinator.delete_device_backend()` der den
    # selben authentifizierten Request-Pfad nutzt wie alles andere.
    coordinator: CrowdergyCoordinator | None = (
        hass.data.get(DOMAIN, {}).get(config_entry.entry_id)
    )
    if coordinator is not None:
        try:
            await coordinator.delete_device_backend(crowdergy_device_id)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Backend delete for %s raised %s — removing locally anyway",
                crowdergy_device_id, err,
            )

    # Drop the device from our persisted config-entry list so the
    # coordinator stops polling/pushing for it on the next reload.
    devices = [
        d for d in config_entry.data.get(CONF_DEVICES, [])
        if d.get(CONF_DEVICE_ID) != crowdergy_device_id
    ]
    new_data = {**config_entry.data, CONF_DEVICES: devices}
    hass.config_entries.async_update_entry(config_entry, data=new_data)

    # Prune the coordinator's per-device bookkeeping dicts so a
    # long-lived session doesn't accumulate stale keys after each
    # device deletion. Coordinator stays running; reload would also
    # reset them but HA doesn't force one here.
    coordinator: CrowdergyCoordinator | None = (
        hass.data.get(DOMAIN, {}).get(config_entry.entry_id)
    )
    if coordinator is not None:
        coordinator.forget_device(crowdergy_device_id)

    return True
