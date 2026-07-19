"""Charakterisierungs-Tests für den Coordinator-Sync-Stack.

Diese Tests beschreiben das HEUTIGE Verhalten (Stand v3.23.0, nach den
Review-Fixes CN-1…CN-14) der Mechanismen, die sich den geteilten
`DeviceStateMirror`-State teilen — `_should_send` (Telemetrie-Drossel),
`_apply_battery_setpoint` (±10-W-Idempotenz) und die Dispatch-Routing-
Matrix in `_handle_ws_message`.

Zweck: Sie sind das Sicherheitsnetz für einen späteren Refactor des
Sync-Stacks (geplanter State-Reducer, durch den alle Schreibpfade laufen).
Sie sollen NICHT ein gewünschtes Verhalten vorschreiben, sondern das
bestehende festschreiben — schlägt nach einem Refactor einer fehl, ist
das ein bewusst zu prüfender Verhaltenswechsel, kein „Test kaputt".

Die existierenden `test_consent_and_guards.py` / `test_telemetry_composer.py`
decken die einzelnen Fix-Garantien ab (Consent-Gates, Self-Heal-Stale,
Mirror-Timestamp-Trennung); hier geht es um die noch ununterstützten
Entscheidungs-/Routing-Matrizen.
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.theothergas.const import (
    CONF_DEVICE_ID,
    CONF_DEVICE_TYPE,
    CONF_ENTITY_BATTERY_MODE,
    CONF_ENTITY_BATTERY_POWER_SETPOINT,
    CONF_VALUE_BATTERY_MODE_ACTIVE,
    CONF_VALUE_BATTERY_MODE_PASSIVE,
    DOMAIN,
    OPT_CONSENT_REMOTE_CONTROL,
)
from custom_components.theothergas.coordinator import (
    IDENTICAL_HEARTBEAT_INTERVAL,
    PER_DEVICE_HEARTBEAT_INTERVAL,
    CrowdergyCoordinator,
    _counter_delta,
)
from custom_components.theothergas.state_mirror import DeviceStateMirror


def make_coordinator(
    hass: HomeAssistant,
    devices: list[dict],
    *,
    options: dict | None = None,
) -> CrowdergyCoordinator:
    """Coordinator ohne DataUpdateCoordinator-Init — nur die Attribute,
    die die hier getesteten Pfade lesen. Gleiches Muster wie
    test_consent_and_guards.make_coordinator (bewusst lokal dupliziert,
    damit die Charakterisierungs-Datei eigenständig bleibt)."""
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=dict(options or {}))
    entry.add_to_hass(hass)
    coord = CrowdergyCoordinator.__new__(CrowdergyCoordinator)
    coord.hass = hass
    coord.entry = entry
    coord.devices = devices
    coord.state = DeviceStateMirror()
    coord._consent_denied_logged = set()
    coord._backend_gone_device_ids = set()  # v3.26.0: 404/410-Gate (siehe __init__)
    coord._last_sent_payload = {}
    coord._last_send_at = {}
    coord._last_mirror_at = {}
    coord._last_sent_hash = {}
    coord._prev_energy_kwh = {}
    coord._prev_energy_kwh_discharged = {}
    return coord


NO_CONSENT = {OPT_CONSENT_REMOTE_CONTROL: False}


# ════════════════════════════════════════════════════════════════════
# A. `_should_send` — Telemetrie-Drossel-Entscheidungsmatrix
#    (pure Funktion; pinnt die exakten Schwellen & Heartbeat-Floors)
# ════════════════════════════════════════════════════════════════════


def _seed_prev(coord, device_id, payload, *, age_s):
    """Verankert einen letzten Send für `device_id`: payload + Hash +
    Wall-Clock-Alter (`age_s` Sekunden her)."""
    coord._last_sent_payload[device_id] = dict(payload)
    coord._last_sent_hash[device_id] = coord._payload_hash(payload)
    coord._last_send_at[device_id] = time.time() - age_s


def test_should_send_first_payload_always_sends(hass: HomeAssistant):
    coord = make_coordinator(hass, [])
    assert coord._should_send("d1", {"power_kw": 1.0}) is True


def test_should_send_identical_fresh_is_suppressed(hass: HomeAssistant):
    coord = make_coordinator(hass, [])
    _seed_prev(coord, "d1", {"power_kw": 1.0}, age_s=1.0)
    assert coord._should_send("d1", {"power_kw": 1.0}) is False


def test_should_send_subthreshold_power_drift_suppressed(hass: HomeAssistant):
    """power_kw-Schwelle ist 50 W — 40 W Drift fließt nicht."""
    coord = make_coordinator(hass, [])
    _seed_prev(coord, "d1", {"power_kw": 1.0}, age_s=1.0)
    assert coord._should_send("d1", {"power_kw": 1.04}) is False


def test_should_send_power_threshold_crossing_sends(hass: HomeAssistant):
    """50 W exakt erreicht die Schwelle (>=)."""
    coord = make_coordinator(hass, [])
    _seed_prev(coord, "d1", {"power_kw": 1.0}, age_s=1.0)
    assert coord._should_send("d1", {"power_kw": 1.05}) is True


def test_should_send_soc_threshold_is_one_point(hass: HomeAssistant):
    coord = make_coordinator(hass, [])
    _seed_prev(coord, "d1", {"soc_percent": 50.0}, age_s=1.0)
    assert coord._should_send("d1", {"soc_percent": 50.5}) is False
    assert coord._should_send("d1", {"soc_percent": 51.0}) is True


def test_should_send_temp_threshold_is_point_three(hass: HomeAssistant):
    coord = make_coordinator(hass, [])
    _seed_prev(coord, "d1", {"current_temp_c": 20.0}, age_s=1.0)
    assert coord._should_send("d1", {"current_temp_c": 20.2}) is False
    assert coord._should_send("d1", {"current_temp_c": 20.3}) is True


def test_should_send_categorical_flip_sends(hass: HomeAssistant):
    """is_on/cool_on/charge_mode/vehicle_status: jede Änderung sendet."""
    coord = make_coordinator(hass, [])
    _seed_prev(coord, "d1", {"is_on": False, "power_kw": 1.0}, age_s=1.0)
    assert coord._should_send("d1", {"is_on": True, "power_kw": 1.0}) is True


def test_should_send_any_energy_delta_sends(hass: HomeAssistant):
    coord = make_coordinator(hass, [])
    _seed_prev(coord, "d1", {"power_kw": 1.0}, age_s=1.0)
    payload = {"power_kw": 1.0, "energy_kwh_delta": 0.2}
    assert coord._should_send("d1", payload) is True


def test_should_send_presence_flip_sends(hass: HomeAssistant):
    """Ein zuvor vorhandenes numerisches Feld, das jetzt fehlt (None),
    zählt als Änderung."""
    coord = make_coordinator(hass, [])
    _seed_prev(coord, "d1", {"power_kw": 1.0}, age_s=1.0)
    assert coord._should_send("d1", {}) is True


def test_should_send_soft_heartbeat_sends_when_hash_differs(
    hass: HomeAssistant,
):
    """Ab 90 s sendet ein unter-schwelliger, aber vom letzten Hash
    abweichender Payload (Soft-Heartbeat)."""
    coord = make_coordinator(hass, [])
    _seed_prev(coord, "d1", {"power_kw": 1.0}, age_s=95.0)
    # Sub-threshold-Drift (40 W) ⇒ keine Schwelle, aber Hash ≠ letzter.
    assert coord._should_send("d1", {"power_kw": 1.04}) is True


def test_should_send_soft_heartbeat_waits_when_hash_identical(
    hass: HomeAssistant,
):
    """Bei 90 s, aber IDENTISCHEM Payload-Hash, wird NICHT gesendet —
    es wird auf das Hard-Ceiling gewartet (spart HTTP auf ruhigen
    Geräten)."""
    coord = make_coordinator(hass, [])
    _seed_prev(coord, "d1", {"power_kw": 1.0}, age_s=95.0)
    assert PER_DEVICE_HEARTBEAT_INTERVAL <= 95.0 < IDENTICAL_HEARTBEAT_INTERVAL
    assert coord._should_send("d1", {"power_kw": 1.0}) is False


def test_should_send_hard_ceiling_sends_even_if_identical(
    hass: HomeAssistant,
):
    """Ab dem Hard-Ceiling (600 s) sendet auch ein bit-identischer
    Payload — Backend-Cache-Refresh + Self-Healing der Near-Dup-Gate."""
    coord = make_coordinator(hass, [])
    _seed_prev(coord, "d1", {"power_kw": 1.0}, age_s=605.0)
    assert coord._should_send("d1", {"power_kw": 1.0}) is True


# ════════════════════════════════════════════════════════════════════
# B. `_apply_battery_setpoint` — ±10-W-Idempotenz-Toleranz
# ════════════════════════════════════════════════════════════════════


def _battery_device():
    return {
        CONF_DEVICE_ID: "batt",
        CONF_DEVICE_TYPE: "battery",
        CONF_ENTITY_BATTERY_MODE: "select.batt_mode",
        CONF_ENTITY_BATTERY_POWER_SETPOINT: "number.batt_setpoint",
        CONF_VALUE_BATTERY_MODE_ACTIVE: "Aktiv",
        CONF_VALUE_BATTERY_MODE_PASSIVE: "Passiv",
    }


async def test_battery_setpoint_within_10w_skips_write(hass: HomeAssistant):
    """Aktueller Setpoint 1000 W, neuer 1005 W (Δ 5 W ≤ 10 W) ⇒ kein
    number.set_value-Write (und Mode bereits aktiv ⇒ kein select)."""
    coord = make_coordinator(hass, [_battery_device()])
    hass.states.async_set("number.batt_setpoint", "1000")
    hass.states.async_set("select.batt_mode", "Aktiv")
    set_value = async_mock_service(hass, "number", "set_value")
    select = async_mock_service(hass, "select", "select_option")

    await coord._apply_battery_setpoint("batt", "charge", 1.005)

    assert set_value == []
    assert select == []


async def test_battery_setpoint_beyond_10w_writes(hass: HomeAssistant):
    """1000 W → 1020 W (Δ 20 W > 10 W) ⇒ number.set_value feuert mit den
    Watts (kW × 1000)."""
    coord = make_coordinator(hass, [_battery_device()])
    hass.states.async_set("number.batt_setpoint", "1000")
    hass.states.async_set("select.batt_mode", "Aktiv")
    set_value = async_mock_service(hass, "number", "set_value")

    await coord._apply_battery_setpoint("batt", "charge", 1.02)

    assert len(set_value) == 1
    assert set_value[0].data["value"] == 1020.0


async def test_battery_passive_writes_only_mode(hass: HomeAssistant):
    """mode=='passive' schreibt NUR den Mode-Select auf Passiv, nie den
    Power-Setpoint (WR übernimmt PV-Native-Priority)."""
    coord = make_coordinator(hass, [_battery_device()])
    hass.states.async_set("number.batt_setpoint", "1000")
    hass.states.async_set("select.batt_mode", "Aktiv")
    set_value = async_mock_service(hass, "number", "set_value")
    select = async_mock_service(hass, "select", "select_option")

    await coord._apply_battery_setpoint("batt", "passive", 0.0)

    assert set_value == []
    assert len(select) == 1
    assert select[0].data["option"] == "Passiv"


# ════════════════════════════════════════════════════════════════════
# C. `_handle_ws_message` — Dispatch-Routing-Matrix
#    (welcher Frame ruft welche _apply_*-Methode; Consent-Gate; Guards)
# ════════════════════════════════════════════════════════════════════


def _routing_coordinator(hass, devices, *, options=None):
    """Coordinator mit gemockten Apply-Methoden + neutralisierten
    State-Sync-/Hold-Helpern — isoliert das reine Routing."""
    coord = make_coordinator(hass, devices, options=options)
    coord._apply_device_state = AsyncMock()
    coord._apply_cool_state = AsyncMock()
    coord._apply_vorlauf_setpoint = AsyncMock()
    coord._apply_battery_setpoint = AsyncMock()
    coord._apply_charge_mode = AsyncMock()
    coord._sync_field_into_data = lambda *a, **k: None
    coord._cancel_hold = lambda *a, **k: None
    coord._cancel_charge_mode_hold = lambda *a, **k: None
    return coord


async def test_dispatch_telemetry_is_on_routes_to_device_state(
    hass: HomeAssistant,
):
    coord = _routing_coordinator(hass, [{CONF_DEVICE_ID: "d1", CONF_DEVICE_TYPE: "heating"}])
    await coord._handle_ws_message(
        {"type": "telemetry", "device_id": "d1", "data": {"is_on": True}}
    )
    coord._apply_device_state.assert_awaited_once_with("d1", True)


async def test_dispatch_telemetry_idempotent_when_cache_matches(
    hass: HomeAssistant,
):
    """is_on-Frame, dessen Wert dem gecachten on_state entspricht, löst
    KEINEN Apply aus (Echo-Suppression gegen Resync-Schleifen)."""
    coord = _routing_coordinator(hass, [{CONF_DEVICE_ID: "d1", CONF_DEVICE_TYPE: "heating"}])
    coord.state.on_state["d1"] = True
    await coord._handle_ws_message(
        {"type": "telemetry", "device_id": "d1", "data": {"is_on": True}}
    )
    coord._apply_device_state.assert_not_awaited()


async def test_dispatch_cool_on_processed_before_is_on(hass: HomeAssistant):
    """v3.6.4-Garantie: cool_on wird VOR is_on dispatcht, damit der
    heat-side-Skip-Guard den frisch gesetzten cool_state sieht."""
    coord = _routing_coordinator(hass, [{CONF_DEVICE_ID: "d1", CONF_DEVICE_TYPE: "heating"}])
    order: list[str] = []
    coord._apply_cool_state = AsyncMock(side_effect=lambda *a: order.append("cool"))
    coord._apply_device_state = AsyncMock(side_effect=lambda *a: order.append("on"))
    await coord._handle_ws_message(
        {
            "type": "telemetry",
            "device_id": "d1",
            "data": {"is_on": True, "cool_on": True},
        }
    )
    assert order == ["cool", "on"]


async def test_dispatch_vorlauf_setpoint_routes_and_coerces_float(
    hass: HomeAssistant,
):
    coord = _routing_coordinator(hass, [{CONF_DEVICE_ID: "d1", CONF_DEVICE_TYPE: "heating"}])
    await coord._handle_ws_message(
        {"type": "telemetry", "device_id": "d1", "data": {"vorlauf_setpoint_c": "45"}}
    )
    coord._apply_vorlauf_setpoint.assert_awaited_once_with("d1", 45.0)


async def test_dispatch_command_battery_reads_mode_and_setpoint(
    hass: HomeAssistant,
):
    coord = _routing_coordinator(hass, [{CONF_DEVICE_ID: "b1", CONF_DEVICE_TYPE: "battery"}])
    await coord._handle_ws_message(
        {
            "type": "command",
            "action": "set_charge_mode",
            "device_id": "b1",
            "mode": "charge",
            "setpoint_kw": 2.0,
        }
    )
    coord._apply_battery_setpoint.assert_awaited_once_with("b1", "charge", 2.0)


async def test_dispatch_command_wallbox_reads_value(hass: HomeAssistant):
    coord = _routing_coordinator(hass, [{CONF_DEVICE_ID: "w1", CONF_DEVICE_TYPE: "wallbox"}])
    await coord._handle_ws_message(
        {
            "type": "command",
            "action": "set_charge_mode",
            "device_id": "w1",
            "value": "solar",
        }
    )
    coord._apply_charge_mode.assert_awaited_once_with(
        "w1", "solar", charge_current_a=None, charge_phases=None
    )


async def test_dispatch_command_wallbox_forwards_charge_current(hass: HomeAssistant):
    """2026-06-20: ein Power-Mode-Frame mit `current_a` reicht den
    Ladestrom (ganze Ampere) an `_apply_charge_mode` durch."""
    coord = _routing_coordinator(hass, [{CONF_DEVICE_ID: "w1", CONF_DEVICE_TYPE: "wallbox"}])
    await coord._handle_ws_message(
        {
            "type": "command",
            "action": "set_charge_mode",
            "device_id": "w1",
            "value": "Power Mode",
            "current_a": 10,
        }
    )
    coord._apply_charge_mode.assert_awaited_once_with(
        "w1", "Power Mode", charge_current_a=10, charge_phases=None
    )


async def test_dispatch_command_unknown_device_is_ignored(hass: HomeAssistant):
    """P3-Fix: ein set_charge_mode für eine lokal unbekannte device_id
    fällt nicht mehr in den Wallbox-Zweig — Frame wird ignoriert."""
    coord = _routing_coordinator(hass, [{CONF_DEVICE_ID: "w1", CONF_DEVICE_TYPE: "wallbox"}])
    await coord._handle_ws_message(
        {
            "type": "command",
            "action": "set_charge_mode",
            "device_id": "ghost",
            "value": "solar",
        }
    )
    coord._apply_charge_mode.assert_not_awaited()
    coord._apply_battery_setpoint.assert_not_awaited()


async def test_dispatch_blocked_entirely_without_remote_consent(
    hass: HomeAssistant,
):
    """Ohne Remote-Control-Consent routet KEIN Frame zu einem Apply —
    der Gate sitzt am Dispatch-Eingang zusätzlich zu den _apply_*."""
    coord = _routing_coordinator(
        hass,
        [{CONF_DEVICE_ID: "d1", CONF_DEVICE_TYPE: "heating"}],
        options=NO_CONSENT,
    )
    await coord._handle_ws_message(
        {"type": "telemetry", "device_id": "d1", "data": {"is_on": True}}
    )
    coord._apply_device_state.assert_not_awaited()


async def test_dispatch_ping_updates_liveness_clock(hass: HomeAssistant):
    """Jeder empfangene Frame (auch ping) frischt die SSE-Liveness-Uhr
    auf — Grundlage des Hold-Loop-Stale-Bails."""
    coord = _routing_coordinator(hass, [])
    coord.state.last_sse_event_at = 0.0
    await coord._handle_ws_message({"type": "ping"})
    assert coord.state.last_sse_event_at > 0.0


# ════════════════════════════════════════════════════════════════════
# E. `_counter_delta` — High-Water-Mark-Δ aus total_increasing-Zählern
#    (Fix: Solar-/PV-kWh 10–15 % zu hoch trotz Mirror-Dedup #62)
# ════════════════════════════════════════════════════════════════════


def test_counter_delta_first_read_has_no_delta():
    """Ohne Baseline (erster Read nach Restart) gibt es nichts zu buchen,
    aber die Baseline wird auf den aktuellen Stand gesetzt."""
    delta, next_prev = _counter_delta(123.4, None)
    assert delta is None
    assert next_prev == 123.4


def test_counter_delta_normal_increase():
    delta, next_prev = _counter_delta(100.5, 100.0)
    assert delta == pytest.approx(0.5)
    assert next_prev == 100.5


def test_counter_delta_noise_dip_holds_baseline():
    """Kernfall des Fixes: ein kleiner Rückschritt (Sensor-Rauschen) darf
    KEINEN negativen Δ buchen UND die Baseline NICHT regressieren —
    sonst zählt der Re-Anstieg ein zweites Mal."""
    delta, next_prev = _counter_delta(99.99, 100.0)
    assert delta == 0.0
    # High-Water-Mark gehalten, nicht auf 99.99 gefallen.
    assert next_prev == 100.0


def test_counter_delta_noise_dip_then_recovery_counts_once():
    """End-to-End des Über-Zähler-Bugs: 100 → 99.99 (Dip) → 100.10.
    Korrekt summiert sind das +0.10, NICHT +0.11 (Dip + voller
    Re-Anstieg)."""
    prev = 100.0
    d1, prev = _counter_delta(99.99, prev)   # Dip
    d2, prev = _counter_delta(100.10, prev)  # Erholung + echte Produktion
    assert d1 == 0.0
    assert d2 == pytest.approx(0.10)
    assert (d1 + d2) == pytest.approx(0.10)


def test_counter_delta_large_drop_is_meter_reset():
    """Ein großer Sturz (< 90 % des letzten Werts) ist ein echter
    Zähler-Reset: kein Δ buchen, aber neu baseline-n, damit der nächste
    Tick wieder ab `current` zählt (kein Spike vom Re-Anstieg)."""
    delta, next_prev = _counter_delta(2.0, 100.0)
    assert delta == 0.0
    assert next_prev == 2.0
    # Folge-Tick zählt wieder normal ab dem Reset-Punkt.
    d2, _ = _counter_delta(2.5, next_prev)
    assert d2 == pytest.approx(0.5)
