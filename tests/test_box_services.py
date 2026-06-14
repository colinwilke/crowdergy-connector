"""Box-Services (Phase 3 / Mapping-Umbau): box_list_presets + box_add_device."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.theothergas.box_services import (
    SERVICE_BOX_ADD_DEVICE,
    SERVICE_BOX_LIST_PRESETS,
)
from custom_components.theothergas.const import (
    CONF_ACCESS_TOKEN,
    CONF_API_URL,
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    CONF_DEVICE_TYPE,
    CONF_DEVICES,
    CONF_EMAIL,
    CONF_ENTITY_POWER,
    CONF_REFRESH_TOKEN,
    CONF_USER_ID,
    DOMAIN,
)

ENTRY_DATA = {
    CONF_API_URL: "https://api.example",
    CONF_EMAIL: "",
    CONF_ACCESS_TOKEN: "access-jwt",
    CONF_REFRESH_TOKEN: "refresh-jwt",
    CONF_USER_ID: "user-1",
    CONF_DEVICES: [],
}

PRESET = {
    "device_type": "solar",
    "vendor": "KOSTAL",
    "model": "Plenticore plus 8.5",
    "entity_map": {CONF_ENTITY_POWER: "sensor.plenticore_total_dc_power"},
    "helper_yaml": None,
    "contribution_count": 3,
    "integration_domain": "kostal_plenticore",
}


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: {}})
    await hass.async_block_till_done()
    entry = MockConfigEntry(domain=DOMAIN, unique_id="user-1", data=dict(ENTRY_DATA))
    entry.add_to_hass(hass)
    return entry


def _response(status: int, payload: dict) -> httpx.Response:
    return httpx.Response(
        status,
        json=payload,
        request=httpx.Request("GET", "https://api.example/x"),
    )


async def test_list_presets_proxies_lookup(hass: HomeAssistant):
    await _setup(hass)

    async def fake_request(hass_, entry_, method, path, **kwargs):
        assert (method, path) == ("GET", "/api/v1/crowd-presets/lookup")
        assert kwargs["params"] == {CONF_DEVICE_TYPE: "solar", "vendor": "KOSTAL"}
        return _response(200, {"presets": [PRESET]})

    with patch(
        "custom_components.theothergas.api_client.authenticated_request",
        new=AsyncMock(side_effect=fake_request),
    ):
        response = await hass.services.async_call(
            DOMAIN,
            SERVICE_BOX_LIST_PRESETS,
            {CONF_DEVICE_TYPE: "solar", "vendor": "KOSTAL"},
            blocking=True,
            return_response=True,
        )

    assert response["presets"][0]["integration_domain"] == "kostal_plenticore"
    assert response["presets"][0]["entity_map"][CONF_ENTITY_POWER]


async def test_list_presets_backend_error(hass: HomeAssistant):
    await _setup(hass)
    with patch(
        "custom_components.theothergas.api_client.authenticated_request",
        new=AsyncMock(return_value=_response(500, {})),
    ):
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                DOMAIN,
                SERVICE_BOX_LIST_PRESETS,
                {CONF_DEVICE_TYPE: "solar"},
                blocking=True,
                return_response=True,
            )


async def test_list_presets_without_entry_raises(hass: HomeAssistant):
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: {}})
    await hass.async_block_till_done()
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_BOX_LIST_PRESETS,
            {CONF_DEVICE_TYPE: "solar"},
            blocking=True,
            return_response=True,
        )


async def test_add_device_registers_and_persists(hass: HomeAssistant):
    entry = await _setup(hass)

    async def fake_request(hass_, entry_, method, path, **kwargs):
        assert (method, path) == ("POST", "/api/v1/devices")
        assert kwargs["json"]["name"] == "Plenticore Solar"
        assert kwargs["json"]["type"] == "solar"
        return _response(200, {"id": "backend-dev-1"})

    with patch(
        "custom_components.theothergas.api_client.authenticated_request",
        new=AsyncMock(side_effect=fake_request),
    ):
        response = await hass.services.async_call(
            DOMAIN,
            SERVICE_BOX_ADD_DEVICE,
            {
                CONF_DEVICE_TYPE: "solar",
                CONF_DEVICE_NAME: "Plenticore Solar",
                "entities": {CONF_ENTITY_POWER: "sensor.plenticore_total_dc_power"},
            },
            blocking=True,
            return_response=True,
        )

    assert response[CONF_DEVICE_ID] == "backend-dev-1"
    devices = entry.data[CONF_DEVICES]
    assert len(devices) == 1
    assert devices[0][CONF_DEVICE_ID] == "backend-dev-1"
    assert devices[0][CONF_ENTITY_POWER] == "sensor.plenticore_total_dc_power"


async def test_add_device_backend_error_keeps_entry_unchanged(hass: HomeAssistant):
    entry = await _setup(hass)
    with patch(
        "custom_components.theothergas.api_client.authenticated_request",
        new=AsyncMock(return_value=_response(500, {})),
    ):
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                DOMAIN,
                SERVICE_BOX_ADD_DEVICE,
                {
                    CONF_DEVICE_TYPE: "solar",
                    CONF_DEVICE_NAME: "X",
                    "entities": {},
                },
                blocking=True,
                return_response=True,
            )
    assert entry.data[CONF_DEVICES] == []


async def test_add_device_rejects_unknown_type(hass: HomeAssistant):
    await _setup(hass)
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_BOX_ADD_DEVICE,
            {CONF_DEVICE_TYPE: "toaster", CONF_DEVICE_NAME: "X", "entities": {}},
            blocking=True,
        )


# ── Phase 4 / E-6 (CN-10): Slot-Allowlist (Default-DENY) ─────────────────────


async def test_add_device_rejects_forbidden_domain(hass: HomeAssistant):
    """Allowlist (E-6): camera auf einem Control-Slot ist nicht in der
    erlaubten Domain-Menge → Ablehnung, Entry unverändert."""
    entry = await _setup(hass)
    with pytest.raises(HomeAssistantError) as excinfo:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_BOX_ADD_DEVICE,
            {
                CONF_DEVICE_TYPE: "generic",
                CONF_DEVICE_NAME: "Spion",
                "entities": {"entity_control": "camera.wohnzimmer"},
            },
            blocking=True,
            return_response=True,
        )
    assert "not allowed" in str(excinfo.value)
    assert entry.data[CONF_DEVICES] == []


async def test_add_device_rejects_lock_on_read_slot(hass: HomeAssistant):
    """Allowlist (E-6): ein lock.* auf einem Read-Slot (entity_current_
    power_kw, nur `sensor` erlaubt) wird abgewiesen."""
    entry = await _setup(hass)
    with pytest.raises(HomeAssistantError) as excinfo:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_BOX_ADD_DEVICE,
            {
                CONF_DEVICE_TYPE: "solar",
                CONF_DEVICE_NAME: "X",
                "entities": {CONF_ENTITY_POWER: "lock.haustuer"},
            },
            blocking=True,
            return_response=True,
        )
    assert "not allowed" in str(excinfo.value)
    assert entry.data[CONF_DEVICES] == []


async def test_add_device_rejects_alarm_panel_on_control_slot(hass: HomeAssistant):
    """Allowlist (E-6): alarm_control_panel auf entity_control →
    abgelehnt (nicht in den schaltbaren Domains)."""
    entry = await _setup(hass)
    with pytest.raises(HomeAssistantError) as excinfo:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_BOX_ADD_DEVICE,
            {
                CONF_DEVICE_TYPE: "generic",
                CONF_DEVICE_NAME: "X",
                "entities": {"entity_control": "alarm_control_panel.haus"},
            },
            blocking=True,
            return_response=True,
        )
    assert "not allowed" in str(excinfo.value)
    assert entry.data[CONF_DEVICES] == []


async def test_add_device_rejects_unknown_slot(hass: HomeAssistant):
    """Allowlist (E-6): ein Slot, der gar kein bekannter Mapping-Slot
    ist, aber wie eine entity_id aussieht, wird Default-DENY abgelehnt."""
    entry = await _setup(hass)
    with pytest.raises(HomeAssistantError) as excinfo:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_BOX_ADD_DEVICE,
            {
                CONF_DEVICE_TYPE: "generic",
                CONF_DEVICE_NAME: "X",
                "entities": {"entity_spy": "sensor.harmlos"},
            },
            blocking=True,
            return_response=True,
        )
    assert "not a mappable entity or preset value slot" in str(excinfo.value)
    assert entry.data[CONF_DEVICES] == []


async def test_add_device_rejects_unknown_slot_without_dot(hass: HomeAssistant):
    """v3.24 (Mapping-Dictionary): unbekannte Slot-Keys werden jetzt
    auch dann abgelehnt, wenn ihr Wert KEIN Punkt-Format hat — vorher
    rutschten sie still durch und wurden erst später gedroppt."""
    entry = await _setup(hass)
    with pytest.raises(HomeAssistantError) as excinfo:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_BOX_ADD_DEVICE,
            {
                CONF_DEVICE_TYPE: "battery",
                CONF_DEVICE_NAME: "X",
                "entities": {"mystery_value": "whatever"},
            },
            blocking=True,
            return_response=True,
        )
    assert "not a mappable entity or preset value slot" in str(excinfo.value)
    assert entry.data[CONF_DEVICES] == []


async def test_add_device_rejects_non_entity_value_on_entity_slot(
    hass: HomeAssistant,
):
    """v3.24: ein Entity-Slot mit einem Wert ohne `<domain>.<object_id>`-
    Form ist ein kaputtes Preset — Ablehnung statt stiller Übernahme
    (vorher übersprang der Punkt-Check solche Werte komplett)."""
    entry = await _setup(hass)
    with pytest.raises(HomeAssistantError) as excinfo:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_BOX_ADD_DEVICE,
            {
                CONF_DEVICE_TYPE: "solar",
                CONF_DEVICE_NAME: "X",
                "entities": {CONF_ENTITY_POWER: "kaputt_ohne_domain"},
            },
            blocking=True,
            return_response=True,
        )
    assert "not allowed" in str(excinfo.value)
    assert entry.data[CONF_DEVICES] == []


async def test_add_device_allows_battery_preset_with_value_map(
    hass: HomeAssistant,
):
    """v3.24 (Mapping-Dictionary): volles Batterie-Preset — Entity-Slots
    mit Domain-Check, Wert-/Flag-Slots verbatim, auch mit Punkt im Wert
    (Select-Optionen wie '7.4 kW Mode' liefen vorher fälschlich in den
    Entity-Check)."""
    entry = await _setup(hass)

    async def fake_request(hass_, entry_, method, path, **kwargs):
        return _response(200, {"id": "backend-bat-1"})

    with patch(
        "custom_components.theothergas.api_client.authenticated_request",
        new=AsyncMock(side_effect=fake_request),
    ):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_BOX_ADD_DEVICE,
            {
                CONF_DEVICE_TYPE: "battery",
                CONF_DEVICE_NAME: "Speicher",
                "entities": {
                    CONF_ENTITY_POWER: "sensor.batterie_power",
                    "entity_soc_percent": "sensor.batterie_soc",
                    "entity_battery_mode": "select.batterie_modus",
                    "entity_battery_power_setpoint_w": "number.batterie_setpoint",
                    "value_battery_mode_active": "Mode 1.0 (extern)",
                    "value_battery_mode_passive": "Internal",
                    "battery_setpoint_invert_sign": "true",
                    "entity_control_hold": "always",
                },
            },
            blocking=True,
            return_response=True,
        )

    dev = entry.data[CONF_DEVICES][0]
    assert dev[CONF_DEVICE_ID] == "backend-bat-1"
    assert dev["entity_battery_mode"] == "select.batterie_modus"
    assert dev["value_battery_mode_active"] == "Mode 1.0 (extern)"
    # Flag kam als String "true" an und ist im Record truthy gespeichert
    assert dev["battery_setpoint_invert_sign"]
    assert dev["entity_control_hold"] == "always"


async def test_add_device_allows_select_on_charge_mode(hass: HomeAssistant):
    """Allowlist (E-6): Control-Slot mit erlaubter Domain (select auf
    entity_charge_mode) geht durch — Gegentest dass die Allowlist die
    echten Presets nicht aussperrt."""
    entry = await _setup(hass)

    async def fake_request(hass_, entry_, method, path, **kwargs):
        return _response(200, {"id": "backend-wb-1"})

    with patch(
        "custom_components.theothergas.api_client.authenticated_request",
        new=AsyncMock(side_effect=fake_request),
    ):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_BOX_ADD_DEVICE,
            {
                CONF_DEVICE_TYPE: "wallbox",
                CONF_DEVICE_NAME: "Wallbox",
                "entities": {
                    CONF_ENTITY_POWER: "sensor.wallbox_power",
                    "entity_charge_mode": "select.lademodus",
                    "entity_control": "switch.wallbox_enable",
                },
            },
            blocking=True,
            return_response=True,
        )
    assert entry.data[CONF_DEVICES][0][CONF_DEVICE_ID] == "backend-wb-1"


async def test_add_device_allows_kostal_solar_preset(hass: HomeAssistant):
    """Allowlist (E-6) KOSTAL-Regression: das live-verifizierte
    Plenticore-Solar-Mapping (sensor.* auf Power + Energy) bleibt
    erlaubt."""
    entry = await _setup(hass)

    async def fake_request(hass_, entry_, method, path, **kwargs):
        return _response(200, {"id": "backend-pv-1"})

    from custom_components.theothergas.const import CONF_ENTITY_ENERGY_TOTAL

    with patch(
        "custom_components.theothergas.api_client.authenticated_request",
        new=AsyncMock(side_effect=fake_request),
    ):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_BOX_ADD_DEVICE,
            {
                CONF_DEVICE_TYPE: "solar",
                CONF_DEVICE_NAME: "Plenticore Solar",
                "entities": {
                    CONF_ENTITY_POWER: "sensor.sum_power_of_all_pv_dc_inputs",
                    CONF_ENTITY_ENERGY_TOTAL: "sensor.plenticore_yield_total",
                },
            },
            blocking=True,
            return_response=True,
        )
    assert entry.data[CONF_DEVICES][0][CONF_DEVICE_ID] == "backend-pv-1"


# ── CN-9: YAML-Gate + eindeutiger Entry ──────────────────────────────────────


async def test_services_not_registered_without_yaml_key(hass: HomeAssistant):
    """CN-9 (2026-06-11): ohne `theothergas:` in configuration.yaml
    werden provision_box + Box-Services NICHT registriert (wie
    dokumentiert); async_setup liefert weiterhin True, damit Config-
    Entries normal laden."""
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    assert not hass.services.has_service(DOMAIN, "provision_box")
    assert not hass.services.has_service(DOMAIN, SERVICE_BOX_LIST_PRESETS)
    assert not hass.services.has_service(DOMAIN, SERVICE_BOX_ADD_DEVICE)
    assert not hass.services.has_service(DOMAIN, "box_set_consent")


async def test_get_entry_rejects_multiple_entries(hass: HomeAssistant):
    """CN-9 (2026-06-11): `_get_entry` darf bei >1 Entry nicht blind
    entries[0] raten — Fehler statt falscher Account."""
    await _setup(hass)
    second = MockConfigEntry(
        domain=DOMAIN, unique_id="user-2", data=dict(ENTRY_DATA)
    )
    second.add_to_hass(hass)
    with pytest.raises(HomeAssistantError, match="multiple"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_BOX_LIST_PRESETS,
            {CONF_DEVICE_TYPE: "solar"},
            blocking=True,
            return_response=True,
        )


# ── CN-14: defensive Server-Antwort-Validierung ──────────────────────────────


async def test_list_presets_drops_malformed_presets(hass: HomeAssistant):
    """CN-14 (2026-06-11): Presets ohne vendor/model (oder Nicht-Dicts)
    werden gedroppt statt downstream den Picker zu crashen."""
    await _setup(hass)
    payload = {
        "presets": [PRESET, {"vendor": "NurVendor"}, "junk", 42],
    }
    with patch(
        "custom_components.theothergas.api_client.authenticated_request",
        new=AsyncMock(return_value=_response(200, payload)),
    ):
        response = await hass.services.async_call(
            DOMAIN,
            SERVICE_BOX_LIST_PRESETS,
            {CONF_DEVICE_TYPE: "solar"},
            blocking=True,
            return_response=True,
        )
    assert response["presets"] == [PRESET]


async def test_add_device_missing_id_raises_clean_error(hass: HomeAssistant):
    """CN-14 (2026-06-11): Registrierungs-Antwort ohne `id` →
    sauberer HomeAssistantError statt KeyError; Entry unverändert."""
    entry = await _setup(hass)
    with patch(
        "custom_components.theothergas.api_client.authenticated_request",
        new=AsyncMock(return_value=_response(200, {"ok": True})),
    ):
        with pytest.raises(HomeAssistantError, match="missing 'id'"):
            await hass.services.async_call(
                DOMAIN,
                SERVICE_BOX_ADD_DEVICE,
                {
                    CONF_DEVICE_TYPE: "solar",
                    CONF_DEVICE_NAME: "X",
                    "entities": {},
                },
                blocking=True,
                return_response=True,
            )
    assert entry.data[CONF_DEVICES] == []


async def test_set_consent_writes_entry_options(hass: HomeAssistant):
    from custom_components.theothergas.box_services import SERVICE_BOX_SET_CONSENT
    from custom_components.theothergas.const import (
        OPT_CONSENT_REMOTE_CONTROL,
        OPT_CONSENT_TELEMETRY,
    )

    entry = await _setup(hass)
    await hass.services.async_call(
        DOMAIN,
        SERVICE_BOX_SET_CONSENT,
        {"telemetry": False, "remote_control": True},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert entry.options[OPT_CONSENT_TELEMETRY] is False
    assert entry.options[OPT_CONSENT_REMOTE_CONTROL] is True
