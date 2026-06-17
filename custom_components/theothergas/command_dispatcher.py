"""Command dispatch + hold-enforcement for the Crowdergy coordinator.

Extracted from coordinator.py (#50 god-file split): every `_apply_*`
handler, the SSE `_handle_ws_message` dispatch, the charge-mode and
entity-control hold loops, and their state-match helpers live here as a
mixin. `CrowdergyCoordinator` inherits it, so `self` is the coordinator
at runtime — methods are unchanged and stay testable against a
coordinator instance (`test_hold_loops_and_eviction.py`).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

# aiohttp + aiohttp_client raus seit FEAT-5 Phase B (2026-06-09) —
# Stream-Reader nutzt sie drüben in sse_client.py.
import httpx

from .const import (
    OPT_CONSENT_REMOTE_CONTROL,
    CONF_ENTITY_BATTERY_MODE,
    CONF_VALUE_BATTERY_MODE_ACTIVE,
    CONF_VALUE_BATTERY_MODE_PASSIVE,
    CONF_ENTITY_BATTERY_POWER_SETPOINT,
    CONF_BATTERY_SETPOINT_INVERT_SIGN,
    CONF_DEVICE_ID,
    CONF_DEVICE_TYPE,
    CONF_DEVICES,
    CONF_ENTITY_CHARGE_MODE,
    CONF_ENTITY_CONTROL,
    CONF_ENTITY_CONTROL_HOLD,
    CONF_ENTITY_VORLAUF_SETPOINT,
    CONF_VALUE_OFF,
    CONF_VALUE_ON,
    CONF_ENTITY_COOL_CONTROL,
    CONF_VALUE_COOL_ON,
    CONF_VALUE_COOL_OFF,
    ENTITY_CONTROL_HOLD_AUTO,
    ENTITY_CONTROL_HOLD_NEVER,
    HOLD_INITIAL_DELAY,
    HOLD_POLL_INTERVAL,
    CHARGE_MODE_HOLD_INITIAL_DELAY,
    CHARGE_MODE_HOLD_INTERVAL,
    SSE_STALE_THRESHOLD_S,
)
from .preset_spec import PRESET_VALUE_SLOTS



_LOGGER = logging.getLogger(__name__)


def _merge_preset_value_map(
    device: dict[str, Any], value_map: dict | None
) -> dict[str, Any]:
    """#40: merge die value-Slots eines Vendor-Presets in einen Device-
    Record. Default-DENY: nur Keys aus `PRESET_VALUE_SLOTS` werden
    übernommen, verbatim als String. Die value_map-Keys SIND die
    Connector-CONF-Keys (die Backend-`api_name`-Übersetzung ist
    backend-seitig) → direkter Merge, kein Mapping nötig. Gibt denselben
    dict zurück, wenn sich nichts ändert (Caller skippt dann den Reload)."""
    changes = {
        key: str(val)
        for key, val in (value_map or {}).items()
        if key in PRESET_VALUE_SLOTS and str(val) != str(device.get(key, ""))
    }
    if not changes:
        return device
    return {**device, **changes}


class CommandDispatcherMixin:
    """Mixin carrying the SSE command-apply + hold-enforcement methods
    for :class:`CrowdergyCoordinator`."""

    async def _self_heal_holds(self, device_ids: list[str]) -> None:
        """Hold-loop self-heal. `_apply_device_state` only fires on
        is_on *transitions* coming through the SSE WS — but the MPC
        tick re-decides the same state every 5 minutes, and HA
        restarts wipe live hold tasks. Without this guard, a device
        that should be ON loses its periodic re-write the moment a
        transition is missed (warmwasser case 2026-05-22: hysteresis
        53→60, MPC writes "60" once, register reverts, next MPC tick
        is also "60" → no transition → no rewrite → device stays
        off). Walks each Crowdergize-active device once per tick and
        restarts the hold task if it's gone.

        CN-1 (2026-06-11): früher Skip ohne Remote-Control-Consent —
        das zentrale Gate in `_apply_device_state` würde ohnehin
        greifen, aber so läuft kein nutzloses Diffing und das Log
        bleibt eindeutig.

        CN-3 (2026-06-11): früher Skip bei SSE-Staleness — derselbe
        Threshold wie der Bail im `_hold_loop`. Vorher restartete der
        Self-Heal den Hold jeden 30-s-Tick und schrieb den gecachten
        Zustand zurück, womit ein User-Eingriff während eines Backend-
        Outages alle ~30 s überschrieben wurde („User regains manual
        control" galt nur bis zum nächsten Tick).
        """
        if not self._remote_control_allowed("hold-self-heal"):
            return
        staleness = time.time() - self.state.last_sse_event_at
        if staleness > SSE_STALE_THRESHOLD_S:
            _LOGGER.debug(
                "hold self-heal: skipping — Crowdergy SSE silent for "
                "%.1fs (> %ds), user keeps manual control",
                staleness, SSE_STALE_THRESHOLD_S,
            )
            return
        for device_id in device_ids:
            if not self.state.active_state.get(device_id, False):
                continue
            if device_id not in self.state.on_state:
                continue  # never commanded — don't synthesise a write
            task = self.state.hold_tasks.get(device_id)
            if task is not None and not task.done():
                continue
            dev = next(
                (d for d in self.devices if d.get(CONF_DEVICE_ID) == device_id),
                None,
            )
            if dev is None:
                continue
            # Hold-Mode: auto (Default) und always rewriten, never skipt.
            mode = (
                dev.get(CONF_ENTITY_CONTROL_HOLD)
                or ENTITY_CONTROL_HOLD_AUTO
            )
            if mode == ENTITY_CONTROL_HOLD_NEVER:
                continue  # user opted out of periodic rewriting
            await self._apply_device_state(
                device_id, self.state.on_state[device_id]
            )

    async def async_post_command(
        self, device_id: str, payload: dict[str, Any]
    ) -> bool:
        """POST a command body to the backend in the schema it expects.

        Payload must include `action` plus the action-specific fields the
        backend's DeviceCommand schema demands, e.g.:
          {"action": "toggle_active", "is_active": True}
        (P3 2026-06-11: das frühere `set_soc_min`-Beispiel entfernt —
        die Action existiert backend-seitig nicht mehr; einziger
        Connector-Caller ist der Crowdergize-Switch mit toggle_active.)
        """
        try:
            response = await self._authenticated_request(
                "POST",
                f"/api/v1/devices/{device_id}/commands",
                json=payload,
            )
            response.raise_for_status()
            return True
        except (httpx.HTTPStatusError, httpx.RequestError) as err:
            _LOGGER.error("Command failed: %s", err)
            return False

    # ── Inbound SSE: commands pushed from the Crowdergy backend ────────────
    #
    # SSE-Stream-Reader extrahiert nach `sse_client.py` (FEAT-5 Phase B,
    # 2026-06-09). Coordinator hält nur noch SSEClient-Instanz +
    # Consumer-Task; das Stream-Reading + Reconnect-Backoff + Auth-
    # Refresh-Trigger leben drüben. Vorteile: testbar ohne Apply-Stack,
    # 512-Slot-Queue als Backpressure gegen hängende Apply-Calls.

    async def _handle_ws_message(self, data: dict[str, Any]) -> None:
        # Update liveness clock for the charge-mode hold-loop kill
        # switch — any received message (ping included) counts as
        # "Crowdergy still talking to us". The 15-s server ping is
        # the most common message type when nothing else is changing.
        self.state.last_sse_event_at = time.time()
        # Box-Consent-Gate (Phase 4): ohne Remote-Control-Consent werden
        # eingehende Steuer-Frames komplett ignoriert — keine Entity-
        # Writes, keine Hold-Loops, kein Cache-Sync. Liveness-Clock oben
        # bleibt aktuell (der Stream selbst ist kein Steuer-Akt).
        if not self._consent(OPT_CONSENT_REMOTE_CONTROL):
            return
        # #40: device_update — ein iOS-`apply-preset` hat ein Vendor-Preset
        # auf dieses Gerät gestempelt + gebroadcastet. Wir ziehen den VOLLEN
        # value_map aus dem Store und mergen ihn in die lokale Device-Config
        # (insb. die non-1:1-Batterie-Modi, die das Backend bewusst nicht
        # schreibt). Bewusst hinter dem Remote-Control-Consent-Gate — ohne
        # Steuer-Consent steuert der Connector ohnehin nicht.
        if data.get("type") == "device_update":
            await self._apply_preset_from_backend(data.get("device_id"))
            return
        # Telemetry mirror frames carry device-level state changes the user
        # may have driven from the iOS app. We watch two flags:
        #   - is_active (Crowdergize consent) — just update the local cache
        #     so the HA switch entity reflects the right value.
        #   - is_on (current on/off state) — drive the actual entity write
        #     using the user-configured entity_control + value_on/value_off.
        if data.get("type") == "telemetry":
            device_id = data.get("device_id")
            payload = data.get("data") or {}
            if not device_id:
                return
            # v3.9.2 (2026-06-04): manuelle App-Befehle kommen als
            # telemetry-Frames mit `is_on`/`is_active`. Ohne dieses Log
            # war „kommt manueller Befehl durch?"-Diagnose blind —
            # einzige Signatur war ein späterer state-resync-Reapply.
            # 2026-06-09 (Cluster D): von WARNING → DEBUG runter — bei
            # 10+ Devices und 30s Heartbeat-Echos lief das auf ~1200
            # WARNINGs/Stunde und wuchs home-assistant.log unnötig.
            # Aktivierung via `logger:` config oder ENV `LOG_LEVEL=DEBUG`.
            if any(k in payload for k in ("is_active", "is_on", "cool_on", "vorlauf_setpoint_c")):
                _LOGGER.debug(
                    "Crowdergy SSE telemetry frame: device=%s is_active=%s is_on=%s cool_on=%s vorlauf=%s",
                    device_id,
                    payload.get("is_active"),
                    payload.get("is_on"),
                    payload.get("cool_on"),
                    payload.get("vorlauf_setpoint_c"),
                )
            if "is_active" in payload:
                new_value = bool(payload["is_active"])
                if self.state.active_state.get(device_id) != new_value:
                    self.state.active_state[device_id] = new_value
                    self._sync_field_into_data(device_id, "is_active", new_value)
                    # E-2 / XR-1 (2026-06-11): der frühere Wallbox-
                    # Snapshot/Restore-Pfad (`charge_mode_value_crowdergy`)
                    # ist entfernt. Das Backend hat die Spalte am
                    # 2026-06-03 gedroppt (Migration 20260603_0005) und
                    # sendet den Key nie mehr → Snapshot wurde nie
                    # befüllt, Restore fand nie etwas. BEWUSSTE
                    # Entscheidung: der Pre-AI-Lademodus wird bei AI-OFF
                    # NICHT mehr automatisch restauriert. Wallbox-AI-OFF
                    # räumt nur noch den Charge-Mode-Hold ab (siehe
                    # unten), danach gehört die Wallbox dem User.
                    if new_value:
                        # Crowdergize on → force an entity_control write
                        # + start the hold loop using the currently
                        # cached desired state. Without this, the user
                        # sees no HA-side action until the backend
                        # publishes the FIRST is_on *transition*, which
                        # never happens when the device's current state
                        # already matches the solver's decision (common
                        # case: WW physically off, solver decides off,
                        # backend's "publish only on change" guard
                        # suppresses the SSE frame). Result: value_off
                        # never written, hold loop never started.
                        desired_on = bool(self.state.on_state.get(device_id, False))
                        await self._apply_device_state(device_id, desired_on)
                    else:
                        # Crowdergize off → schreibe einen sicheren
                        # Default-State, statt den letzten AI-Zustand
                        # hängen zu lassen. User-Erwartung 2026-05-30:
                        # SG-Ready bleibt nicht auf „Erhöht", Batterie
                        # nicht auf force-charge. Wallbox-Spezialpfad
                        # (snapshot/restore) lief schon oben.
                        dev = next(
                            (d for d in self.devices
                             if d.get(CONF_DEVICE_ID) == device_id),
                            None,
                        )
                        dev_type = (dev or {}).get(CONF_DEVICE_TYPE, "")
                        if dev_type == "battery":
                            # v3.8.0 (2026-06-02) AI-off Battery-Übergabe:
                            # Lademodus auf "Passiv" schreiben → HA-
                            # Automation lässt den Setpoint los, WR
                            # übernimmt PV-Native-Priority.
                            self._cancel_charge_mode_hold(device_id)
                            await self._apply_battery_setpoint(
                                device_id, "passive", 0.0,
                            )
                        elif dev_type == "wallbox":
                            # AI-OFF-Cleanup für die Wallbox: nur den
                            # Charge-Mode-Hold-Loop abräumen, damit der
                            # User die Wallbox manuell übernehmen kann.
                            # Der Solver-Lademodus wird NICHT auf einen
                            # früheren Wert zurückgesetzt (E-2 / XR-1,
                            # 2026-06-11: der Restore-Pfad ist tot —
                            # Backend sendet `charge_mode_value_crowdergy`
                            # seit 2026-06-03 nicht mehr).
                            self._cancel_charge_mode_hold(device_id)
                        elif dev_type in ("heating", "warmwater", "aircon", "generic"):
                            # entity_control auf value_off — sorgt
                            # dafür dass das Gerät definitiv stoppt.
                            # _apply_device_state startet danach den
                            # hold-loop; den wollen wir hier NICHT
                            # → direkt nach dem write die hold-loop
                            # canceln (siehe unten).
                            await self._apply_device_state(device_id, False)
                            # Cooling-side auf off bringen falls cool_on
                            # war (SG-Ready / climate / dedizierter
                            # Kühl-Switch — alle drei Pfade über
                            # _apply_cool_state).
                            try:
                                await self._apply_cool_state(device_id, False)
                            except Exception:
                                # Cool-state ist optional; nicht
                                # blockieren wenn Helper bei diesem
                                # device nichts schreiben kann.
                                pass
                        # Cancel hold-loop NACH dem explicit write,
                        # sodass der letzte write das Letzte ist was
                        # wir auf das entity_control schreiben — danach
                        # ist das Gerät dem User überlassen.
                        self._cancel_hold(device_id)
            # v3.6.4: cool_on VOR is_on verarbeiten — `_cool_state`
            # muss gesetzt sein bevor `_apply_device_state(False)` läuft,
            # sonst sieht der Skip-Guard auf der heat-side `_cool_state`
            # als False und schreibt "off" auf die climate-Entity die
            # gerade vom cool-Pfad mit "cool" gefüllt wurde.
            if "cool_on" in payload:
                new_cool = bool(payload["cool_on"])
                if self.state.cool_state.get(device_id) != new_cool:
                    self.state.cool_state[device_id] = new_cool
                    self._sync_field_into_data(device_id, "cool_on", new_cool)
                    await self._apply_cool_state(device_id, new_cool)
            if "is_on" in payload:
                new_on = bool(payload["is_on"])
                if self.state.on_state.get(device_id) != new_on:
                    self.state.on_state[device_id] = new_on
                    self._sync_field_into_data(device_id, "is_on", new_on)
                    await self._apply_device_state(device_id, new_on)
            # Phase 2b (2026-06-02): Vorlauf-Setpoint von modulierenden
            # Heizungen. Backend sendet pro Solver-Tick °C; wir dispatchen
            # via climate.set_temperature gegen die User-konfigurierte
            # entity_vorlauf_setpoint. Skip wenn keine Entity gemapped
            # ist (User darf vorerst weiter on/off-only fahren).
            if "vorlauf_setpoint_c" in payload:
                try:
                    setpoint_val = float(payload["vorlauf_setpoint_c"])
                except (ValueError, TypeError):
                    setpoint_val = None
                if setpoint_val is not None:
                    await self._apply_vorlauf_setpoint(device_id, setpoint_val)
            return

        # `command` frames are mostly handled via the telemetry mirror
        # above. The one that still needs explicit handling is
        # `set_charge_mode` (wallbox Lademodus), which writes a
        # dedicated entity_charge_mode select entity outside the
        # generic entity_control mechanism.
        if data.get("type") == "command":
            action = data.get("action")
            device_id = data.get("device_id")
            value = data.get("value")
            # Frame-Logging: alle action-relevanten Keys (außer
            # Boilerplate). Vorgängerversion printete nur `value`, was
            # für Battery-Frames immer None ist (Battery liest `mode` +
            # `setpoint_kw` aus separaten Keys) — das war misleading bei
            # „kommt Steuerung durch"-Diagnose. v3.9.2 (2026-06-04).
            payload_keys = {
                k: v for k, v in data.items()
                if k not in ("type",)
            }
            # P3 (2026-06-11): WARNING → DEBUG. Command-Frames sind
            # Normalbetrieb (jeder Solver-Tick); als WARNING wuchs
            # home-assistant.log unnötig — analog zum Telemetry-Frame-
            # Log (Cluster D 2026-06-09).
            _LOGGER.debug("Crowdergy SSE command frame: %s", payload_keys)
            if action == "set_charge_mode" and device_id:
                # Dispatch nach Device-Typ:
                #   * battery  → Phase 3 Option D: Lademodus-Select +
                #                Power-Setpoint-Number
                #   * wallbox  → Lademodus-Select (Mode-String-Dispatch)
                dev = next(
                    (d for d in self.devices
                     if d.get(CONF_DEVICE_ID) == device_id),
                    None,
                )
                # P3 (2026-06-11): unbekannte device_id fiel vorher in
                # den Wallbox-Else-Zweig (Cancel-Hold / Apply für ein
                # Gerät das es lokal nicht gibt). Frame ignorieren.
                if dev is None:
                    _LOGGER.debug(
                        "set_charge_mode: unknown device_id %s — "
                        "ignoring command frame", device_id,
                    )
                    return
                dev_type = dev.get(CONF_DEVICE_TYPE, "")
                if dev_type == "battery":
                    mode = data.get("mode") or "passive"
                    setpoint_kw = data.get("setpoint_kw")
                    await self._apply_battery_setpoint(
                        device_id, mode,
                        float(setpoint_kw) if setpoint_kw is not None else 0.0,
                    )
                else:
                    # Wallbox: bisher unverändert.
                    if value is None:
                        self._cancel_charge_mode_hold(device_id)
                    else:
                        await self._apply_charge_mode(device_id, str(value))

    async def _apply_preset_from_backend(self, device_id: str | None) -> None:
        """#40 (Folge von Backend #38): auf ein `device_update` das vom
        iOS-Picker gestempelte Vendor-Preset aus dem Store ziehen und den
        VOLLEN value_map in die lokale Device-Config mergen. Das Backend
        schreibt nur die 1:1-Spalten (charge_mode_value_*); die
        non-1:1-Batterie-Modi (`value_battery_mode_active/passive`) löst der
        Connector hier auf. NIE stumm — der iOS-Apply IST die
        User-Bestätigung. Failures werden geloggt, crashen nie die
        Dispatch-Loop.
        """
        if not device_id:
            return
        dev = next(
            (d for d in self.devices if d.get(CONF_DEVICE_ID) == device_id),
            None,
        )
        if dev is None:
            _LOGGER.debug(
                "device_update: unbekannte device_id %s — ignoriert", device_id
            )
            return
        try:
            resp = await self._authenticated_request(
                "GET", f"/api/v1/devices/{device_id}"
            )
            if resp.status_code >= 400:
                _LOGGER.warning(
                    "device_update: GET device %s → %s", device_id, resp.status_code
                )
                return
            body = resp.json()
            vendor = body.get("vendor")
            model = body.get("model")
            if not vendor or not model:
                # Kein Profil gesetzt (oder zurückgesetzt) — nichts anzuwenden.
                return
            dev_type = dev.get(CONF_DEVICE_TYPE) or body.get("type")
            lookup = await self._authenticated_request(
                "GET", "/api/v1/crowd-presets/lookup",
                params={"device_type": dev_type, "vendor": vendor},
            )
            if lookup.status_code >= 400:
                _LOGGER.warning(
                    "device_update: preset-lookup (%s/%s) → %s",
                    dev_type, vendor, lookup.status_code,
                )
                return
            presets = (lookup.json() or {}).get("presets", [])
            preset = next((p for p in presets if p.get("model") == model), None)
            if preset is None:
                _LOGGER.warning(
                    "device_update: Preset %s/%s/%s nicht im Lookup",
                    dev_type, vendor, model,
                )
                return
            merged = _merge_preset_value_map(dev, preset.get("value_map"))
            if merged is dev:
                _LOGGER.debug(
                    "device_update: value_map für %s unverändert — kein Reload",
                    device_id,
                )
                return
            new_devices = [
                merged if d.get(CONF_DEVICE_ID) == device_id else d
                for d in self.entry.data.get(CONF_DEVICES, [])
            ]
            self.hass.config_entries.async_update_entry(
                self.entry,
                data={**self.entry.data, CONF_DEVICES: new_devices},
            )
            _LOGGER.info(
                "device_update: Preset %s/%s auf Gerät %s angewendet — Reload",
                vendor, model, device_id,
            )
            # Reload ENTKOPPELT planen: ein direktes `async_reload` würde die
            # SSE-Consumer-Task (in der dieser Handler läuft) mitten im Lauf
            # canceln. `async_create_task` läuft erst nach der Rückkehr.
            self.hass.async_create_task(
                self.hass.config_entries.async_reload(self.entry.entry_id)
            )
        except Exception:  # noqa: BLE001 — Dispatch-Loop nie crashen lassen
            _LOGGER.exception(
                "device_update: Preset-Apply für %s fehlgeschlagen", device_id
            )

    async def _apply_charge_mode(
        self, device_id: str, mode: str, *, schedule_hold: bool = True
    ) -> None:
        """Write the device's configured entity_charge_mode entity.

        Two domains supported:
          * `select` / `input_select` — `select.select_option` with the
            mode string as the option. Used by wallbox Lademodus and by
            batteries whose mode-entity happens to be a select.
          * `number` / `input_number` — `number.set_value` with the
            mode string parsed as a float. Used by batteries whose
            mode-entity is a number taking e.g. +5000 / 0 / -5000 W.

        `schedule_hold=True` (the default for fresh SSE commands)
        replaces any existing hold-loop task for this device with one
        that re-writes the same value every `CHARGE_MODE_HOLD_INTERVAL`
        seconds. The loop is what keeps inverters that reset their
        mode after ~15 s of silence (Kostal, BYD, some Sungrow firmware)
        actually obeying Crowdergy's commanded mode. The hold loop
        itself calls this method with `schedule_hold=False` so it
        doesn't keep reseeding its own task.

        CN-1: zentrales Consent-Gate — alle Caller (SSE-Dispatch,
        Snapshot/Restore, Charge-Mode-Hold-Loop) sind cloud-getrieben,
        siehe `_remote_control_allowed`. Gate deckt damit auch den
        Hold-Start (`_start_charge_mode_hold`) ab, der nur von hier
        aus erreichbar ist.
        """
        if not self._remote_control_allowed("_apply_charge_mode"):
            return
        dev = next(
            (d for d in self.devices if d.get(CONF_DEVICE_ID) == device_id),
            None,
        )
        if dev is None:
            _LOGGER.warning(
                "set_charge_mode: no matching device config for %s",
                device_id,
            )
            return
        entity_id = dev.get(CONF_ENTITY_CHARGE_MODE, "") or ""
        if not entity_id:
            _LOGGER.warning(
                "set_charge_mode: device %s has no entity_charge_mode "
                "configured", device_id,
            )
            return
        domain = entity_id.split(".", 1)[0]
        # First write (fresh SSE command) keeps the WARNING so the
        # user sees Crowdergy acting; the hold-loop rewrites drop to
        # DEBUG so a healthy 15-s cadence doesn't flood the HA log.
        log_level = logging.WARNING if schedule_hold else logging.DEBUG
        _LOGGER.log(log_level, "set_charge_mode: %s → %s", entity_id, mode)
        # Update the held value BEFORE the actual write so any in-
        # flight old hold-tick that wakes up between our cancel and
        # its next read still sees the new value rather than re-
        # writing the previous one. Race-safe even if `_start_charge_
        # mode_hold` below loses the cancel race.
        if schedule_hold:
            self.state.held_charge_mode[device_id] = mode
        try:
            if domain in ("select", "input_select"):
                await self.hass.services.async_call(
                    domain, "select_option",
                    {"entity_id": entity_id, "option": mode},
                    blocking=True,
                )
            elif domain in ("number", "input_number"):
                try:
                    value = float(mode)
                except (TypeError, ValueError):
                    _LOGGER.warning(
                        "set_charge_mode: '%s' is not numeric, can't write "
                        "to %s entity %s", mode, domain, entity_id,
                    )
                    return
                await self.hass.services.async_call(
                    domain, "set_value",
                    {"entity_id": entity_id, "value": value},
                    blocking=True,
                )
            else:
                _LOGGER.warning(
                    "set_charge_mode: entity %s domain %s not supported "
                    "(expected select / number)", entity_id, domain,
                )
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("set_charge_mode service call failed: %s", err)

        if schedule_hold:
            self._start_charge_mode_hold(device_id)

    def _start_charge_mode_hold(self, device_id: str) -> None:
        """Replace any existing charge_mode hold task for this device.
        Idempotent — cancelling a not-yet-started task is a no-op.

        Respektiert ENTITY_CONTROL_HOLD: auto/always → hold-loop läuft,
        never → User-Opt-Out, kein Re-Write nach dem ersten Apply.
        """
        prev = self.state.charge_mode_hold_tasks.pop(device_id, None)
        if prev is not None and not prev.done():
            prev.cancel()
        dev = next(
            (d for d in self.devices if d.get(CONF_DEVICE_ID) == device_id),
            None,
        )
        if dev is None:
            return
        mode = dev.get(CONF_ENTITY_CONTROL_HOLD) or ENTITY_CONTROL_HOLD_AUTO
        if mode == ENTITY_CONTROL_HOLD_NEVER:
            return
        # 2026-06-09 (Cluster B Connector Review #11): von
        # async_create_task auf async_create_background_task — Symmetrie
        # mit _hold_tasks und damit HA's Shutdown-Tracker beide
        # Hold-Loop-Familien gleich behandelt.
        self.state.charge_mode_hold_tasks[device_id] = self.hass.async_create_background_task(
            self._charge_mode_hold_loop(device_id),
            name=f"theothergas_charge_mode_hold_{device_id}",
        )

    def _cancel_charge_mode_hold(self, device_id: str) -> None:
        """Stop the per-device charge_mode hold and drop the cached
        held value. Called on `passive` commands (worker signals
        inverter should follow its own logic) and on device removal.
        """
        self.state.held_charge_mode.pop(device_id, None)
        task = self.state.charge_mode_hold_tasks.pop(device_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def _charge_mode_hold_loop(self, device_id: str) -> None:
        """Re-write the last commanded charge_mode every
        CHARGE_MODE_HOLD_INTERVAL seconds. Bails when the held value
        is cleared (cancel via `_cancel_charge_mode_hold` — called on
        `passive` from the worker, on wallbox AI-off, on device
        removal, and on coordinator shutdown) OR when the SSE channel
        has been silent past
        SSE_STALE_THRESHOLD_S (Crowdergy backend / network unreachable
        → the inverter's native logic regains control rather than
        being stuck on the last command forever).

        Deliberately does NOT gate on `_active_state`. The user can
        manually tap Wallbox modes (Aus / An / Solar) while AI is off,
        and those still need to hold against firmwares that auto-
        revert. AI-off scenarios are handled by the explicit cancel
        paths above, not by an inline is_active check.
        """
        try:
            await asyncio.sleep(CHARGE_MODE_HOLD_INITIAL_DELAY)
            while True:
                mode = self.state.held_charge_mode.get(device_id)
                if mode is None:
                    return
                # Liveness check: if Crowdergy isn't talking to us,
                # stop holding so the user's inverter can take over.
                # When SSE reconnects, the next MPC tick re-establishes
                # the hold via a fresh `set_charge_mode` event.
                staleness = time.time() - self.state.last_sse_event_at
                if staleness > SSE_STALE_THRESHOLD_S:
                    _LOGGER.warning(
                        "charge_mode hold: bailing for %s — Crowdergy "
                        "SSE silent for %.1fs (> %ds). Inverter "
                        "native logic resumes.",
                        device_id, staleness, SSE_STALE_THRESHOLD_S,
                    )
                    self.state.held_charge_mode.pop(device_id, None)
                    return
                await self._apply_charge_mode(
                    device_id, mode, schedule_hold=False
                )
                await asyncio.sleep(CHARGE_MODE_HOLD_INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "charge_mode hold loop for %s crashed", device_id
            )

    def _sync_field_into_data(self, device_id: str, field: str, value: Any) -> None:
        """Mutate `self.data[device_id][field]` and notify CoordinatorEntities.

        The HA switch entities read from `coordinator.data` — we copy the
        dict (DataUpdateCoordinator equality-checks references), update
        the field, and push via `async_set_updated_data` to trigger a
        re-render without waiting for the next refresh tick.
        """
        if self.data is None:
            return
        bucket = self.data.get(device_id)
        if bucket is None or bucket.get(field) == value:
            return
        new_data = dict(self.data)
        new_bucket = dict(bucket)
        new_bucket[field] = value
        new_data[device_id] = new_bucket
        self.async_set_updated_data(new_data)

    async def _apply_device_state(self, device_id: str, on: bool) -> None:
        """Write the user-configured entity_control to value_on / value_off.

        Looks up the device's `entity_control` + `value_on` / `value_off`
        from the config entry, dispatches the right HA service via
        `_write_entity_control`, and kicks off the hold loop that
        guards against auto-revert. No-ops cleanly if anything's
        missing so a partial / new-style config can't crash the
        coordinator.

        CN-1 (2026-06-11): zentrales Remote-Control-Consent-Gate.
        Jeder Caller ist cloud-getrieben (SSE-Dispatch, State-Resync,
        Hold-Self-Heal) — siehe `_remote_control_allowed`-Docstring.
        """
        if not self._remote_control_allowed("_apply_device_state"):
            return
        dev = next(
            (d for d in self.devices if d.get(CONF_DEVICE_ID) == device_id),
            None,
        )
        if dev is None:
            return
        entity_id = dev.get(CONF_ENTITY_CONTROL, "") or ""
        if not entity_id:
            _LOGGER.debug(
                "Device %s has no entity_control mapped — Crowdergy can't switch it yet",
                device_id,
            )
            return
        domain = entity_id.split(".", 1)[0]
        raw_value = dev.get(CONF_VALUE_ON if on else CONF_VALUE_OFF, "")

        # v3.6.4: bei climate.* teilen heat- und cool-Pfad dieselbe
        # Entity. Wenn cool-side gerade live ist (cool_on=True), dann
        # darf die heat-side hier KEIN "off" mehr schreiben — das war
        # der „AC geht nach 2 Sek aus"-Bug: Backend emittiert is_on=False
        # (oft als initial-frame für dieses Device, _on_state war None)
        # nach dem cool-Tick, _apply_device_state(False) findet expected=
        # "off" vs actual="cool" → schreibt "off" → AC aus.
        if (
            domain == "climate"
            and not on
            and self.state.cool_state.get(device_id, False)
        ):
            _LOGGER.debug(
                "skip heat-off write for %s: cool_on=True owns climate entity",
                device_id,
            )
            self._cancel_hold(device_id)
            return

        # v3.6.3: idempotent guard — wenn die Entity schon im commanded
        # state sitzt, nicht nochmal schreiben. Heat-Pumpen tolerieren
        # redundante Service-Calls, aber Split-AC's piepen bei jedem
        # set_hvac_mode-Call. Drift-Repair übernimmt der Hold-Loop nach
        # HOLD_INITIAL_DELAY falls actual doch noch != expected ist.
        expected = self._expected_state_value(raw_value, on, domain)
        actual = self._read_current_state(entity_id)
        if expected is not None and self._states_match(actual, expected, domain):
            self._start_hold(device_id, entity_id, raw_value, domain, on)
            return

        await self._write_entity_control(
            entity_id, domain, raw_value, on, verbose=True,
        )

        # After the initial write, kick off (or replace) the hold loop
        # that handles devices reverting their entity_control value
        # back to a default — Kostal-style auto-reset Modbus registers,
        # some OCPP-wallbox transaction timeouts, etc.
        self._start_hold(device_id, entity_id, raw_value, domain, on)

    async def _apply_cool_state(self, device_id: str, cool_on: bool) -> None:
        """Schreibt den Kühl-State für ein heating-Device. Drei
        Dispatch-Patterns nach Priorität:

          1. Dedicated `entity_cool_control` configured → write
             `value_cool_on` / `value_cool_off` against it (mirror of
             the heating-side _apply_device_state path).
          2. No `entity_cool_control` AND the heating-side
             `entity_control` is a `climate.*` entity → call
             `climate.set_hvac_mode("cool")` or `"off"` against it
             (single HA entity handles both modes).
          3. Otherwise: skip — the device isn't actually wired for
             cooling at the HA layer, log debug and move on.

        CN-1: zentrales Consent-Gate, alle Caller cloud-getrieben
        (SSE-Dispatch, State-Resync) — siehe `_remote_control_allowed`.
        """
        if not self._remote_control_allowed("_apply_cool_state"):
            return
        dev = next(
            (d for d in self.devices if d.get(CONF_DEVICE_ID) == device_id),
            None,
        )
        if dev is None:
            return
        cool_entity = dev.get(CONF_ENTITY_COOL_CONTROL, "") or ""
        if cool_entity:
            domain = cool_entity.split(".", 1)[0]
            raw_value = dev.get(
                CONF_VALUE_COOL_ON if cool_on else CONF_VALUE_COOL_OFF, ""
            )
            await self._write_entity_control(
                cool_entity, domain, raw_value, cool_on, verbose=True,
            )
            return
        # Climate-domain single-entity fallback: heating and cooling
        # share the same `entity_control`, and the connector
        # translates the mode via `climate.set_hvac_mode`.
        heat_entity = dev.get(CONF_ENTITY_CONTROL, "") or ""
        if heat_entity.startswith("climate."):
            # v3.6.4: symmetrisch zur heat-side: cool→off darf die
            # entity nicht überschreiben wenn heat-side gerade live ist
            # (is_on=True). Würde sonst die heat-Mode beim cool_on=False
            # SSE-Frame nach "off" zwingen statt heat sauber zu
            # übernehmen.
            if (
                not cool_on
                and self.state.on_state.get(device_id, False)
            ):
                _LOGGER.debug(
                    "skip cool-off write for %s: is_on=True owns climate entity",
                    device_id,
                )
                return
            mode = "cool" if cool_on else "off"
            # v3.6.3: bei climate.* teilen Heat- und Cool-Seite dieselbe
            # Entity. Der Heat-Side-Hold-Loop hätte sonst seinen
            # raw_value (= value_off bei is_on=False) alle 30s gegen
            # unser "cool" gesetzt — Endlos-Pingpong + Piep-Bestätigung
            # bei jeder Klimaanlage. Beim Cool-Flip kapern wir die
            # Mode-Hoheit: bisherigen Heat-Hold canceln, kein neuer
            # Cool-Hold (set_hvac_mode ist sticky, Drift-Repair übernimmt
            # die nächste Solver-Tick falls nötig). Beim Cool-Off-Flip
            # startet `_apply_device_state` automatisch wieder eine
            # Heat-Hold falls is_on=True.
            self._cancel_hold(device_id)
            # v3.5.1: war fälschlich auf .warning gesetzt → tauchte als
            # „Fehler" in HA's Protokoll auf obwohl es eine normale
            # Operation ist. Jetzt auf .debug damit's nur bei aktivem
            # Debug-Logging sichtbar wird.
            _LOGGER.debug(
                "set_hvac_mode: %s → %s (cool side)", heat_entity, mode,
            )
            # v3.6.3: idempotent — wenn der climate-Mode schon stimmt,
            # nicht nochmal schreiben (sonst piept die AC bei jedem
            # SSE-Echo / Re-Tick). Drift-Repair kommt von der nächsten
            # backend-decision, nicht von redundanten Re-Asserts hier.
            actual = self._read_current_state(heat_entity)
            if actual == mode:
                return
            try:
                await self.hass.services.async_call(
                    "climate", "set_hvac_mode",
                    {"entity_id": heat_entity, "hvac_mode": mode},
                    blocking=True,
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception(
                    "set_hvac_mode failed for %s: %s", heat_entity, err,
                )
            return
        _LOGGER.debug(
            "Device %s flipped cool_on but has no cooling entity "
            "(entity_cool_control empty, entity_control not climate.*)",
            device_id,
        )

    async def _apply_battery_setpoint(
        self, device_id: str, mode: str, setpoint_kw: float
    ) -> None:
        """Phase 3 Option D (2026-06-02): Battery-Dispatch via 2 HA-
        Entities — Lademodus-Select (Aktiv/Passiv) + Power-Setpoint-
        Number (signed Watts, + = laden, − = entladen).

        Dispatch-Pfade:
          * mode == "passive"  → schreibe value_battery_mode_passive an
            entity_battery_mode. KEIN Power-Write — HA-Automation hält
            den Setpoint nicht mehr, WR übernimmt PV-Native-Priority.
          * mode != "passive"  → schreibe Setpoint (in Watts) an
            entity_battery_power_setpoint, dann value_battery_mode_active
            an entity_battery_mode. HA-Automation hält den Setpoint.

        Vorzeichen: solver-intern + = laden. Wenn der User
        `battery_setpoint_invert_sign` aktiviert hat (umgekehrter WR),
        multipliziert der Connector mit −1.

        Idempotent: skip write wenn HA-State bereits gleich (Mode-
        Select + Setpoint-Toleranz ±10 W).

        CN-1: zentrales Consent-Gate, alle Caller cloud-getrieben
        (SSE command-Frame, AI-off-Übergabe aus dem SSE-Dispatch) —
        siehe `_remote_control_allowed`.
        """
        if not self._remote_control_allowed("_apply_battery_setpoint"):
            return
        dev = next(
            (d for d in self.devices if d.get(CONF_DEVICE_ID) == device_id),
            None,
        )
        if dev is None:
            return
        mode_entity = dev.get(CONF_ENTITY_BATTERY_MODE, "") or ""
        active_val = dev.get(CONF_VALUE_BATTERY_MODE_ACTIVE, "") or ""
        passive_val = dev.get(CONF_VALUE_BATTERY_MODE_PASSIVE, "") or ""
        setpoint_entity = dev.get(CONF_ENTITY_BATTERY_POWER_SETPOINT, "") or ""
        invert = bool(dev.get(CONF_BATTERY_SETPOINT_INVERT_SIGN, False))

        if not mode_entity or not active_val or not passive_val:
            _LOGGER.debug(
                "Battery %s: mode-entity / mode-values nicht gemapped — skip",
                device_id,
            )
            return

        if mode == "passive":
            current_mode = self._read_current_state(mode_entity)
            if current_mode != passive_val:
                _LOGGER.info(
                    "Battery %s passive: %s → %s",
                    device_id, current_mode, passive_val,
                )
                try:
                    await self.hass.services.async_call(
                        mode_entity.split(".", 1)[0], "select_option",
                        {"entity_id": mode_entity, "option": passive_val},
                        blocking=True,
                    )
                except Exception as err:  # noqa: BLE001
                    _LOGGER.exception(
                        "Battery passive-write failed for %s: %s",
                        mode_entity, err,
                    )
            return

        # Aktiv-Pfad: Setpoint berechnen + schreiben, dann Mode aktivieren.
        target_w = float(setpoint_kw) * 1000.0
        if invert:
            target_w = -target_w
        if setpoint_entity:
            actual_raw = self._read_current_state(setpoint_entity)
            should_write = True
            if actual_raw is not None:
                try:
                    if abs(float(actual_raw) - target_w) <= 10.0:
                        should_write = False
                except (ValueError, TypeError):
                    pass
            if should_write:
                _LOGGER.info(
                    "Battery %s setpoint %s → %.0f W (mode=%s, invert=%s)",
                    device_id, setpoint_entity, target_w, mode, invert,
                )
                try:
                    await self.hass.services.async_call(
                        setpoint_entity.split(".", 1)[0], "set_value",
                        {"entity_id": setpoint_entity, "value": target_w},
                        blocking=True,
                    )
                except Exception as err:  # noqa: BLE001
                    _LOGGER.error(
                        "Battery setpoint-write FAILED for %s: %s — "
                        "skipping subsequent mode-switch to %s damit "
                        "der Inverter nicht mit alt/null-Setpoint auf "
                        "Aktiv läuft (Cluster B Connector 2026-06-09).",
                        setpoint_entity, err, active_val,
                    )
                    return

        # Mode-Select zuletzt setzen — sodass der Setpoint schon
        # geschrieben ist wenn HA's Automation auf "Aktiv" reagiert.
        current_mode = self._read_current_state(mode_entity)
        if current_mode != active_val:
            _LOGGER.info(
                "Battery %s mode: %s → %s",
                device_id, current_mode, active_val,
            )
            try:
                await self.hass.services.async_call(
                    mode_entity.split(".", 1)[0], "select_option",
                    {"entity_id": mode_entity, "option": active_val},
                    blocking=True,
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception(
                    "Battery active-mode-write failed for %s: %s",
                    mode_entity, err,
                )

    async def _apply_vorlauf_setpoint(
        self, device_id: str, temperature_c: float
    ) -> None:
        """Phase 2b (2026-06-02): write the solver's Vorlauf-Setpoint
        in °C to the user-configured entity_vorlauf_setpoint. Dispatch-
        Pfad nach Domain:
          * `climate.*` → `climate.set_temperature(temperature=°C)`
          * `number.*` / `input_number.*` → `set_value(value=°C)`
          * sonst → skip (User-Misconfig oder Phase-3-Entity-Domain
            die wir noch nicht unterstützen).

        Fallback-Kaskade:
          1. `entity_vorlauf_setpoint` explicit gemapped (Manuell-Mode-
             User, SG-Ready-WP mit eigenem number/input_number) → nutzen.
          2. Leer + `entity_control` ist climate.* (Climate-Mode-User,
             v3.7.1+) → entity_control hat `set_temperature` eingebaut,
             dispatch direkt dagegen.
          3. Sonst (Manuell-Mode ohne expliziten Setpoint) → skip,
             Heizung läuft weiter binary on/off.

        CN-1: zentrales Consent-Gate, einziger Caller ist der SSE-
        Dispatch (cloud-getrieben) — siehe `_remote_control_allowed`."""
        if not self._remote_control_allowed("_apply_vorlauf_setpoint"):
            return
        dev = next(
            (d for d in self.devices if d.get(CONF_DEVICE_ID) == device_id),
            None,
        )
        if dev is None:
            return
        entity_id = dev.get(CONF_ENTITY_VORLAUF_SETPOINT, "") or ""
        if not entity_id:
            # Climate-Mode-Fallback: entity_control ist die climate.*-
            # Entity, die kennt `set_temperature` selbst.
            control_entity = dev.get(CONF_ENTITY_CONTROL, "") or ""
            if control_entity.startswith("climate."):
                entity_id = control_entity
        if not entity_id:
            return
        domain = entity_id.split(".", 1)[0]
        # Idempotenz: wenn HA bereits den exakten Wert reportet (≤ 0.05 °C
        # Toleranz für Float-Rauschen), kein Re-Write — climate.set_
        # temperature ist auf manchen WPs ebenso piepend wie set_hvac_mode.
        #
        # CN-4 (2026-06-11): bei climate.* steht im state der HVAC-Mode
        # („heat"), der Ziel-Setpoint sitzt im Attribut `temperature`.
        # Vorher lief `float("heat")` in den except-Pfad → Guard war
        # wirkungslos und `climate.set_temperature` feuerte bei JEDEM
        # Solver-Tick (~5 min) auch bei identischem Wert.
        if domain == "climate":
            state = self.hass.states.get(entity_id)
            actual_raw = (
                None if state is None
                else state.attributes.get("temperature")
            )
        else:
            actual_raw = self._read_current_state(entity_id)
        if actual_raw is not None:
            try:
                if abs(float(actual_raw) - temperature_c) <= 0.05:
                    return
            except (ValueError, TypeError):
                pass
        _LOGGER.debug(
            "set_vorlauf_setpoint: %s → %.1f °C", entity_id, temperature_c,
        )
        try:
            if domain == "climate":
                await self.hass.services.async_call(
                    "climate", "set_temperature",
                    {"entity_id": entity_id, "temperature": temperature_c},
                    blocking=True,
                )
            elif domain in ("number", "input_number"):
                await self.hass.services.async_call(
                    domain, "set_value",
                    {"entity_id": entity_id, "value": temperature_c},
                    blocking=True,
                )
            else:
                _LOGGER.warning(
                    "Device %s entity_vorlauf_setpoint=%s domain %s nicht "
                    "unterstützt — bitte climate / number / input_number "
                    "wählen.",
                    device_id, entity_id, domain,
                )
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception(
                "set_vorlauf_setpoint failed for %s: %s", entity_id, err,
            )

    async def _write_entity_control(
        self,
        entity_id: str,
        domain: str,
        raw_value: Any,
        on: bool,
        *,
        verbose: bool,
    ) -> None:
        """Single domain-dispatched write to a HA entity_control entity.
        Shared by the apply-on-transition path (`_apply_device_state`,
        `verbose=True`) and the hold-loop rewrite (`_hold_loop`,
        `verbose=False`).

        `verbose=True` surfaces WARNING-level breadcrumbs for missing
        config / unsupported domains so the user notices a misconfig
        on first toggle. The hold path stays quiet — the same issue
        would otherwise log every HOLD_POLL_INTERVAL.
        """
        try:
            if domain in ("switch", "input_boolean", "light", "fan"):
                # Bool-style entities: on/off is implicit (turn_on /
                # turn_off services). Honour any configured value_on /
                # value_off string only as an override — empty string is
                # fine and just maps to the natural on/off semantic.
                if raw_value in ("", None):
                    service = "turn_on" if on else "turn_off"
                else:
                    want_on = str(raw_value).lower() in (
                        "on", "true", "1", "yes", "an",
                    )
                    service = "turn_on" if want_on else "turn_off"
                await self.hass.services.async_call(
                    domain, service, {"entity_id": entity_id}, blocking=True,
                )
                return
            # All non-binary domains below need an explicit value.
            if raw_value in ("", None):
                if verbose:
                    _LOGGER.warning(
                        "%s has no value_%s configured — skipping HA write",
                        entity_id, "on" if on else "off",
                    )
                return
            if domain in ("number", "input_number"):
                await self.hass.services.async_call(
                    domain, "set_value",
                    {"entity_id": entity_id, "value": float(raw_value)},
                    blocking=True,
                )
            elif domain in ("select", "input_select"):
                await self.hass.services.async_call(
                    domain, "select_option",
                    {"entity_id": entity_id, "option": str(raw_value)},
                    blocking=True,
                )
            elif domain == "climate":
                await self.hass.services.async_call(
                    domain, "set_hvac_mode",
                    {"entity_id": entity_id, "hvac_mode": str(raw_value)},
                    blocking=True,
                )
            elif domain == "water_heater":
                # HA's water_heater integration uses set_operation_mode
                # with operation_mode parameter (vs climate's hvac_mode).
                # Typical operation_modes are "eco" / "performance" /
                # "electric" / "off" — the user maps these via
                # value_on / value_off in the device_values step just
                # like with climate.
                await self.hass.services.async_call(
                    domain, "set_operation_mode",
                    {"entity_id": entity_id, "operation_mode": str(raw_value)},
                    blocking=True,
                )
            elif verbose:
                _LOGGER.warning(
                    "Unsupported entity_control domain %s for %s",
                    domain, entity_id,
                )
        except (ValueError, TypeError) as err:
            if verbose:
                _LOGGER.warning(
                    "Bad value_%s=%r for %s (%s) — %s",
                    "on" if on else "off", raw_value, entity_id, domain, err,
                )
        except Exception as err:  # noqa: BLE001
            if verbose:
                _LOGGER.exception("entity_control write failed: %s", err)
            else:
                _LOGGER.warning("hold re-apply failed for %s: %s", entity_id, err)

    # ── entity_control hold loop ────────────────────────────────────────

    def _start_hold(
        self,
        device_id: str,
        entity_id: str,
        raw_value: Any,
        domain: str,
        on: bool,
    ) -> None:
        """Start (or replace) a hold-loop for this device.

        Cancels any existing loop for the device first — the latest
        `_apply_device_state` is the source of truth.
        """
        existing = self.state.hold_tasks.pop(device_id, None)
        if existing is not None and not existing.done():
            existing.cancel()

        dev = next(
            (d for d in self.devices if d.get(CONF_DEVICE_ID) == device_id),
            None,
        )
        if dev is None:
            return
        # Hold-Mode-Semantik (v3.6.3+):
        #   * NEVER  → kein Loop, sofortiges Return.
        #   * AUTO   → smart: nur re-write bei Drift (Default). Stoppt
        #     piepende Re-Asserts bei climate.* Devices.
        #   * ALWAYS → blind re-write jeden Tick. Für "schwarze" Devices
        #     wo der HA-State nicht zuverlässig reflektiert was am Gerät
        #     passiert (Kostal Modbus Auto-Reset etc.).
        mode = dev.get(CONF_ENTITY_CONTROL_HOLD) or ENTITY_CONTROL_HOLD_AUTO
        if mode == ENTITY_CONTROL_HOLD_NEVER:
            return

        # Cluster B Connector (2026-06-09, Review #11): Background-Task
        # bei HA registrieren statt naked asyncio.create_task. Vorher
        # wusste HA's Task-Tracker nichts von diesen Tasks → bei
        # Shutdown-Edge-Cases (Exception in async_shutdown vorm Cancel)
        # blieben sie als Orphan-Tasks im Loop. SSE/Heartbeat nutzen
        # das Pattern schon — Symmetrie war hier nur noch nicht da.
        self.state.hold_tasks[device_id] = self.hass.async_create_background_task(
            self._hold_loop(device_id, entity_id, raw_value, domain, on, mode),
            name=f"theothergas_entity_hold_{device_id}",
        )

    def _cancel_hold(self, device_id: str) -> None:
        task = self.state.hold_tasks.pop(device_id, None)
        if task is not None and not task.done():
            task.cancel()

    def forget_device(self, device_id: str) -> None:
        """Prune every per-device bookkeeping dict for a removed
        device. Called from `async_remove_config_entry_device` so
        stale keys don't accumulate across the lifetime of one
        coordinator instance (`async_remove_config_entry_device`
        schedules a reload since CN-8, this keeps the window until
        the reload lands clean). Idempotent — missing keys are
        silently ignored."""
        self._cancel_hold(device_id)
        self._cancel_charge_mode_hold(device_id)
        # CN-8 (2026-06-11): auch die aktive Geräteliste + den
        # Entity-Index mitpflegen — vorher PATCHte der Coordinator
        # das gelöschte Device bis zum nächsten Reload weiter
        # (404-Spam im Telemetry-Loop).
        self.devices = [
            d for d in self.devices
            if d.get(CONF_DEVICE_ID) != device_id
        ]
        self._entity_to_devices = {
            entity_id: remaining
            for entity_id, ids in self._entity_to_devices.items()
            if (remaining := [i for i in ids if i != device_id])
        }
        for d in (
            self.state.active_state,
            self.state.on_state,
            # P3 (2026-06-11): cool_state fehlte im Pruning.
            self.state.cool_state,
            self._prev_energy_kwh,
            self._prev_energy_kwh_discharged,
            self._last_sent_payload,
            self._last_send_at,
            self._last_mirror_at,
            self._last_sent_hash,
        ):
            d.pop(device_id, None)

    async def _hold_loop(
        self,
        device_id: str,
        entity_id: str,
        raw_value: Any,
        domain: str,
        on: bool,
        hold_mode: str,
    ) -> None:
        """Keep entity_control sticking to its commanded value.

        Hold-Mode-Semantik (v3.6.3+):
          * `NEVER`  → kein Loop (in `_start_hold` gefiltert)
          * `AUTO`   → smart: nur re-write wenn `actual != expected`
            (Drift-Repair). Default seit v3.6.3. Verhindert blindes
            Bombardieren von Devices die einen state korrekt halten —
            insbesondere `climate.set_hvac_mode` piept bei jeder
            Klimaanlage, redundante Re-Asserts müssen weg.
          * `ALWAYS` → blind re-write jeden Tick. Für „schwarze"
            Devices wo der HA-state nicht zuverlässig reflektiert was
            wirklich am Gerät passiert (Kostal Modbus etc. — Register
            auto-reset ohne dass HA es als state-change sieht).

        Bails out if Crowdergize gets switched off (the `_cancel_hold`
        path covers that) or on coordinator shutdown.
        """
        try:
            # Initial delay gives the apply call's effect time to
            # propagate before the first rewrite (avoids a duplicate
            # service call back-to-back).
            await asyncio.sleep(HOLD_INITIAL_DELAY)
            while True:
                if not self.state.active_state.get(device_id, False):
                    return
                # Cluster B Connector (2026-06-09): SSE-Stale-Bail
                # mirror'd vom `_charge_mode_hold_loop`. Vorher schrieb
                # diese Loop blind weiter, auch wenn Crowdergy gar
                # nicht mehr antwortet → User-Manuelle-Änderung an
                # Heizung/WP/AC während 2h SSE-Outage wurde im 30s-
                # Tick wieder überschrieben. Bei plugged WP/SG-Ready
                # potentiell gefährlich.
                staleness = time.time() - self.state.last_sse_event_at
                if staleness > SSE_STALE_THRESHOLD_S:
                    _LOGGER.warning(
                        "entity_control hold: bailing for %s — Crowdergy "
                        "SSE silent for %.1fs (> %ds). User regains "
                        "manual control.",
                        device_id, staleness, SSE_STALE_THRESHOLD_S,
                    )
                    return
                expected = self._expected_state_value(raw_value, on, domain)
                actual = self._read_current_state(entity_id)
                if (
                    hold_mode == ENTITY_CONTROL_HOLD_AUTO
                    and expected is not None
                    and self._states_match(actual, expected, domain)
                ):
                    # AUTO + state stimmt → skip. Spart Service-Calls
                    # und Piep-Bestätigungen bei AC.
                    await asyncio.sleep(HOLD_POLL_INTERVAL)
                    continue
                if (
                    actual is not None
                    and expected is not None
                    and not self._states_match(actual, expected, domain)
                ):
                    _LOGGER.info(
                        "hold: %s reverted (%r → %r), re-writing",
                        entity_id, expected, actual,
                    )
                await self._write_entity_control(
                    entity_id, domain, raw_value, on, verbose=False,
                )
                await asyncio.sleep(HOLD_POLL_INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _LOGGER.exception("hold loop for %s crashed", device_id)

    @staticmethod
    def _states_match(
        actual: Any, expected: Any, domain: str
    ) -> bool:
        """Ist-State vs. erwarteter Wert — domain-bewusst (CN-6,
        2026-06-11). HA normalisiert number-/input_number-States auf
        Float-Repräsentation ("60.0"), während `value_on`/`value_off`
        meist als "60" konfiguriert sind. Der frühere String-Vergleich
        meldete dadurch nicht existenten Drift und der AUTO-Hold
        schrieb alle 30 s grundlos nach (Muster: `_read_is_on_state`
        vergleicht number-Domains längst numerisch).
        """
        if actual is None or expected is None:
            return False
        if domain in ("number", "input_number"):
            try:
                return float(actual) == float(expected)
            except (TypeError, ValueError):
                pass
        return str(actual) == str(expected)

    def _expected_state_value(
        self, raw_value: Any, on: bool, domain: str
    ) -> str | None:
        """Normalise the value we'd compare an entity's current state
        against. Returns a string (HA states are strings) or None
        wenn wir's nicht entscheiden können — dann re-write die
        Hold-Loop blind im 'always' Modus (es gab früher einen
        'auto' Modus der bei None passiv blieb; collapse zu 'always'
        seit v1.20.0, also kein Branching mehr nötig).
        """
        if domain in ("switch", "input_boolean", "light", "fan"):
            return "on" if on else "off"
        if raw_value in ("", None):
            return None
        return str(raw_value)

    def _read_current_state(self, entity_id: str) -> str | None:
        """Read the entity's current state as a string, or None if HA
        has no state or it's unknown / unavailable.
        """
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        if state.state in ("unknown", "unavailable"):
            return None
        return state.state
