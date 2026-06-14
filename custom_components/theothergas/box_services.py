"""Box-Services (Crowdergy Box, Phase 3 / Mapping-Umbau 2026-06-10).

Zwei Services für den box-manager der Crowdergy-Appliance:

* `box_list_presets` (response-only): liefert die approved Vendor-
  Presets aus dem Backend (`GET /api/v1/crowd-presets/lookup`) als
  Service-Response — inklusive `integration_domain`, sodass die Box
  nur Presets anbietet, deren Integration sie headless provisionieren
  kann. Die Presets stammen aus User-Beiträgen („share setup" im
  Options-Flow) und sind durch den Promotion-Threshold kuratiert.
  Designentscheidung: die Box bekommt KEINE freie Discovery und KEINEN
  LLM-Mapper — der bleibt Self-Hosted-HA-Usern vorbehalten, wo ein
  Mensch die Vorschläge bestätigt. Auf der Appliance zählt
  Determinismus.
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
    CONF_CITY,
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    CONF_DEVICE_TYPE,
    CONF_DEVICES,
    CONF_DISTRICT,
    CONF_REGION,
    DEVICE_TYPES,
    DOMAIN,
    MAPPABLE_ENTITY_DOMAINS,
    OPT_CONSENT_REMOTE_CONTROL,
    OPT_CONSENT_TELEMETRY,
)
from .device_field_spec import build_payload
from .preset_spec import PRESET_VALUE_SLOTS

_LOGGER = logging.getLogger(__name__)

SERVICE_BOX_LIST_PRESETS = "box_list_presets"
SERVICE_BOX_ADD_DEVICE = "box_add_device"
SERVICE_BOX_SET_CONSENT = "box_set_consent"

BOX_SET_CONSENT_SCHEMA = vol.Schema(
    {
        vol.Required("telemetry"): cv.boolean,
        vol.Required("remote_control"): cv.boolean,
    }
)

BOX_LIST_PRESETS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_DEVICE_TYPE): vol.In(DEVICE_TYPES),
        vol.Optional("vendor"): cv.string,
    }
)

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
    # CN-9 (2026-06-11): vorher blind entries[0] — bei mehreren
    # Entries (sollte auf einer Box nie passieren) wäre das ein
    # stilles Raten auf den falschen Account. Eindeutigkeit erzwingen.
    if len(entries) > 1:
        raise HomeAssistantError(
            "multiple Crowdergy config entries found — box services "
            "need exactly one (remove the stale entry first)"
        )
    return entries[0]


def async_register_box_services(hass: HomeAssistant) -> None:
    async def _handle_list_presets(call: ServiceCall) -> ServiceResponse:
        from .api_client import authenticated_request

        entry = _get_entry(hass)
        params: dict[str, str] = {CONF_DEVICE_TYPE: call.data[CONF_DEVICE_TYPE]}
        if call.data.get("vendor"):
            params["vendor"] = call.data["vendor"]
        response = await authenticated_request(
            hass, entry, "GET", "/api/v1/crowd-presets/lookup", params=params
        )
        if response.status_code >= 400:
            raise HomeAssistantError(
                f"preset lookup failed: {response.status_code}"
            )
        # CN-14 (2026-06-11): Server-Antwort defensiv parsen — kaputtes
        # JSON / unerwartete Shapes dürfen den Service nicht mit einem
        # rohen Traceback crashen, und Presets ohne vendor/model würden
        # downstream (GUI-Picker, config_flow-Schema) hart indexiert.
        try:
            payload = response.json()
        except ValueError as err:
            raise HomeAssistantError(
                f"preset lookup returned invalid JSON: {err}"
            ) from err
        presets = payload.get("presets") if isinstance(payload, dict) else None
        if not isinstance(presets, list):
            presets = []
        valid = []
        for p in presets:
            if isinstance(p, dict) and {"vendor", "model"} <= p.keys():
                valid.append(p)
            else:
                _LOGGER.warning(
                    "box_list_presets: dropping malformed preset "
                    "(keys: %s)",
                    sorted(p.keys()) if isinstance(p, dict) else type(p).__name__,
                )
        return {"presets": valid}

    async def _handle_add_device(call: ServiceCall) -> ServiceResponse:
        # Import hier statt Modulkopf: config_flow ist groß und wird
        # sonst bei jedem HA-Boot mitgeladen, obwohl die Box-Services
        # auf normalen Installationen nie aufgerufen werden.
        from .api_client import authenticated_request
        from .config_flow import _build_device_record

        entry = _get_entry(hass)
        device_type: str = call.data[CONF_DEVICE_TYPE]
        device_name: str = call.data[CONF_DEVICE_NAME]
        entity_input: dict[str, Any] = dict(call.data["entities"])

        # Security-Model #5 (Box), E-6 / CN-10 (2026-06-11): Allowlist
        # statt Blocklist. Default-DENY — pro Mapping-Slot ist nur eine
        # explizite Menge HA-Domains zugelassen (`MAPPABLE_ENTITY_
        # DOMAINS` in const.py). Damit kann keine Kamera-/Personen-/
        # Tracker-/Media-Entity (oder irgendeine andere unerwartete
        # Domain) in den Telemetrie- oder Steuer-Pfad gelangen.
        #
        # v3.24 (Mapping-Dictionary): die Slot-ART entscheidet die
        # Prüfung, nicht mehr „enthält der Wert einen Punkt". Value-/
        # Flag-Slots aus dem Preset (`PRESET_VALUE_SLOTS`) dürfen
        # beliebige Strings tragen — auch mit Punkt (Select-Optionen
        # wie "7.4 kW Mode" liefen vorher in den Entity-Check und
        # wurden fälschlich abgelehnt). Unbekannte Slot-Keys bleiben
        # Default-DENY, jetzt auch ohne Punkt im Wert.
        for slot, value in entity_input.items():
            if value in ("", None):
                continue
            allowed = MAPPABLE_ENTITY_DOMAINS.get(slot)
            if allowed is not None:
                entity_id = str(value)
                domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
                if domain not in allowed:
                    raise HomeAssistantError(
                        f"entity domain '{domain or entity_id}' not allowed "
                        f"in slot '{slot}' "
                        f"(allowed: {', '.join(sorted(allowed))})"
                    )
                continue
            if slot not in PRESET_VALUE_SLOTS:
                raise HomeAssistantError(
                    f"slot '{slot}' is not a mappable entity or preset "
                    f"value slot (value '{value}' rejected)"
                )

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
        response = await authenticated_request(
            hass, entry, "POST", "/api/v1/devices", json=payload
        )
        if response.status_code >= 400:
            raise HomeAssistantError(
                f"backend device registration failed: {response.status_code}"
            )

        # CN-14 (2026-06-11): json() + `id` defensiv — Antwort ohne id
        # hieße ein Device, das backend-seitig existiert, lokal aber
        # nie ankommt; sauberer Fehler statt KeyError-Traceback.
        try:
            body = response.json()
        except ValueError as err:
            raise HomeAssistantError(
                f"backend device registration returned invalid JSON: {err}"
            ) from err
        backend_id = body.get("id") if isinstance(body, dict) else None
        if not backend_id:
            _LOGGER.warning(
                "box_add_device: backend response missing 'id' (keys: %s)",
                sorted(body.keys()) if isinstance(body, dict) else type(body).__name__,
            )
            raise HomeAssistantError(
                "backend device registration response missing 'id'"
            )
        dev = _build_device_record(
            backend_id, device_type, device_name, entity_input
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

    async def _handle_set_consent(call: ServiceCall) -> None:
        """Phase 4: Enforcement-Flags in die Entry-Options schreiben.
        Der Coordinator gated damit Telemetrie-Push (`_async_update_data`)
        und Remote-Steuerung (`_handle_ws_message`); Reload übernimmt
        die neuen Options sofort."""
        entry = _get_entry(hass)
        options = {
            **entry.options,
            OPT_CONSENT_TELEMETRY: bool(call.data["telemetry"]),
            OPT_CONSENT_REMOTE_CONTROL: bool(call.data["remote_control"]),
        }
        hass.config_entries.async_update_entry(entry, options=options)
        await hass.config_entries.async_reload(entry.entry_id)
        _LOGGER.info(
            "box_set_consent: telemetry=%s remote_control=%s",
            options[OPT_CONSENT_TELEMETRY],
            options[OPT_CONSENT_REMOTE_CONTROL],
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_BOX_LIST_PRESETS,
        _handle_list_presets,
        schema=BOX_LIST_PRESETS_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_BOX_SET_CONSENT,
        _handle_set_consent,
        schema=BOX_SET_CONSENT_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_BOX_ADD_DEVICE,
        _handle_add_device,
        schema=BOX_ADD_DEVICE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
