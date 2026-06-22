"""Full-flow integration test for the Coordinator telemetry tick (#21).

`test_sync_stack_characterization` pins the *decision* matrices
(`_should_send`, dispatch routing) and `test_hold_loops_and_eviction`
the background loops, but neither drives the actual
`_async_update_data` round-trip: read live HA entity states → compose the
PATCH payload → send → update the energy/high-water bookkeeping. That
end-to-end path (#21 "Coordinator-/Full-Flow-Integrationstests") had no
coverage. This file closes it with a real `hass` + real entity states,
mocking only the network send and the orthogonal side-loops
(outdoor-temp push, self-heal) so the assertion is purely about payload
composition + bookkeeping.

Like the sibling characterization files this PINS today's behaviour as the
non-regression harness that must stand BEFORE the planned coordinator
disentanglement (FEAT-5 Rest), not a wished-for behaviour.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.theothergas.const import (
    CONF_DEVICE_ID,
    CONF_DEVICE_TYPE,
    CONF_ENTITY_CONTROL,
    CONF_ENTITY_ENERGY_TOTAL,
    CONF_ENTITY_POWER,
    CONF_SUPPORTS_COOLING,
    CONF_VALUE_COOL_ON,
    CONF_VALUE_OFF,
    CONF_VALUE_ON,
    DOMAIN,
    OPT_CONSENT_TELEMETRY,
)
from custom_components.theothergas.coordinator import CrowdergyCoordinator
from custom_components.theothergas.state_mirror import DeviceStateMirror


def _ok_response() -> httpx.Response:
    return httpx.Response(
        200, json={}, request=httpx.Request("PATCH", "https://api.example/x")
    )


def _make_coord(hass: HomeAssistant, devices: list[dict]) -> CrowdergyCoordinator:
    """Coordinator skeleton wired for the `_async_update_data` path: telemetry
    consent on, bootstrap already done, network send + side-loops mocked."""
    entry = MockConfigEntry(
        domain=DOMAIN, data={}, options={OPT_CONSENT_TELEMETRY: True}
    )
    entry.add_to_hass(hass)
    coord = CrowdergyCoordinator.__new__(CrowdergyCoordinator)
    coord.hass = hass
    coord.entry = entry
    coord.devices = devices
    coord.data = None
    coord.state = DeviceStateMirror()
    coord.state.active_state_bootstrapped = True  # skip the GET /devices seed
    coord._connector_version = "9.9.9"
    coord._consent_denied_logged = set()
    coord._backend_gone_device_ids = set()
    coord._last_sent_payload = {}
    coord._last_send_at = {}
    coord._last_mirror_at = {}
    coord._last_sent_hash = {}
    coord._prev_energy_kwh = {}
    coord._prev_energy_kwh_discharged = {}
    # Mock the network send + the two orthogonal side-effects so the test is
    # purely about payload composition / bookkeeping.
    coord._patch_telemetry_with_retry = AsyncMock(return_value=_ok_response())
    coord._push_outdoor_temp = AsyncMock()
    coord._self_heal_holds = AsyncMock()
    return coord


def _sent_payload(coord: CrowdergyCoordinator) -> dict:
    """The payload of the latest PATCH (device_id, payload) call."""
    return coord._patch_telemetry_with_retry.await_args.args[1]


async def test_tick_composes_power_and_energy_payload(hass: HomeAssistant):
    """First tick: W→kW power normalisation + cumulative energy land in the
    PATCH payload; the first read carries NO energy_kwh_delta (no baseline
    yet) and the high-water baseline is seeded only after a successful send."""
    dev = {
        CONF_DEVICE_ID: "dev-1",
        CONF_DEVICE_TYPE: "heating",
        CONF_ENTITY_POWER: "sensor.wp_power",
        CONF_ENTITY_ENERGY_TOTAL: "sensor.wp_energy",
    }
    coord = _make_coord(hass, [dev])
    hass.states.async_set(
        "sensor.wp_power", "1500", {"unit_of_measurement": "W"}
    )
    hass.states.async_set(
        "sensor.wp_energy", "100.0", {"unit_of_measurement": "kWh"}
    )

    result = await coord._async_update_data()

    coord._patch_telemetry_with_retry.assert_awaited_once()
    payload = _sent_payload(coord)
    assert payload["power_kw"] == 1.5            # 1500 W → 1.5 kW
    assert payload["is_online"] is True
    assert payload["energy_kwh_total"] == 100.0
    assert "energy_kwh_delta" not in payload     # first read → no Δ
    # Bookkeeping seeded only on success → next tick computes Δ against this.
    assert coord._prev_energy_kwh["dev-1"] == 100.0
    assert result["dev-1"]["current_power_kw"] == 1.5


async def test_second_tick_emits_energy_delta(hass: HomeAssistant):
    """A subsequent tick with a higher cumulative counter emits both the
    signed `energy_kwh_delta` and the unsigned `energy_kwh_in_delta`
    (v3.21.4 dual-path) — the end-to-end counter-Δ round-trip."""
    dev = {
        CONF_DEVICE_ID: "dev-1",
        CONF_DEVICE_TYPE: "heating",
        CONF_ENTITY_POWER: "sensor.wp_power",
        CONF_ENTITY_ENERGY_TOTAL: "sensor.wp_energy",
    }
    coord = _make_coord(hass, [dev])
    hass.states.async_set("sensor.wp_power", "1.0", {"unit_of_measurement": "kW"})
    hass.states.async_set(
        "sensor.wp_energy", "100.0", {"unit_of_measurement": "kWh"}
    )
    await coord._async_update_data()            # seeds baseline at 100.0

    hass.states.async_set(
        "sensor.wp_energy", "102.0", {"unit_of_measurement": "kWh"}
    )
    await coord._async_update_data()

    payload = _sent_payload(coord)
    assert payload["energy_kwh_delta"] == 2.0
    assert payload["energy_kwh_in_delta"] == 2.0
    assert coord._prev_energy_kwh["dev-1"] == 102.0


async def test_failed_send_does_not_advance_energy_baseline(hass: HomeAssistant):
    """If the PATCH fails (raise_for_status), the high-water baseline must
    NOT advance — the lost kWh is re-attempted on the next tick rather than
    silently dropped. Pins the on-success-only bookkeeping invariant."""
    dev = {
        CONF_DEVICE_ID: "dev-1",
        CONF_DEVICE_TYPE: "heating",
        CONF_ENTITY_POWER: "sensor.wp_power",
        CONF_ENTITY_ENERGY_TOTAL: "sensor.wp_energy",
    }
    coord = _make_coord(hass, [dev])
    coord._patch_telemetry_with_retry = AsyncMock(
        return_value=httpx.Response(
            500, request=httpx.Request("PATCH", "https://api.example/x")
        )
    )
    hass.states.async_set("sensor.wp_power", "1.0", {"unit_of_measurement": "kW"})
    hass.states.async_set(
        "sensor.wp_energy", "50.0", {"unit_of_measurement": "kWh"}
    )

    await coord._async_update_data()

    coord._patch_telemetry_with_retry.assert_awaited_once()
    # 500 → raise_for_status → except branch → baseline NOT seeded.
    assert "dev-1" not in coord._prev_energy_kwh


async def test_prefetch_reads_shared_entity_once(hass: HomeAssistant, monkeypatch):
    """#56: a cooling-capable device whose `is_on` AND `cool_on` both read the
    shared `entity_control` fetches that entity from `hass.states` exactly
    once per tick (the per-device prefetch snapshot), not once per reader."""
    from homeassistant.core import StateMachine

    dev = {
        CONF_DEVICE_ID: "dev-1",
        CONF_DEVICE_TYPE: "heating",
        CONF_ENTITY_POWER: "sensor.wp_power",
        CONF_ENTITY_CONTROL: "climate.wp",
        CONF_SUPPORTS_COOLING: True,
        CONF_VALUE_ON: "heat",
        CONF_VALUE_OFF: "off",
        CONF_VALUE_COOL_ON: "cool",
    }
    coord = _make_coord(hass, [dev])
    hass.states.async_set("sensor.wp_power", "1.0", {"unit_of_measurement": "kW"})
    hass.states.async_set("climate.wp", "heat", {})

    counts: dict[str, int] = {}
    original_get = StateMachine.get

    def _counting_get(self, entity_id):
        counts[entity_id] = counts.get(entity_id, 0) + 1
        return original_get(self, entity_id)

    monkeypatch.setattr(StateMachine, "get", _counting_get)
    await coord._async_update_data()

    # is_on + cool_on both reference climate.wp, but the prefetch fetched it
    # once (would be 2 without #56).
    assert counts.get("climate.wp", 0) == 1
    # …and the snapshot was actually used: "heat" → is_on True.
    assert _sent_payload(coord)["is_on"] is True


async def test_telemetry_consent_off_sends_nothing(hass: HomeAssistant):
    """No telemetry consent → the whole tick is a no-op send-wise (the
    consent gate at the top of _async_update_data)."""
    dev = {
        CONF_DEVICE_ID: "dev-1",
        CONF_DEVICE_TYPE: "heating",
        CONF_ENTITY_POWER: "sensor.wp_power",
    }
    coord = _make_coord(hass, [dev])
    coord.entry = MockConfigEntry(
        domain=DOMAIN, data={}, options={OPT_CONSENT_TELEMETRY: False}
    )
    coord.entry.add_to_hass(hass)
    hass.states.async_set("sensor.wp_power", "1.0", {"unit_of_measurement": "kW"})

    await coord._async_update_data()

    coord._patch_telemetry_with_retry.assert_not_awaited()
    coord._push_outdoor_temp.assert_not_awaited()
