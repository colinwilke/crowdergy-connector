"""Crowd-Contribute v0.2 (Mapping-Store 2026-06-11): Options-Flow-Steps.

Treibt die Steps als Unit (Flow-Objekt direkt, ohne Flow-Manager) —
geprüft wird die Spec-Anbindung: Kandidaten-Filter über die
preset-fähigen Typen, Vollständigkeits-Gate, entity_map/value_map-
Payload und die Preset-Value-Defaults im Add-Pfad.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.theothergas import config_flow
from custom_components.theothergas.const import (
    CONF_DEVICE_ID,
    CONF_DEVICES,
    DOMAIN,
)

SOLAR_DEV = {
    "device_id": "dev-solar",
    "device_name": "PV Dach",
    "device_type": "solar",
    "entity_current_power_kw": "sensor.pv_sum_power_of_all_pv_dc_inputs",
    "entity_energy_total": "sensor.pv_energy_yield_total",
}

BATTERY_DEV = {
    "device_id": "dev-bat",
    "device_name": "Speicher",
    "device_type": "battery",
    "entity_current_power_kw": "sensor.anlage_battery_power",
    "entity_soc_percent": "sensor.anlage_battery_soc",
    "entity_battery_mode": "select.anlage_battery_mode",
    "value_battery_mode_active": "External",
    "value_battery_mode_passive": "Internal",
    "entity_battery_power_setpoint_w": "number.anlage_battery_setpoint",
    "battery_setpoint_invert_sign": True,
    "entity_control_hold": "always",
}

BATTERY_INCOMPLETE = {
    "device_id": "dev-bat-2",
    "device_name": "Speicher ohne Steuerung",
    "device_type": "battery",
    "entity_current_power_kw": "sensor.zweite_battery_power",
    "entity_soc_percent": "sensor.zweite_battery_soc",
}

HEATING_DEV = {
    "device_id": "dev-heat",
    "device_name": "WP",
    "device_type": "heating",
    "entity_current_power_kw": "sensor.wp_power",
    "entity_control": "climate.wp",
}

# generic ist (wie haushalt) bewusst NICHT preset-fähig — Catch-all ohne
# Slot-Spec. Dient hier als „nicht beitragbarer" Typ.
GENERIC_DEV = {
    "device_id": "dev-generic",
    "device_name": "Sonstiges",
    "device_type": "generic",
    "entity_current_power_kw": "sensor.misc_power",
}


def _make_flow(hass: HomeAssistant, devices: list[dict]):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="user-1",
        data={CONF_DEVICES: devices},
    )
    entry.add_to_hass(hass)
    flow = config_flow.CrowdergyOptionsFlow(entry)
    flow.hass = hass
    # Flow-Manager-Attribute, die async_show_form/async_abort brauchen
    flow.flow_id = "test-flow"
    flow.handler = entry.entry_id
    return flow


def _response(status: int, payload: dict) -> httpx.Response:
    return httpx.Response(
        status,
        json=payload,
        request=httpx.Request("POST", "https://api.example/x"),
    )


async def test_contribute_offers_all_preset_capable_types(hass: HomeAssistant):
    """Solar + Batterie + (seit #68) heating sind preset-fähig und tauchen
    im Picker auf; generic (Catch-all ohne Spec) bleibt draußen."""
    flow = _make_flow(hass, [SOLAR_DEV, BATTERY_DEV, HEATING_DEV, GENERIC_DEV])
    result = await flow.async_step_contribute_preset()
    assert result["type"] == "form"
    schema = result["data_schema"].schema
    selector_cfg = next(iter(schema.values())).config
    values = [o["value"] for o in selector_cfg["options"]]
    assert values == ["dev-solar", "dev-bat", "dev-heat"]


async def test_contribute_without_capable_devices_aborts(hass: HomeAssistant):
    # Nur ein generic-Gerät → kein preset-fähiger Typ → Abbruch.
    flow = _make_flow(hass, [GENERIC_DEV])
    result = await flow.async_step_contribute_preset()
    assert result["type"] == "abort"
    assert result["reason"] == "contribute_no_devices"


async def test_contribute_incomplete_battery_names_missing_slots(
    hass: HomeAssistant,
):
    """Vollständigkeits-Gate: Batterie ohne Dispatch-Steuerung wird vor
    dem Vendor/Model-Formular abgewiesen, mit deutscher Fehlliste."""
    flow = _make_flow(hass, [BATTERY_INCOMPLETE])
    result = await flow.async_step_contribute_preset(
        {CONF_DEVICE_ID: "dev-bat-2"}
    )
    assert result["type"] == "abort"
    assert result["reason"] == "contribute_incomplete"
    missing = result["description_placeholders"]["missing"]
    assert "Betriebsmodus (Select)" in missing
    assert "Leistungs-Sollwert (W, Number)" in missing


async def test_contribute_battery_posts_entity_and_value_map(
    hass: HomeAssistant,
):
    flow = _make_flow(hass, [BATTERY_DEV])
    result = await flow.async_step_contribute_preset(
        {CONF_DEVICE_ID: "dev-bat"}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "contribute_preset_form"

    captured: dict = {}

    async def fake_request(hass_, entry_, method, path, **kwargs):
        assert (method, path) == ("POST", "/api/v1/crowd-presets/contribute")
        captured.update(kwargs["json"])
        return _response(200, {"status": "staged", "contribution_count": 1})

    with patch(
        "custom_components.theothergas.config_flow._authenticated_config_request",
        new=AsyncMock(side_effect=fake_request),
    ):
        result = await flow.async_step_contribute_preset_form(
            {"vendor": "KOSTAL", "model": "Plenticore plus 8.5"}
        )

    assert result["type"] == "abort"
    assert result["reason"] == "contribute_success"
    assert result["description_placeholders"]["status"] == "staged"
    assert captured["device_type"] == "battery"
    assert captured["entity_map"] == {
        "entity_current_power_kw": "sensor.anlage_battery_power",
        "entity_soc_percent": "sensor.anlage_battery_soc",
        "entity_battery_mode": "select.anlage_battery_mode",
        "entity_battery_power_setpoint_w": "number.anlage_battery_setpoint",
    }
    assert captured["value_map"] == {
        "value_battery_mode_active": "External",
        "value_battery_mode_passive": "Internal",
        "battery_setpoint_invert_sign": "true",
        "entity_control_hold": "always",
    }
    # Install-Spezifisches bleibt draußen (Allowlist-Anonymisierung)
    assert "device_name" not in captured["entity_map"]


async def test_contribute_solar_payload_has_no_value_map(hass: HomeAssistant):
    """Solar-Regression: Payload wie v0.1 (entity_map only) — ohne
    gesetzte Werte/Flags wird kein value_map-Feld angehängt."""
    flow = _make_flow(hass, [SOLAR_DEV])
    await flow.async_step_contribute_preset({CONF_DEVICE_ID: "dev-solar"})
    captured: dict = {}

    async def fake_request(hass_, entry_, method, path, **kwargs):
        captured.update(kwargs["json"])
        return _response(200, {"status": "approved", "contribution_count": 4})

    with patch(
        "custom_components.theothergas.config_flow._authenticated_config_request",
        new=AsyncMock(side_effect=fake_request),
    ):
        result = await flow.async_step_contribute_preset_form(
            {"vendor": "KOSTAL", "model": "Plenticore plus 8.5"}
        )

    assert result["reason"] == "contribute_success"
    assert "value_map" not in captured
    assert captured["entity_map"] == {
        "entity_current_power_kw": "sensor.pv_sum_power_of_all_pv_dc_inputs",
        "entity_energy_total": "sensor.pv_energy_yield_total",
    }


async def test_add_preset_pick_stashes_value_map_and_opens_battery_step(
    hass: HomeAssistant,
):
    """Options-Add: Preset-Wahl übernimmt value_map als Step-Defaults,
    und ein Battery-Preset mit Dispatch-Slots öffnet den Battery-
    Werte-Step auch ohne gesetztes Lademodus-Select."""
    flow = _make_flow(hass, [])
    flow._pending_type = "battery"
    flow._pending_name = "Speicher"
    flow._pending_lookup_cache = [
        {
            "vendor": "KOSTAL",
            "model": "Plenticore plus 8.5",
            "entity_map": {
                "entity_current_power_kw": "sensor.x_battery_power",
                "entity_battery_mode": "select.x_battery_mode",
            },
            "value_map": {
                "value_battery_mode_active": "External",
                "value_battery_mode_passive": "Internal",
            },
        }
    ]

    result = await flow.async_step_add_vendor_preset_pick(
        {"preset_choice": "KOSTAL::Plenticore plus 8.5"}
    )
    assert result["step_id"] == "add_device_entities"
    assert flow._pending_preset_value_map == {
        "value_battery_mode_active": "External",
        "value_battery_mode_passive": "Internal",
    }

    # Entities-Step ohne Lademodus-Select submitted → Battery-Werte-Step
    # öffnet trotzdem (Preset schlägt die Dispatch-Steuerung vor).
    result = await flow._dispatch_add_post_entities(
        {"entity_current_power_kw": "sensor.eigene_battery_power"}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "add_device_battery_values"


async def test_pick_schema_marks_staged_presets(hass: HomeAssistant):
    schema = config_flow._vendor_preset_pick_schema(
        [
            {"vendor": "A", "model": "M1", "status": "approved",
             "contribution_count": 5},
            {"vendor": "B", "model": "M2", "status": "staged",
             "contribution_count": 1},
            {"vendor": "C", "model": "M3", "contribution_count": 2},
        ]
    )
    selector_cfg = next(iter(schema.schema.values())).config
    labels = {o["value"]: o["label"] for o in selector_cfg["options"]}
    assert "unbestätigt" not in labels["A::M1"]
    assert "unbestätigt" in labels["B::M2"]
    # fehlender status (Alt-Backend) = approved-Verhalten
    assert "unbestätigt" not in labels["C::M3"]


async def test_contribute_payload_carries_required_integrations(
    hass: HomeAssistant,
):
    """Der Contribute-Payload trägt die distinkten Integrationen des
    Mappings, damit das Backend sie speichert + neuen Usern anzeigt."""
    flow = _make_flow(hass, [BATTERY_DEV])
    await flow.async_step_contribute_preset({CONF_DEVICE_ID: "dev-bat"})
    captured: dict = {}

    async def fake_request(hass_, entry_, method, path, **kwargs):
        captured.update(kwargs["json"])
        return _response(200, {"status": "staged", "contribution_count": 1})

    with patch(
        "custom_components.theothergas.entity_mapper.required_integration_domains",
        return_value=["kostal_plenticore"],
    ), patch(
        "custom_components.theothergas.config_flow._authenticated_config_request",
        new=AsyncMock(side_effect=fake_request),
    ):
        result = await flow.async_step_contribute_preset_form(
            {"vendor": "KOSTAL", "model": "Plenticore plus 8.5"}
        )

    assert result["reason"] == "contribute_success"
    assert captured["required_integrations"] == ["kostal_plenticore"]


def test_pick_schema_labels_required_integrations(hass: HomeAssistant):
    """Der Profil-Picker zeigt pro Preset die benötigte(n) Integration(en)
    als Klarname (Slug-Fallback)."""
    schema = config_flow._vendor_preset_pick_schema(
        [
            # required_integrations (neu) → Klarnamen, dedupliziert
            {"vendor": "A", "model": "M1", "status": "approved",
             "contribution_count": 5,
             "required_integrations": ["kostal_plenticore", "go_e"]},
            # nur integration_domain (Alt-Backend) → Fallback auf Einzelwert
            {"vendor": "B", "model": "M2", "status": "approved",
             "contribution_count": 2, "integration_domain": "tibber"},
            # unbekannte Domain → aufgehübschter Slug
            {"vendor": "C", "model": "M3", "status": "approved",
             "contribution_count": 1,
             "required_integrations": ["my_inverter"]},
            # keine Integration → kein „benötigt"-Suffix
            {"vendor": "D", "model": "M4", "status": "approved",
             "contribution_count": 1},
        ]
    )
    selector_cfg = next(iter(schema.schema.values())).config
    labels = {o["value"]: o["label"] for o in selector_cfg["options"]}
    assert "benötigt: Kostal Plenticore, go-e" in labels["A::M1"]
    assert "benötigt: Tibber" in labels["B::M2"]
    assert "benötigt: My Inverter" in labels["C::M3"]
    assert "benötigt" not in labels["D::M4"]


def test_integration_display_name_known_and_fallback():
    from custom_components.theothergas.entity_mapper import (
        integration_display_name,
    )

    assert integration_display_name("kostal_plenticore") == "Kostal Plenticore"
    assert integration_display_name("go_e") == "go-e"
    # unbekannte Domain → Slug aufgehübscht
    assert integration_display_name("acme_meter_x") == "Acme Meter X"
    assert integration_display_name("") == ""


# ── required_helpers (HA-Helfer-Provisionierung, 2026-07-04) ───────────

# Batterie, deren Steuer-Slots auf selbst angelegte HA-HELFER zeigen
# (input_select/input_number) statt native Integrations-Entities — der
# reale Kostal-Fall (input_select treibt eine Modbus-Automation).
HELPER_BATTERY_DEV = {
    "device_id": "dev-bat-helper",
    "device_name": "Speicher (Helfer)",
    "device_type": "battery",
    "entity_current_power_kw": "sensor.solar_battery_power",
    "entity_soc_percent": "sensor.solar_battery_soc",
    "entity_battery_mode": "input_select.hausbatterie_lademodus",
    "value_battery_mode_active": "Extern",
    "value_battery_mode_passive": "Automatik",
    "entity_battery_power_setpoint_w": "input_number.hausbatterie_zielleistung",
}


def test_required_helper_specs_reads_input_helpers(hass: HomeAssistant):
    """Pure Funktion: liest je Helfer-Slot die HA-Config aus; native
    Entities (sensor/select/number) werden übersprungen."""
    from custom_components.theothergas.entity_mapper import required_helper_specs

    hass.states.async_set(
        "input_select.hausbatterie_lademodus",
        "Extern",
        {"options": ["Extern", "Automatik", "Laden"], "friendly_name": "Lademodus"},
    )
    hass.states.async_set(
        "input_number.hausbatterie_zielleistung",
        "0",
        {"min": -10000, "max": 10000, "step": 100, "unit_of_measurement": "W"},
    )
    specs = required_helper_specs(
        hass,
        {
            "entity_current_power_kw": "sensor.solar_battery_power",  # native → skip
            "entity_battery_mode": "input_select.hausbatterie_lademodus",
            "entity_battery_power_setpoint_w": "input_number.hausbatterie_zielleistung",
        },
    )
    assert specs == [
        {
            "slot": "entity_battery_mode",
            "type": "input_select",
            "options": ["Extern", "Automatik", "Laden"],
            "name": "Lademodus",
        },
        {
            "slot": "entity_battery_power_setpoint_w",
            "type": "input_number",
            "min": -10000.0,
            "max": 10000.0,
            "step": 100.0,
            "unit": "W",
            # kein friendly_name gesetzt → kein "name"
        },
    ]


def test_required_helper_specs_skips_unreadable_and_none_without_helpers(
    hass: HomeAssistant,
):
    from custom_components.theothergas.entity_mapper import required_helper_specs

    # native-only map → None
    assert (
        required_helper_specs(
            hass,
            {"entity_battery_mode": "select.native", "x": "sensor.y"},
        )
        is None
    )
    # input_select ohne options (State fehlt) → übersprungen → None
    assert (
        required_helper_specs(
            hass, {"entity_battery_mode": "input_select.ghost"}
        )
        is None
    )


async def test_contribute_payload_carries_required_helpers(hass: HomeAssistant):
    """Der Contribute-Payload trägt strukturierte Helfer-Specs, wenn die
    entity_map auf HA-Helfer zeigt — damit ein Empfänger sie nachbaut."""
    hass.states.async_set(
        "input_select.hausbatterie_lademodus",
        "Extern",
        {"options": ["Extern", "Automatik"], "friendly_name": "Lademodus"},
    )
    hass.states.async_set(
        "input_number.hausbatterie_zielleistung",
        "0",
        {"min": -5000, "max": 5000, "unit_of_measurement": "W"},
    )
    flow = _make_flow(hass, [HELPER_BATTERY_DEV])
    await flow.async_step_contribute_preset({CONF_DEVICE_ID: "dev-bat-helper"})
    captured: dict = {}

    async def fake_request(hass_, entry_, method, path, **kwargs):
        captured.update(kwargs["json"])
        return _response(200, {"status": "staged", "contribution_count": 1})

    with patch(
        "custom_components.theothergas.config_flow._authenticated_config_request",
        new=AsyncMock(side_effect=fake_request),
    ):
        result = await flow.async_step_contribute_preset_form(
            {"vendor": "KOSTAL", "model": "Plenticore plus 8.5"}
        )

    assert result["reason"] == "contribute_success"
    slots = {h["slot"]: h for h in captured["required_helpers"]}
    assert slots["entity_battery_mode"]["type"] == "input_select"
    assert slots["entity_battery_mode"]["options"] == ["Extern", "Automatik"]
    assert slots["entity_battery_power_setpoint_w"]["type"] == "input_number"
    assert slots["entity_battery_power_setpoint_w"]["min"] == -5000.0


def test_pick_schema_labels_required_helpers(hass: HomeAssistant):
    """Der Profil-Picker informiert, wenn ein Profil HA-Helfer braucht
    (required_helpers) — der User muss sie in HA anlegen."""
    schema = config_flow._vendor_preset_pick_schema(
        [
            {"vendor": "A", "model": "M1", "status": "approved",
             "contribution_count": 3,
             "required_helpers": [
                 {"slot": "entity_battery_mode", "type": "input_select",
                  "options": ["x"], "name": "Lademodus"},
                 {"slot": "entity_battery_power_setpoint_w",
                  "type": "input_number", "min": 0, "max": 1},
             ]},
            # ohne required_helpers → kein Hinweis
            {"vendor": "B", "model": "M2", "status": "approved",
             "contribution_count": 1},
        ]
    )
    selector_cfg = next(iter(schema.schema.values())).config
    labels = {o["value"]: o["label"] for o in selector_cfg["options"]}
    # Name wenn vorhanden, sonst Slot-Name
    assert "HA-Helfer nötig: Lademodus, entity_battery_power_setpoint_w" in labels["A::M1"]
    assert "HA-Helfer" not in labels["B::M2"]


async def test_contribute_native_entities_have_no_required_helpers(
    hass: HomeAssistant,
):
    """Native select/number-Entities (BATTERY_DEV) → kein required_helpers
    im Payload (nur echte input_*-Helfer werden mitgeschickt)."""
    flow = _make_flow(hass, [BATTERY_DEV])
    await flow.async_step_contribute_preset({CONF_DEVICE_ID: "dev-bat"})
    captured: dict = {}

    async def fake_request(hass_, entry_, method, path, **kwargs):
        captured.update(kwargs["json"])
        return _response(200, {"status": "staged", "contribution_count": 1})

    with patch(
        "custom_components.theothergas.config_flow._authenticated_config_request",
        new=AsyncMock(side_effect=fake_request),
    ):
        await flow.async_step_contribute_preset_form(
            {"vendor": "KOSTAL", "model": "Plenticore plus 8.5"}
        )

    assert "required_helpers" not in captured


# ── entity_identity_map: user-namens-unabhängige Preset-Auflösung ──────


def _register(hass, domain, object_id, platform, unique_id, **kwargs):
    from homeassistant.helpers import entity_registry as er

    return er.async_get(hass).async_get_or_create(
        domain, platform, unique_id,
        suggested_object_id=object_id, **kwargs,
    )


async def test_contribute_payload_carries_entity_identity_map(
    hass: HomeAssistant,
):
    """Der Contribute-Payload trägt je Entity-Slot die Registry-Identität
    (platform + translation_key/original_name) — NIE die unique_id
    (Seriennummern-PII)."""
    _register(
        hass, "sensor", "anlage_battery_power", "kostal_plenticore",
        "SERIAL123_battery_power", translation_key="battery_power",
    )
    _register(
        hass, "select", "anlage_battery_mode", "kostal_plenticore",
        "SERIAL123_battery_mode", original_name="Battery Operating Mode",
    )
    flow = _make_flow(hass, [BATTERY_DEV])
    await flow.async_step_contribute_preset({CONF_DEVICE_ID: "dev-bat"})
    captured: dict = {}

    async def fake_request(hass_, entry_, method, path, **kwargs):
        captured.update(kwargs["json"])
        return _response(200, {"status": "staged", "contribution_count": 1})

    with patch(
        "custom_components.theothergas.config_flow._authenticated_config_request",
        new=AsyncMock(side_effect=fake_request),
    ):
        result = await flow.async_step_contribute_preset_form(
            {"vendor": "KOSTAL", "model": "Plenticore plus 8.5"}
        )

    assert result["reason"] == "contribute_success"
    identity = captured["entity_identity_map"]
    assert identity["entity_current_power_kw"] == {
        "platform": "kostal_plenticore",
        "translation_key": "battery_power",
    }
    assert identity["entity_battery_mode"] == {
        "platform": "kostal_plenticore",
        "original_name": "Battery Operating Mode",
    }
    # Slots ohne Registry-Eintrag (setpoint/soc hier nicht registriert)
    # fehlen — und unique_id taucht NIRGENDS auf.
    assert "unique_id" not in str(identity)


async def test_resolve_preset_entities_via_identity_ignores_device_name(
    hass: HomeAssistant,
):
    """Contributor nannte den WR „Solar", der Empfänger „Wechselrichter"
    (und hat die Power-Entity sogar komplett umbenannt): die Registry-
    Identität löst trotzdem auf — der Suffix-Match hätte beim Voll-
    Rename keine Chance."""
    from custom_components.theothergas.entity_mapper import (
        resolve_preset_entities,
    )

    _register(
        hass, "sensor", "mein_speicher_leistung", "kostal_plenticore",
        "S9_battery_power", translation_key="battery_power",
    )
    resolved = resolve_preset_entities(
        hass,
        {"entity_current_power_kw": "sensor.solar_battery_power"},
        {"entity_current_power_kw": {
            "platform": "kostal_plenticore",
            "translation_key": "battery_power",
        }},
    )
    assert resolved == {
        "entity_current_power_kw": "sensor.mein_speicher_leistung"
    }


async def test_resolve_preset_entities_exact_and_ambiguous(
    hass: HomeAssistant,
):
    """Exakte ID gewinnt unverändert; mehrere Identity-Treffer
    (Multi-Inverter) → verbatim, nie raten."""
    from custom_components.theothergas.entity_mapper import (
        resolve_preset_entities,
    )

    # exakt: die Contributor-ID existiert hier
    hass.states.async_set("sensor.solar_battery_power", "1.0")
    ident = {"platform": "kostal_plenticore", "translation_key": "battery_power"}
    resolved = resolve_preset_entities(
        hass,
        {"entity_current_power_kw": "sensor.solar_battery_power"},
        {"entity_current_power_kw": ident},
    )
    assert resolved["entity_current_power_kw"] == "sensor.solar_battery_power"

    # mehrdeutig: zwei WR mit derselben Identität
    _register(
        hass, "sensor", "wr1_battery_power", "kostal_plenticore",
        "WR1_bp", translation_key="battery_power",
    )
    _register(
        hass, "sensor", "wr2_battery_power", "kostal_plenticore",
        "WR2_bp", translation_key="battery_power",
    )
    resolved = resolve_preset_entities(
        hass,
        {"entity_current_power_kw": "sensor.fremd_battery_power"},
        {"entity_current_power_kw": ident},
    )
    assert resolved["entity_current_power_kw"] == "sensor.fremd_battery_power"


async def test_resolve_preset_entities_suffix_fallback_without_identity(
    hass: HomeAssistant,
):
    """Alt-Preset ohne entity_identity_map → Suffix-Match (Box-Heuristik):
    eindeutiger same-domain-Suffix löst auf, unauflösbar bleibt verbatim
    (input_*-Helfer-Slots behalten so ihre Anlege-Anleitung)."""
    from custom_components.theothergas.entity_mapper import (
        resolve_preset_entities,
    )

    hass.states.async_set("sensor.wechselrichter_battery_power", "0.5")
    resolved = resolve_preset_entities(
        hass,
        {
            "entity_current_power_kw": "sensor.solar_battery_power",
            "entity_battery_mode": "input_select.hausbatterie_lademodus",
        },
        None,
    )
    assert resolved == {
        "entity_current_power_kw": "sensor.wechselrichter_battery_power",
        "entity_battery_mode": "input_select.hausbatterie_lademodus",
    }


async def test_add_preset_pick_resolves_entity_prefill(hass: HomeAssistant):
    """Flow-Ebene: der Profil-Pick befüllt den Entity-Step mit den
    AUFGELÖSTEN eigenen Entity-IDs statt der rohen Contributor-IDs."""
    _register(
        hass, "sensor", "wechselrichter_batterieleistung", "kostal_plenticore",
        "X_bp", translation_key="battery_power",
    )
    flow = _make_flow(hass, [])
    flow._pending_type = "battery"
    flow._pending_name = "Speicher"
    flow._pending_lookup_cache = [
        {
            "vendor": "KOSTAL",
            "model": "Plenticore plus 8.5",
            "entity_map": {
                "entity_current_power_kw": "sensor.solar_battery_power",
            },
            "value_map": {},
            "entity_identity_map": {
                "entity_current_power_kw": {
                    "platform": "kostal_plenticore",
                    "translation_key": "battery_power",
                },
            },
        }
    ]
    result = await flow.async_step_add_vendor_preset_pick(
        {"preset_choice": "KOSTAL::Plenticore plus 8.5"}
    )
    assert result["step_id"] == "add_device_entities"
    assert flow._pending_preset_entity_map == {
        "entity_current_power_kw": "sensor.wechselrichter_batterieleistung"
    }
