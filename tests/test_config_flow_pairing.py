"""Config-Flow Pairing-Code-Onboarding (#39, 2026-06-16).

Der HACS-Connector pairt per Code (`POST /connector/claim`) statt
Email/Passwort — symmetrisch zur Crowdergy Box. Diese Tests sperren den
`user`-Step (Claim → Entry mit Tokens) + die Fehlerpfade (404 →
Feldfehler), damit der Onboarding-Vertrag nicht wieder driftet.
"""
from __future__ import annotations

from unittest.mock import patch

import httpx
import respx
from homeassistant.core import HomeAssistant

# Entry-Create triggert sonst das echte async_setup_entry (Coordinator →
# GET /devices + SSE) — fürs Flow-Verhalten irrelevant, also weggepatcht.
_NO_SETUP = patch(
    "custom_components.theothergas.async_setup_entry", return_value=True
)

from custom_components.theothergas.const import (
    CONF_ACCESS_TOKEN,
    CONF_PAIRING_CODE,
    CONF_REFRESH_TOKEN,
    CONF_USER_ID,
    DEFAULT_API_URL,
    DOMAIN,
)

CLAIM_URL = f"{DEFAULT_API_URL}/api/v1/connector/claim"
ME_URL = f"{DEFAULT_API_URL}/api/v1/users/me"


@respx.mock
async def test_user_flow_pairing_code_creates_entry(hass: HomeAssistant):
    """Happy path: gültiger Code → Claim liefert Tokens → Entry mit
    Tokens + unique_id; der Best-Effort-/me-Lookup liefert den Titel."""
    claim_route = respx.post(CLAIM_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "acc",
                "refresh_token": "ref",
                "user_id": "u-123",
                "token_type": "bearer",
            },
        )
    )
    respx.get(ME_URL).mock(
        return_value=httpx.Response(200, json={"email": "a@b.de"})
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "user"

    with _NO_SETUP:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PAIRING_CODE: "ABCD-1234"}
        )
        await hass.async_block_till_done()
    assert result["type"] == "create_entry"
    data = result["data"]
    assert data[CONF_ACCESS_TOKEN] == "acc"
    assert data[CONF_REFRESH_TOKEN] == "ref"
    assert data[CONF_USER_ID] == "u-123"
    assert "a@b.de" in result["title"]
    # Es wurde NUR `code` gesendet (Backend-Schema ist extra=forbid).
    import json as _json

    sent = claim_route.calls.last.request
    assert _json.loads(sent.content) == {"code": "ABCD-1234"}


@respx.mock
async def test_user_flow_invalid_code_shows_field_error(hass: HomeAssistant):
    """404 vom Claim (unbekannt/abgelaufen/verbraucht) → Feldfehler am
    Pairing-Code, kein Entry."""
    respx.post(CLAIM_URL).mock(
        return_value=httpx.Response(404, json={"detail": "Unknown"})
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PAIRING_CODE: "BADCODE1"}
    )
    assert result["type"] == "form"
    assert result["errors"] == {CONF_PAIRING_CODE: "invalid_pairing_code"}


@respx.mock
async def test_user_flow_title_falls_back_to_user_id(hass: HomeAssistant):
    """Schlägt der /me-Lookup fehl (kein Email), trägt der Titel die
    User-ID statt zu crashen."""
    respx.post(CLAIM_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "acc",
                "refresh_token": "ref",
                "user_id": "abcdef12-rest",
                "token_type": "bearer",
            },
        )
    )
    respx.get(ME_URL).mock(return_value=httpx.Response(500))

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    with _NO_SETUP:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PAIRING_CODE: "ABCD1234"}
        )
        await hass.async_block_till_done()
    assert result["type"] == "create_entry"
    assert "abcdef12" in result["title"]
    assert result["data"][CONF_ACCESS_TOKEN] == "acc"
