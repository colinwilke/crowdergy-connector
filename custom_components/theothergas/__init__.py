"""The Crowdergy integration."""
from __future__ import annotations

import logging

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
    """Komponenten-Setup. Die Box-Services (provision_box,
    box_list_presets, box_add_device, box_set_consent) werden NUR
    registriert, wenn `theothergas:` in der configuration.yaml steht
    (CN-9, 2026-06-11) — exakt wie in services.yaml/box_services.py
    dokumentiert. Die Crowdergy Box lädt den YAML-Key (verifiziert:
    crowdergy-box/ha-config/configuration.yaml); normale HACS-
    Installationen ohne YAML-Key bekommen die Services nicht mehr
    (gewollt: kein headless Provisioning-Endpoint auf Self-Hosted-
    Instanzen). Config-Entry-Setup läuft unabhängig davon weiter.
    """
    if DOMAIN not in config:
        return True

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

    _prune_legacy_device_cards(hass, entry)

    return True


def _prune_legacy_device_cards(
    hass: HomeAssistant, entry: CrowdergyConfigEntry
) -> None:
    """Remove pre-v3.38.0 per-device registry cards.

    Until v3.38.0 the connector registered one `Crowdergy_<Name>` device
    per mapped device. Since v3.38.0 every entity attaches to the single
    hub device ``(DOMAIN, entry_id)``; the old per-device cards are left
    entity-less (their "Crowdergy AI" switch re-homed to the hub above,
    their mirror sensors were removed in v3.37.0). Detach those leftovers
    from this entry so HA clears the empty cards automatically.

    Without this the user has to delete the stale cards by hand — and a
    card-delete used to trigger a real backend device delete (see
    ``async_remove_config_entry_device``), so tidying up the empty cards
    silently deleted live devices. Pruning here (after the switches have
    re-homed to the hub) is safe: the leftover cards carry no entities.
    """
    dev_reg = dr.async_get(hass)
    hub_identifier = (DOMAIN, entry.entry_id)
    for device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
        if hub_identifier in device.identifiers:
            continue
        dev_reg.async_update_device(
            device.id, remove_config_entry_id=entry.entry_id
        )


async def async_unload_entry(hass: HomeAssistant, entry: CrowdergyConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        # Defensiv: pop mit Defaults, falls async_setup_entry nie lief
        # (z. B. früher Fehlschlag) — ein KeyError beim Unload würde sonst
        # den Entry hängen lassen.
        coordinator: CrowdergyCoordinator | None = hass.data.get(
            DOMAIN, {}
        ).pop(entry.entry_id, None)
        if coordinator is not None:
            await coordinator.async_shutdown()

    return unload_ok


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    config_entry: CrowdergyConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Handle a device-card deletion from the HA UI.

    Since v3.38.0 the integration registers a single "Crowdergy" hub
    device ``(DOMAIN, entry_id)`` that all switches and the connectivity
    sensor attach to — there are no per-device cards for active devices
    anymore. So this hook must NEVER delete a backend device: it only
    decides whether HA may remove a card.

    - **Hub device** (identifier == entry_id): the live integration
      device. Refuse deletion (return False) so a stray card-delete can't
      orphan every switch; the whole integration is removed via its entry.
    - **Any other ``(DOMAIN, …)`` card**: a pre-v3.38.0 per-device
      leftover (now entity-less). Allow HA to clear the empty card, but
      leave the backend untouched — the real device still lives under the
      hub. Removing a device is done explicitly via Configure → Remove
      device (options flow), which deletes it on the backend.

    Historic behaviour (delete the backend device + reload on any card
    deletion) was destructive under the hub model: deleting an empty
    leftover card silently deleted the live device it used to represent.
    """
    for domain, identifier in device_entry.identifiers:
        if domain == DOMAIN and identifier == config_entry.entry_id:
            # The live hub device — don't let a card-delete remove it out
            # from under its entities.
            return False

    # A stale per-device leftover card: let HA clear the empty card, no
    # backend delete (device removal lives in the options flow).
    return True
