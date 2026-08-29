"""Wirkungs-Kontrolle #152 (2026-08-29) — steuert Crowdergy ins Leere?

Der Connector prüfte bisher nur, ob SEIN Wert in der Steuer-Entity
steht — nicht, ob er auch WIRKT. Zwei Feldfälle aus einer Session mit
colins Stiebel-WP, beide vorher unsichtbar:

* **Wert außerhalb des Entity-Bereichs.** Heizung mit `value_off = 35`
  gegen eine Entity mit `max 30`: der #135-Clamp schreibt 30, der
  Vergleich erwartete aber weiter 35 → Dauer-Scheindrift → der
  AUTO-Hold deutete die EIGENE Klemmung als manuellen Eingriff (#140)
  und legte das Gerät 2 h still (Prod-Beleg: `local_override_since
  08-29 21:45`). Jetzt wird gegen den geklemmten Wert verglichen, und
  die Klemmung selbst geht als Zustand ans Backend.
* **Gerät fährt einen anderen Sollwert.** Warmwasser: Komfort-Register
  auf 35 geschrieben (steht dort korrekt), die WP fuhr im
  Programmbetrieb durchgehend ihr ECO-Register mit 49,5. Der optionale
  Slot `entity_effective_setpoint` macht die Differenz sichtbar.

Test-Mechanik (Sleep-Patch/_StopHold) gespiegelt aus
`test_safety_bundle.py`.
"""
from __future__ import annotations

import time

from unittest.mock import patch

from homeassistant.core import HomeAssistant

from custom_components.theothergas.config_flow_mapping import (
    _build_device_record,
)
from custom_components.theothergas.const import (
    CONF_DEVICE_ID,
    CONF_DEVICE_TYPE,
    CONF_ENTITY_CHARGE_MODE,
    CONF_ENTITY_CONTROL,
    CONF_ENTITY_CONTROL_HOLD,
    CONF_ENTITY_EFFECTIVE_SETPOINT,
    CONF_VALUE_OFF,
    CONF_VALUE_ON,
    CONTROL_EFFECT_MIN_MISMATCH_S,
    ENTITY_CONTROL_HOLD_AUTO,
)

from .test_safety_bundle import (  # noqa: F401  (Fixtures/Helfer teilen)
    _breaking_sleep,
    _run_until_stopped,
    _SLEEP,
    make_coordinator,
)


def _thermal_device(
    *,
    value_off: str,
    value_on: str = "55",
    entity: str = "number.wp_komfort_ww",
    effective: str = "",
) -> dict:
    dev = {
        CONF_DEVICE_ID: "d1",
        CONF_DEVICE_TYPE: "warmwater",
        CONF_ENTITY_CONTROL: entity,
        CONF_ENTITY_CONTROL_HOLD: ENTITY_CONTROL_HOLD_AUTO,
        CONF_VALUE_ON: value_on,
        CONF_VALUE_OFF: value_off,
    }
    if effective:
        dev[CONF_ENTITY_EFFECTIVE_SETPOINT] = effective
    return dev


# ════════════════════════════════════════════════════════════════════
# (1) Vergleich gegen den GESCHRIEBENEN (geklemmten) Wert
# ════════════════════════════════════════════════════════════════════


async def test_expected_value_is_clamped_to_entity_range(
    hass: HomeAssistant,
):
    """Der Erwartungswert ist der, den die Entity annehmen KONNTE."""
    coord = make_coordinator(hass, [])
    hass.states.async_set("number.hk1", "25", {"min": 5, "max": 30})

    expected = coord._expected_state_value("35", False, "number", "number.hk1")

    assert float(expected) == 30.0


async def test_expected_value_unchanged_when_in_range(hass: HomeAssistant):
    coord = make_coordinator(hass, [])
    hass.states.async_set("number.hk1", "25", {"min": 5, "max": 30})

    assert coord._expected_state_value("28", False, "number", "number.hk1") \
        == "28.0"
    # Modus-Strings laufen unverändert durch (kein Zahlen-Pfad).
    hass.states.async_set("select.modus", "eco")
    assert coord._expected_state_value(
        "eco", False, "select", "select.modus"
    ) == "eco"


async def test_out_of_range_value_does_not_trip_local_override(
    hass: HomeAssistant,
):
    """DIE Regression: der geklemmte Wert steht in der Entity, der
    konfigurierte liegt darüber — das ist KEIN Nutzer-Eingriff, sondern
    unsere eigene Klemmung. Vor dem Fix hat der AUTO-Hold hier das
    Gerät für 2 h stillgelegt (Feld: Heizung 08-29 21:45)."""
    dev = _thermal_device(value_off="35", entity="number.hk1")
    coord = make_coordinator(hass, [dev])
    # Entity hält brav den geklemmten Wert.
    hass.states.async_set("number.hk1", "30.0", {"min": 5, "max": 30})
    coord.state.active_state["d1"] = True
    coord.state.last_sse_event_at = time.time()
    # Letzter eigener Write liegt lange zurück → ohne Fix greift die
    # #140-Übersteuerungs-Erkennung.
    coord.state.last_own_write_at["number.hk1"] = time.time() - 3600

    with patch(_SLEEP, _breaking_sleep(2)):
        await _run_until_stopped(
            coord._hold_loop(
                "d1", "number.hk1", "35", "number", False,
                ENTITY_CONTROL_HOLD_AUTO,
            )
        )

    assert "d1" not in coord.state.local_override_until


async def test_genuine_foreign_drift_still_trips_override(
    hass: HomeAssistant,
):
    """Gegenprobe: ein WIRKLICH fremder Wert (weder konfiguriert noch
    geklemmt) muss weiter als Übersteuerung gelten — der Fix darf #140
    nicht entschärfen."""
    dev = _thermal_device(value_off="20", entity="number.hk1")
    coord = make_coordinator(hass, [dev])
    hass.states.async_set("number.hk1", "27.0", {"min": 5, "max": 30})
    coord.state.active_state["d1"] = True
    coord.state.last_sse_event_at = time.time()
    coord.state.last_own_write_at["number.hk1"] = time.time() - 3600

    with patch(_SLEEP, _breaking_sleep(2)):
        await _run_until_stopped(
            coord._hold_loop(
                "d1", "number.hk1", "20", "number", False,
                ENTITY_CONTROL_HOLD_AUTO,
            )
        )

    assert "d1" in coord.state.local_override_until


# ════════════════════════════════════════════════════════════════════
# (2) control_value_rejected — Konfigurationsfehler als Zustand
# ════════════════════════════════════════════════════════════════════


async def test_clamped_write_marks_value_rejected(hass: HomeAssistant):
    coord = make_coordinator(hass, [])
    hass.states.async_set("number.hk1", "25", {"min": 5, "max": 30})

    coord._clamp_write_value("number.hk1", "number", 35.0, device_id="d1")
    assert "d1" in coord.state.value_rejected_devices

    # Ein Write im Bereich löst den Zustand wieder auf.
    coord._clamp_write_value("number.hk1", "number", 28.0, device_id="d1")
    assert "d1" not in coord.state.value_rejected_devices


async def test_expected_value_lookup_does_not_mark_rejected(
    hass: HomeAssistant,
):
    """Der Vergleichs-Pfad klemmt still: er darf weder loggen noch den
    Zustand setzen — sonst meldet jeder Hold-Tick einen Fehler."""
    coord = make_coordinator(hass, [])
    hass.states.async_set("number.hk1", "25", {"min": 5, "max": 30})

    coord._expected_state_value("35", False, "number", "number.hk1")

    assert coord.state.value_rejected_devices == {}


# ════════════════════════════════════════════════════════════════════
# (3) Gefahrener vs. geschriebener Sollwert
# ════════════════════════════════════════════════════════════════════


async def test_no_effective_slot_means_no_statement(hass: HomeAssistant):
    dev = _thermal_device(value_off="35")
    coord = make_coordinator(hass, [dev])
    coord.state.last_written_value["d1"] = "35.0"

    assert coord._effective_setpoint_mismatch(dev) is False


async def test_never_commanded_means_no_statement(hass: HomeAssistant):
    dev = _thermal_device(value_off="35", effective="sensor.ww_soll")
    coord = make_coordinator(hass, [dev])
    hass.states.async_set("sensor.ww_soll", "49.5")

    assert coord._effective_setpoint_mismatch(dev) is False


async def test_matching_effective_setpoint_clears_clock(
    hass: HomeAssistant,
):
    dev = _thermal_device(value_off="35", effective="sensor.ww_soll")
    coord = make_coordinator(hass, [dev])
    coord.state.last_written_value["d1"] = "35.0"
    coord.state.effective_mismatch_since["d1"] = time.time() - 99999
    hass.states.async_set("sensor.ww_soll", "35.2")  # innerhalb Toleranz

    assert coord._effective_setpoint_mismatch(dev) is False
    assert "d1" not in coord.state.effective_mismatch_since


async def test_mismatch_reported_only_after_it_persists(
    hass: HomeAssistant,
):
    """Der Stiebel-Feldfall: Komfort 35 geschrieben, WP fährt ECO 49,5.
    Gemeldet wird erst, was sich nicht von selbst auflöst — ein Gerät
    darf rampen und verzögert übernehmen."""
    dev = _thermal_device(value_off="35", effective="sensor.ww_soll")
    coord = make_coordinator(hass, [dev])
    coord.state.last_written_value["d1"] = "35.0"
    hass.states.async_set("sensor.ww_soll", "49.5")

    # Erster Tick: Uhr startet, noch kein Befund.
    assert coord._effective_setpoint_mismatch(dev) is False
    assert "d1" in coord.state.effective_mismatch_since

    # Uhr zurückdatieren → Abweichung hält an.
    coord.state.effective_mismatch_since["d1"] = (
        time.time() - CONTROL_EFFECT_MIN_MISMATCH_S - 1
    )
    assert coord._effective_setpoint_mismatch(dev) is True


async def test_unreadable_effective_entity_is_no_finding(
    hass: HomeAssistant,
):
    """Ein toter Sensor darf keinen Steuerfehler melden — und die Uhr
    nicht weiterlaufen lassen."""
    dev = _thermal_device(value_off="35", effective="sensor.ww_soll")
    coord = make_coordinator(hass, [dev])
    coord.state.last_written_value["d1"] = "35.0"
    coord.state.effective_mismatch_since["d1"] = (
        time.time() - CONTROL_EFFECT_MIN_MISMATCH_S - 1
    )
    hass.states.async_set("sensor.ww_soll", "unavailable")

    assert coord._effective_setpoint_mismatch(dev) is False
    assert "d1" not in coord.state.effective_mismatch_since


async def test_mode_devices_compare_as_strings(hass: HomeAssistant):
    """Uniform über die Gerätetypen: bei einem Modus-gesteuerten Gerät
    (Wallbox/Batterie) ist der gefahrene Zustand ein String."""
    dev = {
        CONF_DEVICE_ID: "d1",
        CONF_DEVICE_TYPE: "wallbox",
        CONF_ENTITY_CHARGE_MODE: "select.wallbox_modus",
        CONF_ENTITY_EFFECTIVE_SETPOINT: "select.wallbox_ist_modus",
    }
    coord = make_coordinator(hass, [dev])
    coord.state.last_written_value["d1"] = "Solar Pure Mode"
    hass.states.async_set("select.wallbox_ist_modus", "Standard")
    coord.state.effective_mismatch_since["d1"] = (
        time.time() - CONTROL_EFFECT_MIN_MISMATCH_S - 1
    )

    assert coord._effective_setpoint_mismatch(dev) is True

    hass.states.async_set("select.wallbox_ist_modus", "Solar Pure Mode")
    assert coord._effective_setpoint_mismatch(dev) is False


async def test_readonly_types_make_no_statement(hass: HomeAssistant):
    """Nur steuerbare Typen — ein Solar-Zähler hat keinen kommandierten
    Zustand, gegen den man vergleichen könnte."""
    dev = {
        CONF_DEVICE_ID: "d1",
        CONF_DEVICE_TYPE: "solar",
        CONF_ENTITY_EFFECTIVE_SETPOINT: "sensor.irgendwas",
    }
    coord = make_coordinator(hass, [dev])
    coord.state.last_written_value["d1"] = "35.0"
    hass.states.async_set("sensor.irgendwas", "49.5")

    assert coord._effective_setpoint_mismatch(dev) is False


# ════════════════════════════════════════════════════════════════════
# (4) Rückmeldung + Persistenz
# ════════════════════════════════════════════════════════════════════


async def test_is_on_readback_uses_clamped_value(hass: HomeAssistant):
    """Steht die Entity auf dem geklemmten AUS-Wert, sind wir AUS —
    nicht "unklar". Sonst behält das Backend ewig seinen alten Wert."""
    dev = _thermal_device(value_on="25", value_off="35", entity="number.hk1")
    coord = make_coordinator(hass, [dev])
    hass.states.async_set("number.hk1", "30.0", {"min": 5, "max": 30})

    assert coord._read_is_on_state(dev) is False


async def test_is_on_none_when_on_and_off_collapse(hass: HomeAssistant):
    """Klemmen AN und AUS auf dieselbe Zahl (beide über max), sind die
    beiden Zustände am Gerät nicht mehr unterscheidbar — dann lieber
    keine Aussage als eine erfundene. Das WARUM liefert separat
    `control_value_rejected`."""
    dev = _thermal_device(value_on="55", value_off="35", entity="number.hk1")
    coord = make_coordinator(hass, [dev])
    hass.states.async_set("number.hk1", "30.0", {"min": 5, "max": 30})

    assert coord._read_is_on_state(dev) is None


def test_build_device_record_persists_effective_slot():
    """Die v3.28.0/v3.33.x-Falle: Schema ergänzt, Persistenz vergessen —
    das Feld würde beim Submit still verworfen."""
    record = _build_device_record(
        "dev-1", "warmwater", "Warmwasser",
        {
            CONF_ENTITY_CONTROL: "number.wp_komfort_ww",
            CONF_ENTITY_EFFECTIVE_SETPOINT: "sensor.wp_ww_soll",
        },
    )

    assert record[CONF_ENTITY_EFFECTIVE_SETPOINT] == "sensor.wp_ww_soll"
