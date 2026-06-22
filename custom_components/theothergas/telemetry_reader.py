"""Telemetry read / compose / send-decision helpers for the coordinator.

#21 Phase-C extraction: the per-tick HA-state readers, the payload composer
and the ``_should_send`` gate moved out of ``coordinator.py`` into this mixin,
mirroring the ``command_dispatcher.CommandDispatcherMixin`` split (#50). The
mixin is inherited by :class:`~.coordinator.CrowdergyCoordinator`, so every
``self._read_*`` / ``self._should_send`` call resolves unchanged and the
methods keep operating on the coordinator instance (``self.hass``,
``self.state``, the ``self._last_sent_*`` bookkeeping, ``self._state_cache``).

The auth/HTTP cluster, the lifecycle/loop wiring, the consent gates and
``_async_update_data`` itself stay in ``coordinator.py`` (the tests pin
``coordinator.asyncio`` and import ``_jwt_exp`` as a module attribute). The
module-level send-decision / extra-field constants live here now and are
re-exported from ``coordinator`` so existing ``coordinator.<NAME>`` imports
keep working.
"""
from __future__ import annotations

import json
import time
from typing import Any

from .const import (
    CONF_DEVICE_TYPE,
    CONF_ENTITY_CLIMATE,
    CONF_ENTITY_CHARGE_MODE,
    CONF_ENTITY_CONTROL,
    CONF_ENTITY_COOL_CONTROL,
    CONF_ENTITY_CURRENT_TEMP,
    CONF_ENTITY_ENERGY_DISCHARGED_TOTAL,
    CONF_ENTITY_ENERGY_TOTAL,
    CONF_ENTITY_HC_BATTERY_POWER,
    CONF_ENTITY_HC_GRID_POWER,
    CONF_ENTITY_HC_PV_POWER,
    CONF_ENTITY_POWER,
    CONF_ENTITY_POWER_2,
    CONF_ENTITY_PV_TO_BATTERY_POWER,
    CONF_ENTITY_SOC,
    CONF_ENTITY_VEHICLE_STATUS,
    CONF_ENTITY_VORLAUF_TEMP,
    CONF_SUPPORTS_COOLING,
    CONF_VALUE_COOL_OFF,
    CONF_VALUE_COOL_ON,
    CONF_VALUE_OFF,
    CONF_VALUE_ON,
    CONF_VEHICLE_STATUS_VALUE_ERROR,
    CONF_VEHICLE_STATUS_VALUE_PLUGGED,
    CONF_VEHICLE_STATUS_VALUE_UNPLUGGED,
)


PER_DEVICE_HEARTBEAT_INTERVAL = 90.0
"""Soft-Heartbeat (2026-06-01+, C7): nach 90 s wird ein PATCH gesendet
WENN der payload-Hash sich seit dem letzten Send verändert hat (z.B.
durch klein-rauschende Werte unter SEND_THRESHOLDS). 90 s matched
weiterhin iOS's 120-s tile-freshness threshold für aktive Geräte.

Pre-C7 (vor 2026-06-01) lief das hier als HARD-Floor, der auch
identical-payload-PATCHes alle 90 s rausschickte — auf truly quiet
Geräten (Solar nachts, Wallbox idle, Heizung im Sommer aus) bedeutete
das ~960 unnötige HTTP-Calls/Tag/Gerät. Mit der Hash-Bedingung
fällt das auf den IDENTICAL_HEARTBEAT_INTERVAL-Floor zurück."""

IDENTICAL_HEARTBEAT_INTERVAL = 600.0
"""Hard-Ceiling für payload-identische PATCHes (C7): auch wenn nichts
am Payload changed, mindestens alle 10 min ein PATCH zur Backend-
Cache-Aktualisierung + Self-Healing der near-duplicate-Gate (falls
`_should_send`s in-memory state vom DB-Stand abdriftet).

10 min ist ein Trade-off: lang genug für signifikante HTTP-Reduktion
(~6.7× ggü. 90 s), kurz genug um die hash-dedup-gate self-heilen zu
lassen. Per-Device-Frische auf iOS-Seite kommt NICHT von hier — das
übernimmt der `_device_mirror_loop` mit `PER_DEVICE_MIRROR_INTERVAL`.
Pre-v3.4.3 hat hier ein falscher Kommentar suggeriert dass der 25-s
user-level Heartbeat die device-tiles frisch hält — der refresht aber
nur `connector_last_seen`, nicht das per-Device telemetry-Timestamp."""

# Per-field "changed enough to be worth a row" thresholds. When NO
# field crosses these AND the per-device heartbeat hasn't expired,
# the entire PATCH is skipped. Categorical fields (vehicle_status,
# charge_mode, is_on) trigger on ANY change.
SEND_THRESHOLDS: dict[str, float] = {
    "power_kw": 0.05,         # 50 W
    "soc_percent": 1.0,       # 1 percentage point
    "current_temp_c": 0.3,    # 0.3 °C
}

# ── Solver-only Extra-Field-Registry (v3.3+) ─────────────────────────
#
# Pro Gerätetyp: Liste von (payload_key, conf_key, reader) Tupeln. Pro
# Tick liest der Coordinator jede mappte Entity, packt das Resultat in
# `payload["extra"]`. Backend filtert + validiert serverseitig
# (app/mpc/solver_fields.py) — Single Source of Truth bleibt dort.
#
# Neues Solver-Feld hier hinzufügen → fertig connector-seitig. Sobald
# der Backend-Registry-Eintrag steht, fließt das Feld pro Telemetry-
# Tick durch zum Solver.
#
# `reader` muss eine der Reader-Methoden auf der Coordinator-Klasse
# sein (siehe `_compose_extra`). "temp" → liest °C-Sensoren oder die
# `current_temperature` aus climate-Attributen; "power" → liest einen
# Leistungssensor und normalisiert auf kW (W→kW über das HA-Unit-Attr).
_SOLVER_EXTRA_FIELDS: dict[str, list[tuple[str, str, str]]] = {
    "heating": [
        ("vorlauf_temp_c", CONF_ENTITY_VORLAUF_TEMP, "temp"),
    ],
    "warmwater": [
        # Brauchwasser-WPs liefern oft eine eigene Vorlauf-Temperatur
        # fürs Aufheizen — typisch höher als HK-VL. Backend nutzt das
        # gleiche cop_at_outdoor_temp(t_vorlauf_c=…) Modell auch hier.
        ("vorlauf_temp_c", CONF_ENTITY_VORLAUF_TEMP, "temp"),
    ],
    # Hausverbrauchs-Flow-Sensoren (#42, NICHT solver-gelesen — reine
    # Chart-Eingabe für Backend #41 `GET /users/me/energy/today`). Je
    # Wert wird auf kW normalisiert und als `*_power_kw` im extra-Bag
    # mitgeschickt; die Keys spiegeln 1:1 die Backend-`SOLVER_FIELDS`.
    "solar": [
        ("hc_pv_power_kw", CONF_ENTITY_HC_PV_POWER, "power"),
    ],
    "battery": [
        ("hc_battery_power_kw", CONF_ENTITY_HC_BATTERY_POWER, "power"),
        ("pv_to_battery_power_kw", CONF_ENTITY_PV_TO_BATTERY_POWER, "power"),
    ],
    "grid": [
        ("hc_grid_power_kw", CONF_ENTITY_HC_GRID_POWER, "power"),
    ],
    # Andere Gerätetypen können ihre Solver-only-Felder hier
    # anhängen ohne den eigentlichen `_async_update_data`-Loop
    # anfassen zu müssen.
}

# Entity slots a single device's per-tick readers may touch (#56). Used by
# `_prefetch_device_states` to snapshot each mapped entity exactly once; the
# device's `_SOLVER_EXTRA_FIELDS` sensors are added on top per device type.
_PREFETCH_SLOT_KEYS: tuple[str, ...] = (
    CONF_ENTITY_POWER,
    CONF_ENTITY_POWER_2,
    CONF_ENTITY_SOC,
    CONF_ENTITY_VEHICLE_STATUS,
    CONF_ENTITY_CHARGE_MODE,
    CONF_ENTITY_CURRENT_TEMP,
    CONF_ENTITY_CLIMATE,  # aircon current-temp fallback
    CONF_ENTITY_ENERGY_TOTAL,
    CONF_ENTITY_ENERGY_DISCHARGED_TOTAL,
    CONF_ENTITY_CONTROL,
    CONF_ENTITY_COOL_CONTROL,
)


class TelemetryReaderMixin:
    """Per-tick HA-state readers + payload compose/decide for
    :class:`~.coordinator.CrowdergyCoordinator` (#21 Phase-C). Pure read/
    compose logic — no auth, no asyncio, no network; operates on ``self``
    (the coordinator instance) via inheritance."""

    def _get_state(self, entity_id: str) -> Any:
        """Read an HA state, preferring the per-tick prefetch snapshot (#56).

        `_async_update_data` snapshots each device's mapped entity states
        once (`_prefetch_device_states`) into `self._state_cache` for the
        duration of that device's synchronous read phase, so the several
        readers that reference the SAME entity (is_on + cool_on share
        entity_control; current_temp + an extra sensor may coincide) hit
        `hass.states` once instead of 2-3×. Outside that window the cache is
        None, so every other caller (hold loops, dispatch) reads live.
        """
        cache = getattr(self, "_state_cache", None)
        if cache is not None and entity_id in cache:
            return cache[entity_id]
        return self.hass.states.get(entity_id)

    def _prefetch_device_states(self, dev: dict[str, Any]) -> dict[str, Any]:
        """Snapshot every entity this device's readers may touch this tick
        (#56) — the mapped slots plus its `_SOLVER_EXTRA_FIELDS` sensors —
        each fetched from `hass.states` exactly once."""
        ids: set[str] = set()
        for key in _PREFETCH_SLOT_KEYS:
            eid = dev.get(key, "")
            if eid:
                ids.add(eid)
        for _payload_key, conf_key, _reader in _SOLVER_EXTRA_FIELDS.get(
            dev.get(CONF_DEVICE_TYPE, ""), []
        ):
            eid = dev.get(conf_key, "")
            if eid:
                ids.add(eid)
        return {eid: self.hass.states.get(eid) for eid in ids}

    def _read_entity_state(self, entity_id: str) -> Any:
        if not entity_id:
            return None
        state = self._get_state(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return state.state

    def _read_temp_c(self, entity_id: str) -> Any:
        """Ist-Temperatur lesen. Bei climate.* / water_heater.* steht
        im state ein Mode-String (z.B. 'heat' / 'eco') und die echte
        Temperatur sitzt im Attribut `current_temperature`. Für
        sensor-/number-Entities Fallback auf den State.
        """
        if not entity_id:
            return None
        domain = entity_id.split(".", 1)[0]
        if domain in ("climate", "water_heater"):
            state = self._get_state(entity_id)
            if state is None:
                return None
            attr = state.attributes.get("current_temperature")
            if attr is None:
                return None
            try:
                return float(attr)
            except (ValueError, TypeError):
                return None
        return self._read_entity_state(entity_id)

    @staticmethod
    def _payload_hash(payload: dict[str, Any]) -> int:
        """Stable content-hash für payload-dedup (C7). `json.dumps` mit
        sort_keys + default=str für mixed-type Stabilität; built-in
        hash() ist OK weil wir nur identity-vs-difference brauchen,
        keine kryptografische Eigenschaft."""
        return hash(json.dumps(payload, sort_keys=True, default=str))

    def _should_send(self, device_id: str, payload: dict[str, Any]) -> bool:
        """Decide whether the just-computed payload differs enough
        from the last sent one to be worth a new telemetry row.

        Returns True if any of:
          * No previous payload exists yet for this device (first send).
          * `IDENTICAL_HEARTBEAT_INTERVAL` (Hard-Ceiling 10 min) seit
            letztem Send (Backend-Cache + Self-Healing der near-dup-Gate).
          * `PER_DEVICE_HEARTBEAT_INTERVAL` (Soft-Heartbeat 90 s) seit
            letztem Send UND payload-Hash unterscheidet sich
            (klein-rauschende Sub-Threshold-Werte).
          * A numeric field crossed its SEND_THRESHOLDS magnitude.
          * A categorical field (vehicle_status / charge_mode / is_on)
            differs at all from the last sent value.
          * `energy_kwh_delta` carries a positive value (any energy
            since last send is worth recording).
        """
        # v3.26.0: Device wurde vom Backend mit 404/410 quittiert
        # (User hat es in der iOS-App gelöscht). Kein weiterer PATCH
        # bis HA-Restart bzw. Config-Reload.
        if device_id in self._backend_gone_device_ids:
            return False
        prev = self._last_sent_payload.get(device_id)
        if prev is None:
            return True
        age = time.time() - self._last_send_at.get(device_id, 0.0)
        # Hard ceiling — Backend-Cache + Self-Healing der near-dup-Gate.
        if age >= IDENTICAL_HEARTBEAT_INTERVAL:
            return True
        # Any non-zero energy Δ (signed for storage devices, positive
        # otherwise) is reason enough to land a row — every kWh
        # matters for the chart totals.
        if abs(payload.get("energy_kwh_delta") or 0.0) > 0:
            return True
        for key, threshold in SEND_THRESHOLDS.items():
            cur, old = payload.get(key), prev.get(key)
            if cur is None and old is None:
                continue
            if cur is None or old is None:
                return True   # presence flipped
            if abs(cur - old) >= threshold:
                return True
        for key in ("vehicle_status", "charge_mode", "is_on", "cool_on"):
            if payload.get(key) != prev.get(key):
                return True
        # Soft heartbeat NUR wenn der payload-Hash sich vom letzten
        # Send unterscheidet — sonst hat der 90s-Tick nichts Neues zu
        # erzählen und wir warten auf den Hard-Ceiling. Spart auf
        # truly-quiet Geräten ~6.7× HTTP-Calls.
        if age >= PER_DEVICE_HEARTBEAT_INTERVAL:
            if self._payload_hash(payload) != self._last_sent_hash.get(device_id):
                return True
        return False

    def _read_energy_kwh(self, entity_id: str) -> float | None:
        """Read a `total_increasing` HA energy sensor as kWh.

        Most integrations report in kWh directly, but a few (Shelly
        EM in default mode, some Modbus bridges) expose the lifetime
        counter in Wh — the raw value would be 1000× too high and
        the iOS-side display would scream "MWh consumed today" on a
        sub-1-kWh tick. Read `unit_of_measurement` from the state's
        attributes and normalise.
        """
        if not entity_id:
            return None
        state = self._get_state(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        try:
            value = float(state.state)
        except (ValueError, TypeError):
            return None
        unit = (state.attributes.get("unit_of_measurement") or "").strip().lower()
        if unit in ("wh", "w·h", "watt-hours", "watthours"):
            return value / 1000.0
        if unit in ("mwh", "megawatt-hours"):
            return value * 1000.0
        # Default assume kWh — matches HA's recommended state_class
        # for energy sensors and the user-confirmed setup here.
        return value

    def _read_power_kw(self, entity_id: str) -> float | None:
        if not entity_id:
            return None
        state = self._get_state(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        try:
            value = float(state.state)
        except (ValueError, TypeError):
            return None
        unit = state.attributes.get("unit_of_measurement", "").lower()
        if unit == "w":
            return value / 1000.0
        return value

    def _compose_extra(self, dev: dict[str, Any]) -> dict[str, Any]:
        """Read this device's registered `telemetry.extra` sensors into a
        flat bag. Driven by `_SOLVER_EXTRA_FIELDS` (per device type) so a
        new extra field is one registry line + one Backend `SolverField`
        — the `_async_update_data` loop never changes.

        Readers: "temp" → °C (sensor or climate `current_temperature`),
        "power" → kW (W→kW normalised). Non-numeric / unavailable reads
        are skipped so the bag only carries live values. Empty dict when
        nothing maps — caller drops `extra` entirely then.
        """
        extra_payload: dict[str, Any] = {}
        for payload_key, conf_key, reader in _SOLVER_EXTRA_FIELDS.get(
            dev.get(CONF_DEVICE_TYPE, ""), []
        ):
            entity_id = dev.get(conf_key, "")
            if not entity_id:
                continue
            if reader == "power":
                value = self._read_power_kw(entity_id)
            elif reader == "temp":
                value = self._read_temp_c(entity_id)
            else:
                value = self._read_entity_state(entity_id)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                extra_payload[payload_key] = float(value)
        return extra_payload

    def _read_string(self, entity_id: str) -> str | None:
        """Read an entity state as the raw `state.state` string.

        C4 (2026-06-01): docstring previously claimed a friendly_value
        fallback, but the code never read attributes. The raw state IS
        the right thing — friendly_value would have masked the raw
        token the user's HA Frontend translates per locale, which
        would silently break our downstream value-matching (e.g.
        vehicle_status mapping). Aligned docstring to reality.
        """
        if not entity_id:
            return None
        state = self._get_state(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        text = str(state.state)
        return text if text else None

    def _normalised_vehicle_status(
        self, dev: dict[str, Any], raw: str | None
    ) -> str | None:
        """Translate a wallbox's vehicle-status sensor reading into one
        of the normalised values the backend / iOS expects:
        'plugged' / 'unplugged' / 'error'.

        Each mapping field is treated as a COMMA-SEPARATED list — most
        wallboxes have multiple states that semantically mean the same
        thing (e.g. "Connected, Charging, Paused" all = plugged). The
        user can comma-list them in a single field; the connector
        matches case-insensitively after stripping whitespace.

        Returns:
          - the matching normalised value when raw matches a mapping,
          - the RAW string when nothing matches (v2.1 used to force
            "error" here, which alarmed users whose wallbox had a
            state they hadn't mapped yet — better to pass through and
            let iOS display the actual wallbox label),
          - raw when no mapping is configured at all (pre-v2.0 setups).
        """
        if raw is None:
            return None
        plugged = dev.get(CONF_VEHICLE_STATUS_VALUE_PLUGGED, "")
        unplugged = dev.get(CONF_VEHICLE_STATUS_VALUE_UNPLUGGED, "")
        error = dev.get(CONF_VEHICLE_STATUS_VALUE_ERROR, "")
        # No mapping at all → pass through raw.
        if not plugged and not unplugged and not error:
            return raw
        normalised = raw.strip().lower()

        def _matches(mapping: str) -> bool:
            if not mapping:
                return False
            return any(
                normalised == part.strip().lower()
                for part in mapping.split(",")
                if part.strip()
            )

        if _matches(plugged):
            return "plugged"
        if _matches(unplugged):
            return "unplugged"
        if _matches(error):
            return "error"
        # Unmapped state — surface the wallbox's raw label rather than
        # mis-labelling it "error" and panicking the user.
        return raw

    def _read_is_on_state(self, dev: dict[str, Any]) -> bool | None:
        """Translate the device's entity_control current state into a
        Boolean `is_on`. Returns None when we can't decide cleanly so the
        backend keeps its existing value rather than guessing.

        - switch / input_boolean / light / fan: HA's native "on" / "off".
        - number / select / climate: compare against value_on / value_off.
          Equal to value_on → True, equal to value_off → False, anything
          else (a user setting a different value manually) → None.

        Spezialfall climate-Entity mit supports_cooling: ein "cool"
        State zählt explizit als is_on=False (nicht heizen), damit das
        Backend die Heat/Cool-Trennung sauber sieht.
        """
        entity_id = dev.get(CONF_ENTITY_CONTROL, "") or ""
        if not entity_id:
            return None
        state = self._get_state(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None

        domain = entity_id.split(".", 1)[0]
        raw_state = str(state.state)

        if domain in ("switch", "input_boolean", "light", "fan"):
            if raw_state.lower() == "on":
                return True
            if raw_state.lower() == "off":
                return False
            return None

        value_on = dev.get(CONF_VALUE_ON, "")
        value_off = dev.get(CONF_VALUE_OFF, "")

        def _matches(target: Any) -> bool:
            if target in ("", None):
                return False
            if domain in ("number", "input_number"):
                try:
                    return float(raw_state) == float(target)
                except (TypeError, ValueError):
                    return False
            return raw_state == str(target)

        if _matches(value_on):
            return True
        if _matches(value_off):
            return False
        # Cooling-aware: wenn die selbe Entity gerade auf cool-Wert
        # steht (climate.* mit value_cool_on = "cool"), ist das Gerät
        # NICHT am heizen.
        if dev.get(CONF_SUPPORTS_COOLING):
            value_cool_on = dev.get(CONF_VALUE_COOL_ON, "")
            if _matches(value_cool_on):
                return False
        return None

    def _read_cool_on_state(self, dev: dict[str, Any]) -> bool | None:
        """Translate cooling-side state into a Boolean `cool_on`.

        Drei Konfigurationen:
        1. supports_cooling=False → immer None (Backend bleibt 0).
        2. Separate entity_cool_control gemapped → diese Entity gegen
           value_cool_on / value_cool_off (bzw. value_off).
        3. Geteilte entity_control (typisch climate.*) → die selbe
           Entity gegen value_cool_on / value_off (Heizung-Off-Wert
           dient auch als Cool-Off).

        Returns None bei unklarem State, sodass Backend cool_on
        unverändert lässt.
        """
        if not dev.get(CONF_SUPPORTS_COOLING):
            return None
        cool_entity = dev.get(CONF_ENTITY_COOL_CONTROL, "") or ""
        if cool_entity:
            entity_id = cool_entity
            value_cool_on = dev.get(CONF_VALUE_COOL_ON, "")
            value_cool_off = dev.get(CONF_VALUE_COOL_OFF, "")
        else:
            entity_id = dev.get(CONF_ENTITY_CONTROL, "") or ""
            value_cool_on = dev.get(CONF_VALUE_COOL_ON, "")
            value_cool_off = dev.get(CONF_VALUE_OFF, "")
        if not entity_id:
            return None
        state = self._get_state(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        domain = entity_id.split(".", 1)[0]
        raw_state = str(state.state)

        def _matches(target: Any) -> bool:
            if target in ("", None):
                return False
            if domain in ("number", "input_number"):
                try:
                    return float(raw_state) == float(target)
                except (TypeError, ValueError):
                    return False
            return raw_state == str(target)

        if _matches(value_cool_on):
            return True
        if _matches(value_cool_off):
            return False
        return None
