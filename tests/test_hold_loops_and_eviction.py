"""Charakterisierungs-/Integrationstests für die Coordinator-Hold-Loops,
das 404/410-Eviction-Gate und die SSE→Apply→Hold-Start-Verdrahtung.

Backlog #21 (FEAT-5 Rest): `test_sync_stack_characterization.py` pinnt
bewusst nur die *Entscheidungs-/Routing-Matrizen* (`_should_send`,
`_apply_battery_setpoint`-Idempotenz, `_handle_ws_message`-Dispatch) und
überlässt „die Loops" explizit einem Folge-Schritt. Diese Datei schließt
genau diese Lücke — die sicherheitskritischen Hintergrund-Mechanismen,
die heute KEIN direkter Test abdeckt:

* `_hold_loop` (entity_control): AUTO-Drift-Repair vs. ALWAYS-Blind-
  Rewrite, der SSE-Stale-Bail (User bekommt bei Backend-Outage die
  manuelle Kontrolle zurück) und der Inactive-Bail.
* `_charge_mode_hold_loop` (Wallbox/Batterie): Rewrite-Kadenz, SSE-Stale-
  Bail inkl. Aufräumen des gehaltenen Werts, Cleared-Bail.
* Das 404/410-Eviction-Gate in `_should_send` (Device in der App
  gelöscht → kein weiterer PATCH).
* Die echte (ungemockte) SSE→`_apply_*`→Hold-Start-Kette — die Routing-
  Matrix testet das mit gemockten Applies; hier laufen die echten
  `_apply_device_state`/`_apply_charge_mode` bis zum Service-Call.

Zweck wie bei der Charakterisierungs-Datei: das HEUTIGE Verhalten
festschreiben (Sicherheitsnetz vor dem geplanten Sync-Stack-Refactor),
kein Wunschverhalten vorschreiben.

Determinismus-Trick: die Loops sind `while True` mit `asyncio.sleep`.
Wir patchen `asyncio.sleep` durch einen zählenden Ersatz, der sofort
zurückkehrt (kein echtes Warten) und nach N Aufrufen `_StopHold` wirft —
eine `BaseException`, die der `except Exception`-Crash-Handler der Loop
NICHT schluckt, sodass sie sauber aus dem awaiteten Coroutine
herausfällt. So beobachten wir exakt einen Tick ohne Timing-Flakiness.
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.theothergas.const import (
    CONF_DEVICE_ID,
    CONF_DEVICE_TYPE,
    CONF_ENTITY_CHARGE_MODE,
    CONF_ENTITY_CONTROL,
    CONF_ENTITY_CONTROL_HOLD,
    DOMAIN,
    ENTITY_CONTROL_HOLD_ALWAYS,
    ENTITY_CONTROL_HOLD_AUTO,
    OPT_CONSENT_REMOTE_CONTROL,
    SSE_STALE_THRESHOLD_S,
)
from custom_components.theothergas.coordinator import CrowdergyCoordinator
from custom_components.theothergas.state_mirror import DeviceStateMirror

_SLEEP = "custom_components.theothergas.coordinator.asyncio.sleep"

NO_CONSENT = {OPT_CONSENT_REMOTE_CONTROL: False}


def make_coordinator(
    hass: HomeAssistant,
    devices: list[dict],
    *,
    options: dict | None = None,
) -> CrowdergyCoordinator:
    """Coordinator ohne DataUpdateCoordinator-Init — gleiches Muster wie
    test_consent_and_guards.make_coordinator (bewusst lokal dupliziert,
    damit die Datei eigenständig bleibt). `data=None`, damit der echte
    `_sync_field_into_data` in der Dispatch-Kette sauber früh aussteigt
    statt `async_set_updated_data` ohne Coordinator-Maschinerie zu rufen.
    """
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=dict(options or {}))
    entry.add_to_hass(hass)
    coord = CrowdergyCoordinator.__new__(CrowdergyCoordinator)
    coord.hass = hass
    coord.entry = entry
    coord.devices = devices
    coord.data = None
    coord.state = DeviceStateMirror()
    coord._consent_denied_logged = set()
    coord._backend_gone_device_ids = set()
    coord._last_sent_payload = {}
    coord._last_send_at = {}
    coord._last_mirror_at = {}
    coord._last_sent_hash = {}
    coord._prev_energy_kwh = {}
    coord._prev_energy_kwh_discharged = {}
    return coord


class _StopHold(BaseException):
    """Sentinel, der die sonst-endlose Hold-Loop deterministisch bricht.
    BaseException (nicht Exception!) — sonst fängt der `except
    Exception`-Crash-Handler der Loop ihn ab und die Loop „verschluckt"
    den Abbruch statt ihn nach oben durchzureichen."""


def _breaking_sleep(max_calls: int):
    """Ersatz für `asyncio.sleep`: zählt Aufrufe, kehrt sofort zurück
    (kein echtes Warten) und wirft beim `max_calls`-ten Aufruf
    `_StopHold`. So stoppt eine schreibende Loop nach genau N Ticks."""
    state = {"n": 0}

    async def _sleep(_delay):
        state["n"] += 1
        if state["n"] >= max_calls:
            raise _StopHold

    return _sleep


async def _run_until_stopped(coro) -> None:
    """Awaitet eine Hold-Loop, schluckt den `_StopHold`-Sentinel. Loops,
    die von selbst (Stale-/Inactive-/Cleared-Bail) zurückkehren, lösen
    ihn nie aus — dann ist das ein normaler Return."""
    try:
        await coro
    except _StopHold:
        pass


def _heating_switch_device(hold: str) -> dict:
    return {
        CONF_DEVICE_ID: "d1",
        CONF_DEVICE_TYPE: "heating",
        CONF_ENTITY_CONTROL: "switch.kessel",
        CONF_ENTITY_CONTROL_HOLD: hold,
    }


# ════════════════════════════════════════════════════════════════════
# A. `_hold_loop` — entity_control-Halte-Schleife
# ════════════════════════════════════════════════════════════════════


async def test_hold_loop_bails_when_sse_stale(hass: HomeAssistant):
    """SSE länger als SSE_STALE_THRESHOLD_S still → die Loop bailt, BEVOR
    sie (trotz ALWAYS) erneut schreibt. Schützt manuelle User-Eingriffe
    während eines Backend-Outages."""
    coord = make_coordinator(hass, [_heating_switch_device(ENTITY_CONTROL_HOLD_ALWAYS)])
    coord.state.active_state["d1"] = True
    coord.state.last_sse_event_at = time.time() - SSE_STALE_THRESHOLD_S - 30
    hass.states.async_set("switch.kessel", "off")
    calls = async_mock_service(hass, "switch", "turn_on")

    with patch(_SLEEP, _breaking_sleep(8)):
        await _run_until_stopped(
            coord._hold_loop("d1", "switch.kessel", "on", "switch", True,
                             ENTITY_CONTROL_HOLD_ALWAYS)
        )

    assert calls == []


async def test_hold_loop_returns_when_device_inactive(hass: HomeAssistant):
    """Crowdergize aus (active_state False) → kein Rewrite, sauberer
    Return (der `_cancel_hold`-Pfad räumt den Task normalerweise ab;
    diese Selbst-Bedingung ist der Backstop)."""
    coord = make_coordinator(hass, [_heating_switch_device(ENTITY_CONTROL_HOLD_ALWAYS)])
    coord.state.active_state["d1"] = False
    coord.state.last_sse_event_at = time.time()
    hass.states.async_set("switch.kessel", "off")
    calls = async_mock_service(hass, "switch", "turn_on")

    with patch(_SLEEP, _breaking_sleep(8)):
        await _run_until_stopped(
            coord._hold_loop("d1", "switch.kessel", "on", "switch", True,
                             ENTITY_CONTROL_HOLD_ALWAYS)
        )

    assert calls == []


async def test_hold_loop_always_rewrites_even_when_state_matches(hass: HomeAssistant):
    """ALWAYS = blind re-write jeden Tick — auch wenn der HA-State bereits
    passt (für „schwarze" Modbus-Geräte, deren HA-State den Hardware-Reset
    nicht reflektiert)."""
    coord = make_coordinator(hass, [_heating_switch_device(ENTITY_CONTROL_HOLD_ALWAYS)])
    coord.state.active_state["d1"] = True
    coord.state.last_sse_event_at = time.time()
    hass.states.async_set("switch.kessel", "on")  # State stimmt bereits
    calls = async_mock_service(hass, "switch", "turn_on")

    with patch(_SLEEP, _breaking_sleep(2)):  # initial-delay + ein Tick
        await _run_until_stopped(
            coord._hold_loop("d1", "switch.kessel", "on", "switch", True,
                             ENTITY_CONTROL_HOLD_ALWAYS)
        )

    assert len(calls) == 1


async def test_hold_loop_auto_skips_rewrite_when_state_matches(hass: HomeAssistant):
    """AUTO + State stimmt → KEIN Service-Call (spart redundante Re-Asserts,
    insb. das Piepen bei jeder climate.set_hvac_mode-Bestätigung)."""
    coord = make_coordinator(hass, [_heating_switch_device(ENTITY_CONTROL_HOLD_AUTO)])
    coord.state.active_state["d1"] = True
    coord.state.last_sse_event_at = time.time()
    hass.states.async_set("switch.kessel", "on")  # == expected("on")
    calls = async_mock_service(hass, "switch", "turn_on")

    with patch(_SLEEP, _breaking_sleep(2)):
        await _run_until_stopped(
            coord._hold_loop("d1", "switch.kessel", "on", "switch", True,
                             ENTITY_CONTROL_HOLD_AUTO)
        )

    assert calls == []


async def test_hold_loop_auto_rewrites_on_drift(hass: HomeAssistant):
    """AUTO + State driftet (off statt on) → Drift-Repair feuert genau
    einen turn_on."""
    coord = make_coordinator(hass, [_heating_switch_device(ENTITY_CONTROL_HOLD_AUTO)])
    coord.state.active_state["d1"] = True
    coord.state.last_sse_event_at = time.time()
    hass.states.async_set("switch.kessel", "off")  # driftet von expected("on")
    calls = async_mock_service(hass, "switch", "turn_on")

    with patch(_SLEEP, _breaking_sleep(2)):
        await _run_until_stopped(
            coord._hold_loop("d1", "switch.kessel", "on", "switch", True,
                             ENTITY_CONTROL_HOLD_AUTO)
        )

    assert len(calls) == 1
    assert calls[0].data["entity_id"] == "switch.kessel"


# ════════════════════════════════════════════════════════════════════
# B. `_charge_mode_hold_loop` — Wallbox/Batterie-Lademodus-Halte-Schleife
# ════════════════════════════════════════════════════════════════════


async def test_charge_mode_hold_loop_bails_and_clears_when_stale(hass: HomeAssistant):
    """SSE stale → Loop bailt UND räumt den gehaltenen Wert ab, damit die
    native Inverter-Logik wieder übernimmt (kein hängender Befehl)."""
    coord = make_coordinator(hass, [])
    coord.state.held_charge_mode["w1"] = "solar"
    coord.state.last_sse_event_at = time.time() - SSE_STALE_THRESHOLD_S - 30
    coord._apply_charge_mode = AsyncMock()

    with patch(_SLEEP, _breaking_sleep(8)):
        await _run_until_stopped(coord._charge_mode_hold_loop("w1"))

    coord._apply_charge_mode.assert_not_awaited()
    assert "w1" not in coord.state.held_charge_mode


async def test_charge_mode_hold_loop_returns_when_held_cleared(hass: HomeAssistant):
    """Ohne gehaltenen Wert (z. B. nach `_cancel_charge_mode_hold`) kehrt
    die Loop sofort zurück — kein Apply."""
    coord = make_coordinator(hass, [])
    coord.state.last_sse_event_at = time.time()
    coord._apply_charge_mode = AsyncMock()

    with patch(_SLEEP, _breaking_sleep(8)):
        await _run_until_stopped(coord._charge_mode_hold_loop("w1"))

    coord._apply_charge_mode.assert_not_awaited()


async def test_charge_mode_hold_loop_rewrites_while_fresh(hass: HomeAssistant):
    """Frische SSE + gehaltener Wert → re-applied der Modus mit
    `schedule_hold=False` (die Loop reseedet ihren eigenen Task nicht)."""
    coord = make_coordinator(hass, [])
    coord.state.held_charge_mode["w1"] = "solar"
    coord.state.last_sse_event_at = time.time()
    coord._apply_charge_mode = AsyncMock()

    with patch(_SLEEP, _breaking_sleep(2)):
        await _run_until_stopped(coord._charge_mode_hold_loop("w1"))

    coord._apply_charge_mode.assert_awaited_once_with("w1", "solar", schedule_hold=False)


# ════════════════════════════════════════════════════════════════════
# C. 404/410-Eviction-Gate in `_should_send`
# ════════════════════════════════════════════════════════════════════


def test_should_send_suppressed_for_backend_gone_device(hass: HomeAssistant):
    """Device in der iOS-App gelöscht (Backend quittiert PATCH mit 404/410,
    `_async_update_data` trägt die ID in `_backend_gone_device_ids` ein) →
    selbst der sonst immer-sendende Erst-Payload wird unterdrückt. Andere
    Devices bleiben unberührt."""
    coord = make_coordinator(hass, [])

    # Sanity: ein unbekanntes Device sendet seinen ersten Payload.
    assert coord._should_send("d1", {"power_kw": 5.0}) is True

    coord._backend_gone_device_ids.add("d1")
    assert coord._should_send("d1", {"power_kw": 5.0}) is False
    assert coord._should_send("d2", {"power_kw": 5.0}) is True


# ════════════════════════════════════════════════════════════════════
# D. Echte SSE→Apply→Hold-Start-Kette (ungemockte _apply_*)
# ════════════════════════════════════════════════════════════════════


async def test_sse_is_on_frame_writes_entity_and_starts_hold(hass: HomeAssistant):
    """Telemetry-Frame mit is_on=True läuft durch den ECHTEN
    `_apply_device_state`: Cache-Update → Service-Call → Hold-Start.
    (`_start_hold` gestubbt, damit kein echter Hintergrund-Task entsteht;
    die Verdrahtung wird über den Call-Assert geprüft.)"""
    coord = make_coordinator(hass, [_heating_switch_device(ENTITY_CONTROL_HOLD_AUTO)])
    coord._start_hold = MagicMock()
    hass.states.async_set("switch.kessel", "off")
    calls = async_mock_service(hass, "switch", "turn_on")

    await coord._handle_ws_message(
        {"type": "telemetry", "device_id": "d1", "data": {"is_on": True}}
    )

    assert len(calls) == 1
    assert coord.state.on_state["d1"] is True
    coord._start_hold.assert_called_once_with("d1", "switch.kessel", "", "switch", True)


async def test_sse_is_on_frame_blocked_without_consent(hass: HomeAssistant):
    """Gegentest: ohne Remote-Control-Consent erreicht derselbe Frame
    weder Service-Call noch Hold-Start (Gate sitzt am Dispatch-Eingang)."""
    coord = make_coordinator(
        hass, [_heating_switch_device(ENTITY_CONTROL_HOLD_AUTO)], options=NO_CONSENT
    )
    coord._start_hold = MagicMock()
    hass.states.async_set("switch.kessel", "off")
    calls = async_mock_service(hass, "switch", "turn_on")

    await coord._handle_ws_message(
        {"type": "telemetry", "device_id": "d1", "data": {"is_on": True}}
    )

    assert calls == []
    coord._start_hold.assert_not_called()


async def test_sse_command_writes_charge_mode_and_starts_hold(hass: HomeAssistant):
    """Wallbox-`set_charge_mode`-Command läuft durch den ECHTEN
    `_apply_charge_mode`: held-Wert gesetzt → select.select_option →
    Hold-Start (gestubbt, Call-Assert)."""
    dev = {
        CONF_DEVICE_ID: "w1",
        CONF_DEVICE_TYPE: "wallbox",
        CONF_ENTITY_CHARGE_MODE: "select.lademodus",
    }
    coord = make_coordinator(hass, [dev])
    coord._start_charge_mode_hold = MagicMock()
    hass.states.async_set("select.lademodus", "Eco")
    calls = async_mock_service(hass, "select", "select_option")

    await coord._handle_ws_message(
        {"type": "command", "action": "set_charge_mode", "device_id": "w1",
         "value": "Power"}
    )

    assert len(calls) == 1
    assert calls[0].data["option"] == "Power"
    assert coord.state.held_charge_mode["w1"] == "Power"
    coord._start_charge_mode_hold.assert_called_once_with("w1")
