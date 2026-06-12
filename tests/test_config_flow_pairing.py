"""Config-Flow Pairing-Code-Onboarding + Reauth (Connector-Arch 2026-06-12).

Der `user`-Step claimt einen Pairing-Code aus der Crowdergy-App
(ersetzt den Email/Passwort-Login komplett); Reauth holt ein frisches
Token-Paar über denselben Weg und lehnt fremde Accounts ab.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.theothergas.api_client import (
    CannotConnect,
    InvalidPairingCode,
)
from custom_components.theothergas.const import (
    CONF_ACCESS_TOKEN,
    CONF_API_URL,
    CONF_DEVICES,
    CONF_EMAIL,
    CONF_PAIRING_CODE,
    CONF_REFRESH_TOKEN,
    CONF_USER_ID,
    DEFAULT_API_URL,
    DOMAIN,
)

TOKENS = {
    "access_token": "access-jwt",
    "refresh_token": "refresh-jwt",
    "user_id": "user-1",
    "token_type": "bearer",
}


def _patches(
    *,
    claim=None,
    email="colin@example.com",
):
    """Claim + E-Mail-Lookup + Instance-ID mocken; Entry-Setup wird
    abgeklemmt (der Coordinator würde sonst echte Requests starten)."""
    claim_mock = (
        AsyncMock(return_value=dict(TOKENS)) if claim is None else claim
    )
    return (
        claim_mock,
        patch(
            "custom_components.theothergas.config_flow.claim_pairing_code",
            new=claim_mock,
        ),
        patch(
            "custom_components.theothergas.config_flow.fetch_account_email",
            new=AsyncMock(return_value=email),
        ),
        patch(
            "custom_components.theothergas.config_flow.instance_id.async_get",
            new=AsyncMock(return_value="ha-instance-1"),
        ),
        patch(
            "custom_components.theothergas.async_setup_entry",
            return_value=True,
        ),
    )


async def test_user_step_happy_path(hass: HomeAssistant):
    claim_mock, *ps = _patches()
    with ps[0], ps[1], ps[2], ps[3]:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_PAIRING_CODE: " abcd-2345 ", CONF_API_URL: DEFAULT_API_URL},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Crowdergy (colin@example.com)"
    entry = result["result"]
    assert entry.unique_id == "user-1"
    assert entry.data[CONF_ACCESS_TOKEN] == "access-jwt"
    assert entry.data[CONF_REFRESH_TOKEN] == "refresh-jwt"
    assert entry.data[CONF_USER_ID] == "user-1"
    assert entry.data[CONF_EMAIL] == "colin@example.com"
    assert entry.data[CONF_API_URL] == DEFAULT_API_URL
    assert entry.data[CONF_DEVICES] == []
    # Whitespace getrimmt, Code unverändert weitergereicht (das
    # Backend normalisiert Bindestriche/Case selbst).
    claim_mock.assert_awaited_once_with(
        hass, DEFAULT_API_URL, "abcd-2345", "ha-instance-1"
    )


async def test_user_step_title_falls_back_to_user_id(hass: HomeAssistant):
    _, *ps = _patches(email=None)
    with ps[0], ps[1], ps[2], ps[3]:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PAIRING_CODE: "ABCD-2345"}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Crowdergy (user-1)"
    assert result["result"].data[CONF_EMAIL] == ""


async def test_user_step_invalid_code_shows_field_error(hass: HomeAssistant):
    _, *ps = _patches(claim=AsyncMock(side_effect=InvalidPairingCode))
    with ps[0], ps[1], ps[2], ps[3]:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PAIRING_CODE: "AAAA-AAAA"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_PAIRING_CODE: "invalid_pairing_code"}


async def test_user_step_cannot_connect(hass: HomeAssistant):
    _, *ps = _patches(claim=AsyncMock(side_effect=CannotConnect("boom")))
    with ps[0], ps[1], ps[2], ps[3]:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PAIRING_CODE: "ABCD-2345"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_step_same_account_aborts(hass: HomeAssistant):
    """unique_id = user_id (P3): Token-Tausch gehört in den Reauth-
    Flow, nicht in einen Duplikat-Entry."""
    MockConfigEntry(
        domain=DOMAIN, unique_id="user-1", data={}
    ).add_to_hass(hass)

    _, *ps = _patches()
    with ps[0], ps[1], ps[2], ps[3]:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PAIRING_CODE: "ABCD-2345"}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


def _existing_entry(hass: HomeAssistant, **overrides) -> MockConfigEntry:
    data = {
        CONF_API_URL: "https://api.example",
        CONF_EMAIL: "colin@example.com",
        CONF_ACCESS_TOKEN: "old-access",
        CONF_REFRESH_TOKEN: "old-refresh",
        CONF_USER_ID: "user-1",
        CONF_DEVICES: [],
        **overrides,
    }
    entry = MockConfigEntry(domain=DOMAIN, unique_id="user-1", data=data)
    entry.add_to_hass(hass)
    return entry


async def test_reauth_replaces_tokens(hass: HomeAssistant):
    entry = _existing_entry(hass)

    claim_mock, *ps = _patches()
    with ps[0], ps[1], ps[2], ps[3]:
        result = await entry.start_reauth_flow(hass)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "reauth_confirm"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PAIRING_CODE: "ABCD-2345"}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_ACCESS_TOKEN] == "access-jwt"
    assert entry.data[CONF_REFRESH_TOKEN] == "refresh-jwt"
    # api_url des Entries wird benutzt, nicht der Default.
    claim_mock.assert_awaited_once_with(
        hass, "https://api.example", "ABCD-2345", "ha-instance-1"
    )


async def test_reauth_rejects_foreign_account(hass: HomeAssistant):
    entry = _existing_entry(hass)

    foreign = {**TOKENS, "user_id": "user-OTHER"}
    _, *ps = _patches(claim=AsyncMock(return_value=foreign))
    with ps[0], ps[1], ps[2], ps[3]:
        result = await entry.start_reauth_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PAIRING_CODE: "ABCD-2345"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {
        CONF_PAIRING_CODE: "reauth_account_mismatch"
    }
    # Fremde Tokens wurden NICHT persistiert.
    assert entry.data[CONF_ACCESS_TOKEN] == "old-access"
    assert entry.data[CONF_USER_ID] == "user-1"


async def test_reauth_invalid_code_shows_field_error(hass: HomeAssistant):
    entry = _existing_entry(hass)

    _, *ps = _patches(claim=AsyncMock(side_effect=InvalidPairingCode))
    with ps[0], ps[1], ps[2], ps[3]:
        result = await entry.start_reauth_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PAIRING_CODE: "AAAA-AAAA"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_PAIRING_CODE: "invalid_pairing_code"}
    assert entry.data[CONF_ACCESS_TOKEN] == "old-access"
