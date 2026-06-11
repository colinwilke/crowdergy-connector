"""Headless provisioning (Crowdergy Box, Phase 2).

Eine Crowdergy Box claimt sich beim Backend per Pairing-Code ein
JWT-Paar (`POST /api/v1/box/claim`) und provisioniert damit diese
Integration ohne UI: ihr box-manager ruft den HA-Service
`theothergas.provision_box` auf, der einen Import-Config-Flow startet
(`async_step_import` in config_flow.py).

Dieses Modul ist bewusst HA-frei (reine Validierung/Normalisierung),
damit es ohne HA-Test-Harness unit-testbar bleibt.
"""
from __future__ import annotations

from typing import Any

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_API_URL,
    CONF_CITY,
    CONF_DEVICES,
    CONF_DISTRICT,
    CONF_EMAIL,
    CONF_ENTITY_OUTDOOR_TEMP,
    CONF_REFRESH_TOKEN,
    CONF_REGION,
    CONF_USER_ID,
    DEFAULT_API_URL,
)

REQUIRED_KEYS = (CONF_ACCESS_TOKEN, CONF_REFRESH_TOKEN, CONF_USER_ID)


def validate_provision_data(raw: dict[str, Any]) -> dict[str, Any]:
    """Service-Payload → vollständige Config-Entry-Daten.

    Entry-Shape identisch zum interaktiven Login-Flow (v3.16: leere
    Geräteliste, Grundeinstellungen kommen später aus dem Options-Flow
    bzw. ab Box-Phase 3 vom box-manager). Wirft ValueError mit
    feldgenauer Meldung — Token-Werte tauchen darin nie auf.
    """
    for key in REQUIRED_KEYS:
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"provision_box: missing or empty '{key}'")

    api_url = raw.get(CONF_API_URL) or DEFAULT_API_URL
    if not isinstance(api_url, str) or not api_url.startswith(("https://", "http://")):
        raise ValueError("provision_box: 'api_url' must be an http(s) URL")

    email = raw.get(CONF_EMAIL) or ""
    if not isinstance(email, str):
        raise ValueError("provision_box: 'email' must be a string")

    return {
        CONF_API_URL: api_url.rstrip("/"),
        CONF_EMAIL: email,
        CONF_ACCESS_TOKEN: raw[CONF_ACCESS_TOKEN].strip(),
        CONF_REFRESH_TOKEN: raw[CONF_REFRESH_TOKEN].strip(),
        CONF_USER_ID: raw[CONF_USER_ID].strip(),
        CONF_DEVICES: [],
        CONF_DISTRICT: "",
        CONF_CITY: "",
        CONF_REGION: "",
        CONF_ENTITY_OUTDOOR_TEMP: "",
    }


def entry_title(data: dict[str, Any]) -> str:
    """Box-Entries haben keine Email zwingend — fallback auf User-ID."""
    if data.get(CONF_EMAIL):
        return f"Crowdergy ({data[CONF_EMAIL]})"
    return f"Crowdergy (Box, {data[CONF_USER_ID][:8]})"


def extract_consent_options(raw: dict[str, Any]) -> dict[str, bool]:
    """Consent-Flags aus dem provision_box-Payload → Entry-OPTIONS.

    Der Box-Wizard erfasst Consent VOR dem Pairing (privacy-model.md);
    damit zwischen Entry-Erzeugung und erstem box_set_consent kein
    Fenster mit Default-True entsteht, setzt der Import-Flow die
    Options atomar beim async_create_entry. Fehlen die Felder (ältere
    Box, Re-Pairing), bleibt das Options-Dict leer — bestehende
    Options/Defaults gelten weiter.
    """
    from .const import OPT_CONSENT_REMOTE_CONTROL, OPT_CONSENT_TELEMETRY

    options: dict[str, bool] = {}
    if "consent_telemetry" in raw:
        options[OPT_CONSENT_TELEMETRY] = bool(raw["consent_telemetry"])
    if "consent_remote_control" in raw:
        options[OPT_CONSENT_REMOTE_CONTROL] = bool(raw["consent_remote_control"])
    return options
