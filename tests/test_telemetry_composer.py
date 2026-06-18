"""Regression-Tests für TelemetryComposer (Review-Fixes 2026-06-11).

* CN-1: `resync_once` skippt KOMPLETT (inkl. GET) ohne Remote-Control-
  Consent; mit Consent repariert er Drift weiterhin via `_apply_*`.
* CN-5: der Device-Mirror bucht auf `_last_mirror_at` — `_last_send_at`
  bleibt exklusiv für echte Sends, sodass 90-s-Soft-Heartbeat und
  600-s-Hard-Ceiling in `_should_send` wieder greifen.
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock

import httpx
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.theothergas.const import (
    DOMAIN,
    OPT_CONSENT_REMOTE_CONTROL,
    OPT_CONSENT_TELEMETRY,
)
from custom_components.theothergas.coordinator import CrowdergyCoordinator
from custom_components.theothergas.state_mirror import DeviceStateMirror
from custom_components.theothergas.telemetry_composer import TelemetryComposer


def make_coordinator(
    hass: HomeAssistant, *, options: dict | None = None
) -> CrowdergyCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=dict(options or {}))
    entry.add_to_hass(hass)
    coord = CrowdergyCoordinator.__new__(CrowdergyCoordinator)
    coord.hass = hass
    coord.entry = entry
    coord.devices = []
    coord.state = DeviceStateMirror()
    coord._consent_denied_logged = set()
    coord._backend_gone_device_ids = set()  # v3.26.0: 404/410-Gate (siehe __init__)
    coord._last_sent_payload = {}
    coord._last_send_at = {}
    coord._last_mirror_at = {}
    coord._last_sent_hash = {}
    return coord


def _response(status: int, payload) -> httpx.Response:
    return httpx.Response(
        status,
        json=payload,
        request=httpx.Request("GET", "https://api.example/x"),
    )


# ── CN-1: Resync-Gate ────────────────────────────────────────────────


async def test_resync_skips_entirely_without_consent(hass: HomeAssistant):
    coord = make_coordinator(
        hass, options={OPT_CONSENT_REMOTE_CONTROL: False}
    )
    coord._authenticated_request = AsyncMock()
    coord._apply_device_state = AsyncMock()
    coord._apply_cool_state = AsyncMock()
    composer = TelemetryComposer(coord)

    await composer.resync_once()

    # Kein GET, kein Apply — der ganze Tick ist ohne Consent sinnlos.
    coord._authenticated_request.assert_not_awaited()
    coord._apply_device_state.assert_not_awaited()
    coord._apply_cool_state.assert_not_awaited()


async def test_resync_reapplies_drift_with_consent(hass: HomeAssistant):
    """Gegentest: Default-Consent → Drift-Repair läuft wie vorher."""
    coord = make_coordinator(hass)
    coord._authenticated_request = AsyncMock(
        return_value=_response(
            200,
            [{"id": "d1", "is_active": True, "is_on": True, "cool_on": False}],
        )
    )
    coord._apply_device_state = AsyncMock()
    coord._apply_cool_state = AsyncMock()
    coord.state.on_state["d1"] = False  # lokaler Cache hinkt hinterher
    composer = TelemetryComposer(coord)

    await composer.resync_once()

    coord._apply_device_state.assert_awaited_once_with("d1", True)
    assert coord.state.on_state["d1"] is True


# ── CN-5: Mirror auf eigenem Timestamp ───────────────────────────────


async def test_mirror_does_not_reset_last_send_at(hass: HomeAssistant):
    coord = make_coordinator(hass)
    sent_at = time.time() - 70.0
    coord._last_sent_payload["d1"] = {
        "power_kw": 1.0, "is_online": True, "energy_kwh_delta": 0.4,
    }
    coord._last_send_at["d1"] = sent_at
    coord._authenticated_request = AsyncMock(return_value=_response(200, {}))
    composer = TelemetryComposer(coord)

    await composer.mirror_once()

    coord._authenticated_request.assert_awaited_once()
    method, path = coord._authenticated_request.await_args.args
    assert (method, path) == ("PATCH", "/api/v1/devices/d1/telemetry")
    # Δ-kWh darf im Mirror nie doppelt landen.
    assert "energy_kwh_delta" not in (
        coord._authenticated_request.await_args.kwargs["json"]
    )
    # Kern von CN-5: der echte Send-Timestamp bleibt unangetastet …
    assert coord._last_send_at["d1"] == sent_at
    # … der Mirror bucht auf sein eigenes Feld.
    assert coord._last_mirror_at["d1"] > sent_at


async def test_mirror_strips_all_energy_delta_fields(hass: HomeAssistant):
    """#62: der Mirror replayt das letzte Payload, muss aber JEDES
    Energie-Δ-Feld droppen — den signed `energy_kwh_delta` UND das
    unsigned `energy_kwh_in_delta`/`energy_kwh_out_delta`-Paar, das das
    Backend bevorzugt und (signed) re-derivt. Vorher blieb das
    unsigned Paar im Mirror → Backend zählte die Δ-kWh ein zweites Mal
    (Solar-kWh-Counter zu hoch)."""
    coord = make_coordinator(hass)
    coord._last_sent_payload["d1"] = {
        "power_kw": 0.4,
        "is_online": True,
        "energy_kwh_delta": 0.4,
        "energy_kwh_in_delta": 0.4,
        "energy_kwh_out_delta": 0.0,
    }
    coord._last_send_at["d1"] = time.time() - 70.0
    coord._authenticated_request = AsyncMock(return_value=_response(200, {}))
    composer = TelemetryComposer(coord)

    await composer.mirror_once()

    coord._authenticated_request.assert_awaited_once()
    sent = coord._authenticated_request.await_args.kwargs["json"]
    # Kein Δ-Feld darf im Mirror landen.
    assert "energy_kwh_delta" not in sent
    assert "energy_kwh_in_delta" not in sent
    assert "energy_kwh_out_delta" not in sent
    # Non-Δ-Felder reiten weiter mit (Freshness-Mirror).
    assert sent["power_kw"] == 0.4
    assert sent["is_online"] is True


async def test_mirror_respects_own_interval(hass: HomeAssistant):
    """Frischer Mirror → kein erneuter PATCH, auch wenn der letzte
    echte Send lange her ist (Mirror-Kadenz bleibt 60 s)."""
    coord = make_coordinator(hass)
    coord._last_sent_payload["d1"] = {"power_kw": 1.0, "is_online": True}
    coord._last_send_at["d1"] = time.time() - 500.0
    coord._last_mirror_at["d1"] = time.time() - 10.0
    coord._authenticated_request = AsyncMock(return_value=_response(200, {}))
    composer = TelemetryComposer(coord)

    await composer.mirror_once()

    coord._authenticated_request.assert_not_awaited()


async def test_hard_ceiling_fires_despite_recent_mirror(hass: HomeAssistant):
    """600-s-Hard-Ceiling in `_should_send` war durch das Mirror-
    Reset von `_last_send_at` toter Code — jetzt feuert er wieder,
    egal wie frisch der letzte Mirror war."""
    coord = make_coordinator(hass)
    payload = {"power_kw": 1.0, "is_online": True}
    coord._last_sent_payload["d1"] = dict(payload)
    coord._last_send_at["d1"] = time.time() - 601.0
    coord._last_sent_hash["d1"] = coord._payload_hash(payload)
    coord._last_mirror_at["d1"] = time.time() - 5.0

    assert coord._should_send("d1", dict(payload)) is True


async def test_mirror_skipped_without_telemetry_consent(hass: HomeAssistant):
    """services.yaml verspricht: telemetry=false stoppt ALLE
    Telemetrie-Pushes — der Mirror re-PATCHt Telemetrie und ist
    deshalb genauso gated wie `_async_update_data`."""
    coord = make_coordinator(hass, options={OPT_CONSENT_TELEMETRY: False})
    coord._last_sent_payload["d1"] = {"power_kw": 1.0, "is_online": True}
    coord._last_send_at["d1"] = time.time() - 500.0
    coord._authenticated_request = AsyncMock(return_value=_response(200, {}))
    composer = TelemetryComposer(coord)

    await composer.mirror_once()

    coord._authenticated_request.assert_not_awaited()
