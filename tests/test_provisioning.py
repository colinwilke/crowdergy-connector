"""Box-Provisioning (Phase 2): Validator, Import-Flow und Service.

Der Validator ist HA-frei und läuft pur; Import-Flow + Service nutzen
die pytest-homeassistant-custom-component-Harness aus conftest.py.
"""
from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.theothergas import SERVICE_PROVISION_BOX
from custom_components.theothergas.const import (
    CONF_ACCESS_TOKEN,
    CONF_API_URL,
    CONF_DEVICES,
    CONF_EMAIL,
    CONF_REFRESH_TOKEN,
    CONF_USER_ID,
    DEFAULT_API_URL,
    DOMAIN,
)
from custom_components.theothergas.provisioning import (
    entry_title,
    validate_provision_data,
)

CLAIM = {
    CONF_ACCESS_TOKEN: "access-jwt",
    CONF_REFRESH_TOKEN: "refresh-jwt",
    CONF_USER_ID: "11111111-2222-3333-4444-555555555555",
}


# ── Validator (pur, ohne HA) ─────────────────────────────────────────────────


def test_validate_minimal_claim_payload():
    data = validate_provision_data(dict(CLAIM))
    assert data[CONF_API_URL] == DEFAULT_API_URL
    assert data[CONF_ACCESS_TOKEN] == "access-jwt"
    assert data[CONF_DEVICES] == []
    assert data[CONF_EMAIL] == ""


def test_validate_strips_trailing_slash_and_whitespace():
    data = validate_provision_data(
        {**CLAIM, CONF_API_URL: "https://staging.example/", CONF_ACCESS_TOKEN: " access-jwt "}
    )
    assert data[CONF_API_URL] == "https://staging.example"
    assert data[CONF_ACCESS_TOKEN] == "access-jwt"


@pytest.mark.parametrize("missing", [CONF_ACCESS_TOKEN, CONF_REFRESH_TOKEN, CONF_USER_ID])
def test_validate_rejects_missing_required(missing):
    payload = {k: v for k, v in CLAIM.items() if k != missing}
    with pytest.raises(ValueError) as excinfo:
        validate_provision_data(payload)
    # feldgenaue Meldung, aber nie Token-Werte
    assert missing in str(excinfo.value)
    assert "access-jwt" not in str(excinfo.value)


def test_validate_rejects_non_http_api_url():
    with pytest.raises(ValueError):
        validate_provision_data({**CLAIM, CONF_API_URL: "ftp://nope"})


def test_entry_title_prefers_email_falls_back_to_user_id():
    assert entry_title({CONF_EMAIL: "a@b.de", CONF_USER_ID: "x"}) == "Crowdergy (a@b.de)"
    assert entry_title({CONF_EMAIL: "", CONF_USER_ID: CLAIM[CONF_USER_ID]}) == (
        "Crowdergy (Box, 11111111)"
    )


# ── Import-Flow + Service (HA-Harness) ───────────────────────────────────────


async def test_import_flow_creates_entry(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "import"}, data=dict(CLAIM)
    )
    assert result["type"] == "create_entry"
    assert result["title"] == "Crowdergy (Box, 11111111)"
    assert result["data"][CONF_ACCESS_TOKEN] == "access-jwt"
    assert result["data"][CONF_DEVICES] == []


async def test_import_flow_repairing_updates_tokens(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=CLAIM[CONF_USER_ID],
        data={
            **validate_provision_data(dict(CLAIM)),
            CONF_ACCESS_TOKEN: "old-access",
            CONF_REFRESH_TOKEN: "old-refresh",
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "import"}, data=dict(CLAIM)
    )
    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_ACCESS_TOKEN] == "access-jwt"
    assert entry.data[CONF_REFRESH_TOKEN] == "refresh-jwt"


async def test_import_flow_invalid_data_aborts(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "import"}, data={CONF_ACCESS_TOKEN: "x"}
    )
    assert result["type"] == "abort"
    assert result["reason"] == "invalid_provision_data"


async def test_provision_box_service_creates_entry(hass: HomeAssistant):
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: {}})
    await hass.async_block_till_done()
    assert hass.services.has_service(DOMAIN, SERVICE_PROVISION_BOX)

    await hass.services.async_call(
        DOMAIN, SERVICE_PROVISION_BOX, dict(CLAIM), blocking=True
    )
    await hass.async_block_till_done()

    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    assert entries[0].unique_id == CLAIM[CONF_USER_ID]
    assert entries[0].data[CONF_ACCESS_TOKEN] == "access-jwt"


async def test_provision_box_service_surfaces_invalid_data(hass: HomeAssistant):
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: {}})
    await hass.async_block_till_done()

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_PROVISION_BOX,
            {**CLAIM, CONF_API_URL: "ftp://nope"},
            blocking=True,
        )
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_import_flow_sets_consent_options_atomically(hass: HomeAssistant):
    from custom_components.theothergas.const import (
        OPT_CONSENT_REMOTE_CONTROL,
        OPT_CONSENT_TELEMETRY,
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "import"},
        data={**CLAIM, "consent_telemetry": True, "consent_remote_control": False},
    )
    assert result["type"] == "create_entry"
    assert result["options"][OPT_CONSENT_TELEMETRY] is True
    assert result["options"][OPT_CONSENT_REMOTE_CONTROL] is False
    # Consent-Flags gehören in die Options, nie in die Entry-Daten
    assert "consent_telemetry" not in result["data"]


async def test_import_flow_without_consent_flags_keeps_options_empty(
    hass: HomeAssistant,
):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "import"}, data=dict(CLAIM)
    )
    assert result["type"] == "create_entry"
    assert result["options"] == {}
