"""Constants for the Crowdergy Connector integration."""

import json
from pathlib import Path

# Pull the version from manifest.json so we have a single source of
# truth. Used in HTTP User-Agent strings (Nominatim's TOS asks for a
# descriptive UA; bumping it along with each release keeps drift out).
_MANIFEST = json.loads((Path(__file__).parent / "manifest.json").read_text())
VERSION: str = _MANIFEST["version"]
USER_AGENT: str = (
    f"crowdergy-connector/{VERSION} "
    "(+https://github.com/colinwilke/crowdergy-connector)"
)

DOMAIN = "theothergas"
DEFAULT_API_URL = "https://api.theothergas.de"
CONF_API_URL = "api_url"
CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_USER_ID = "user_id"
CONF_DEVICES = "devices"
CONF_DEVICE_ID = "device_id"
# Heating + Warmwater haben jeweils ein eigenes thermisches Modell im
# joint MPC. Ein warmwater-Gerät kann via CONF_SHARES_HARDWARE_WITH
# erklären, dass es einen Verdichter mit einem heating-Gerät teilt
# (joint-power-constraint serverseitig).
DEVICE_TYPES = [
    "solar", "battery", "wallbox", "grid",
    "heating", "warmwater", "aircon",
    "generic", "haushalt",
]
UPDATE_INTERVAL = 30

CONF_DEVICE_NAME = "device_name"
CONF_DEVICE_TYPE = "device_type"

# v3.0 Konfigurations-Modus pro Gerät. User wählt einmal im Anlage-
# Flow wie er sein Gerät konfiguriert; folgende Steps wechseln daraufhin
# ihr Schema (Manuell = alle Entities einzeln; Climate = climate.*-
# Entity + abgeleitete Steuerung + Modi). Aktuell relevant nur für
# heating + warmwater. Wallbox + sonstige laufen immer "manual" (Wert
# implizit, Step wird übersprungen). Edit-Flow zeigt den Picker nicht;
# wer den Modus wechseln will: Gerät entfernen + neu anlegen.
CONF_DEVICE_CONFIG_MODE = "config_mode"
CONFIG_MODE_MANUAL = "manual"
CONFIG_MODE_CLIMATE = "climate"

# v3.0 Zweite Power-Entity für bidirektionale Geräte (Grid-
# Einspeisung, Batterie-Ladung, später V2G-Wallbox). Wenn nur das
# erste Power-Feld gesetzt ist, gilt der signed-Convention-Path
# (positiv = aus dem Device, negativ = ins Device) plus der bekannte
# invert_power_sign-Toggle. Wenn beide gesetzt sind, computet der
# Coordinator power = power_1 − power_2 analog zur Energie-Logik.
CONF_ENTITY_POWER_2 = "entity_current_power_kw_2"
CONF_DISTRICT = "district"
CONF_CITY = "city"
CONF_REGION = "region"
# Integration-wide (not per-device): an HA sensor with the current
# outdoor temperature in °C. Optional — when missing, the backend
# falls back to Open-Meteo for the user's location every 15 min.
CONF_ENTITY_OUTDOOR_TEMP = "entity_outdoor_temp_c"
CONF_ENTITY_POWER = "entity_current_power_kw"
# Wenn True: power_kw vor dem PATCH ans Backend mit -1 multiplizieren.
# Crowdergy-Konvention: positiv = Bezug / Verbrauch / Lade, negativ =
# Einspeisung / Abgabe / Entlade. Manche Wechselrichter exposen ihre
# Wirkleistung umgekehrt (positiv beim Einspeisen). Dieser Toggle
# erspart dem User ein HA-Template — er muss nur die existierende
# Sensor-Entity wählen + diesen Haken setzen.
CONF_INVERT_POWER_SIGN = "invert_power_sign"
CONF_ENTITY_SOC = "entity_soc_percent"
# Wallbox-only read-only field.
CONF_ENTITY_VEHICLE_STATUS = "entity_vehicle_status"
# Heatpump (and hot-water tank) read-only current-temperature entity.
# Optional — devices without a temp sensor leave it null. The setpoint
# entity was removed in v2.5.3: Crowdergy never wrote it, and the
# stored value duplicated information the user controls via their own
# HA heating integration.
CONF_ENTITY_CURRENT_TEMP = "entity_current_temp_c"
# Optional per-device kWh meter. Must be an HA sensor with
# `state_class: total_increasing` (lifetime cumulative). The
# Crowdergy backend stores the raw value and derives day / month /
# year totals via SQL `date_trunc` deltas — never integrate this
# from `power_kw` (5–10 % drift compounds badly). Devices without
# such a sensor mapped just keep the kW-only display in the app.
CONF_ENTITY_ENERGY_TOTAL = "entity_energy_total"
# Optional second `total_increasing` kWh counter for the OTHER
# direction. Offered for bidirectional device types (battery, grid,
# later V2G wallbox) where HA typically exposes the two flows as
# separate cumulative sensors. Semantically: `entity_energy_total`
# above carries the "consumed by device" counter (battery charge,
# grid import); this field carries the "delivered by device" counter
# (battery discharge, grid export). When both are mapped the
# coordinator computes a signed net Δ so the backend stores a
# single value per row.
# Config key stays `entity_energy_discharged_total` for stored-config
# backward compatibility — pre-v1.24 it was battery-only.
CONF_ENTITY_ENERGY_DISCHARGED_TOTAL = "entity_energy_discharged_total"

# As of v1.8.0 Crowdergy controls each device via a single user-mapped
# entity. When the Crowdergy app flips the device on/off (or, later, the
# smart controller does), the connector writes `value_on` / `value_off`
# to `entity_control`. Replaces the previous typ-specific writable
# entity mappings (entity_is_active / entity_soc_min / entity_soc_max /
# entity_charge_mode) which are no longer used.
CONF_ENTITY_CONTROL = "entity_control"
CONF_VALUE_ON = "value_on"
CONF_VALUE_OFF = "value_off"

# Climate-first Onboarding für heating + warmwater (v2.6.0+). Wenn der
# User eine climate.* Entity wählt, leitet der Connector daraus
# automatisch ab: Steuerung (set_hvac_mode), Ist-Temperatur (Attribut
# current_temperature), Heiz/Kühl-Modi (hvac_modes). Form-only Feld —
# beim Speichern kopieren wir den Wert auf entity_control +
# entity_current_temp_c, sodass die Coordinator/Backend-Pipeline
# unverändert bleibt. Leer = klassischer Pfad mit separater Steuer-
# und Ist-Temperatur-Entity.
CONF_ENTITY_CLIMATE = "entity_climate"

# v3.0.6 Warmwasser-Variante zu CONF_ENTITY_CLIMATE: viele
# Brauchwasser-WPs erscheinen in HA als water_heater.* statt
# climate.* — separates Form-Feld mit eigenem Domain-Selector
# damit der EntitySelector nur passende Optionen anbietet.
# _apply_climate_first collapsed beides aufs gleiche entity_control
# zum Speichern.
CONF_ENTITY_WATER_HEATER = "entity_water_heater"

# Wallbox-only: separate write-target for the Lademodus picker in the iOS
# app. Settable HA select entity (typically with "Lock Mode" / "Power Mode" /
# "Solar Pure Mode" — or any other options the user has configured on their
# wallbox integration). Distinct from entity_control because the user
# wants three-way manual control rather than a binary on/off mapping.
CONF_ENTITY_CHARGE_MODE = "entity_charge_mode"

# Wallbox-only ternary mapping for the charge-mode select entity (v2.2+).
# The Crowdergy app exposes three explicit modes — "Aus", "An",
# "Solaroptimiert" — plus the AI auto-pilot. Each maps onto one of the
# wallbox's HA select-options (e.g. "Lock Mode" / "Power Mode" /
# "Solar Pure Mode" on a go-eCharger; "Off" / "Boost" / "Eco" on
# others). v2.1 and earlier had the user enter these strings in the
# iOS app — moving the mapping here keeps all infrastructure plumbing
# in one place alongside CONF_VALUE_ON / OFF and CONF_VEHICLE_STATUS_*.
#
# Internal token names track the solver's vocabulary:
#   * lock  → "Aus"             (charging blocked)
#   * power → "An"              (max-power push)
#   * solar → "Solaroptimiert"  (PV-surplus follow)
# Any mode the user leaves blank simply doesn't get a button in iOS.
CONF_CHARGE_MODE_VALUE_LOCK = "charge_mode_value_lock"
CONF_CHARGE_MODE_VALUE_POWER = "charge_mode_value_power"
CONF_CHARGE_MODE_VALUE_SOLAR = "charge_mode_value_solar"

# Battery dispatch v3.8.0 (Phase 3 Option D, 2026-06-02). Aus den alten
# 4 mode-string Werten wird ein 2-Entity-Setup mit kontinuierlichem
# Power-Setpoint. Backend Solver wählt:
#   * Mode aktiv vs passiv      → entity_battery_mode (Select)
#   * signed Power-Setpoint kW  → entity_battery_power_setpoint (Number)
# HA-Automation hält den Wert bei aktiv, schreibt nichts bei passiv
# (= WR-Native-PV-Priority).
CONF_ENTITY_BATTERY_MODE = "entity_battery_mode"
CONF_VALUE_BATTERY_MODE_ACTIVE = "value_battery_mode_active"
CONF_VALUE_BATTERY_MODE_PASSIVE = "value_battery_mode_passive"
CONF_ENTITY_BATTERY_POWER_SETPOINT = "entity_battery_power_setpoint_w"
# Sign-Convention: solver-intern + = laden, − = entladen. Wenn der
# Inverter umgekehrt verdrahtet ist (z.B. − = laden), kann der User
# hier invertieren — Connector multipliziert dann mit −1 vor dem Write.
CONF_BATTERY_SETPOINT_INVERT_SIGN = "battery_setpoint_invert_sign"

# Wallbox-only ternary mapping for the vehicle-status sensor (v2.0+).
# Pre-v2.0 the connector forwarded the raw HA state string and the
# backend / iOS tried to interpret it from localized labels — fragile.
# Now the user maps each of their wallbox's possible states to one of
# three normalised values:
#   * 'plugged'    — a car is connected (charging / connected / paused / …)
#   * 'unplugged'  — no car (idle / disconnected / …)
#   * 'error'      — fault / authorisation error / cable error
# Coordinator normalises before pushing; mapping stays local to HA.
# If none of the three values is configured the connector falls back
# to passing the raw string (back-compat with pre-v2.0 config entries).
CONF_VEHICLE_STATUS_VALUE_PLUGGED = "vehicle_status_value_plugged"
CONF_VEHICLE_STATUS_VALUE_UNPLUGGED = "vehicle_status_value_unplugged"
CONF_VEHICLE_STATUS_VALUE_ERROR = "vehicle_status_value_error"

# Warmwater-only: the backend device id of the heating device that
# shares the same compressor as this warmwater device. Sent to the
# backend on POST /devices so the joint solver can enforce
# `P_heat + P_ww ≤ P_compressor_max`. Optional — leave empty for a
# standalone WW heater on a separate appliance.
CONF_SHARES_HARDWARE_WITH = "shares_hardware_with_device_id"

# Hold-mode for entity_control: how the connector handles devices whose
# Modbus registers / OCPP transaction state revert to a default after a
# few seconds (Kostal, SMA, some OCPP wallboxes …).
#
# "always" (default) — write once, then re-write every 30 s for as long
#                      as Crowdergize is active. Harmless on devices
#                      that hold fine; rescues those that don't.
# "never"            — write once, leave it alone. For low-traffic
#                      Modbus setups; assumes the device sticks.
#
# Legacy `auto` values stored in old config entries collapse to
# `always` at load time (see `_start_hold`) — we used to have a
# release-after-stable-checks third mode but it never paid off
# against the simple-and-safe periodic rewrite.
CONF_ENTITY_CONTROL_HOLD = "entity_control_hold"
# Drei Modi für die Steuer-Entitäts-Hold-Loop (gilt für entity_control
# bei heating/warmwater/generic und für entity_charge_mode bei wallbox/
# battery):
#   "auto"   — Default für neue Geräte. Aktuell identisch zu "always"
#              (alle 30 s nachschreiben). Reserviert für zukünftige
#              Smart-Verifikation: erstmal nachschreiben, dann nach
#              stabilen Reads aufhören.
#   "always" — User-explizit: immer nachschreiben, kein Smart.
#   "never"  — Einmal schreiben, dann Crowdergy fasst die Entity nicht
#              mehr an (für Geräte die bei jedem Schreiben piepen,
#              eine Aktion auslösen oder ein Display einschalten).
ENTITY_CONTROL_HOLD_AUTO = "auto"
ENTITY_CONTROL_HOLD_ALWAYS = "always"
ENTITY_CONTROL_HOLD_NEVER = "never"

# Classic-consumer Flag für den Haushalt-Doppelzähl-Fix. Wenn True,
# zählt das Gerät bereits in den Haushalts-Sensor — das Backend zieht
# es bei jeder Snapshot-Berechnung ab. Nur für heating / warmwater /
# wallbox / generic; solar / grid / battery / haushalt selbst tragen
# es nie.
CONF_INCLUDED_IN_HAUSHALT = "included_in_haushalt"

# Cooling-Capability für heating-family. Capability-Flag + entity /
# value mappings für die Kühl-Seite. supports_cooling=True →
# Solver-Layout schaltet auf Dual-Mode MILP; Connector dispatcht die
# Kühl-Entscheidung an entity_cool_control ODER ruft bei nativen
# climate.* Entities `set_hvac_mode('cool')` gegen entity_control auf.
CONF_SUPPORTS_COOLING = "supports_cooling"
CONF_ENTITY_COOL_CONTROL = "entity_cool_control"
CONF_VALUE_COOL_ON = "value_cool_on"
CONF_VALUE_COOL_OFF = "value_cool_off"

# Solver-only Telemetry-Felder (v3.3+, Phase-0-Infra). Pro Gerätetyp
# eine Liste optionaler HA-Sensor-Entities, deren Wert pro Tick als
# `extra: {key: value, ...}` mit dem Telemetry-PATCH mitläuft.
# Backend (app/mpc/solver_fields.py) ist Single Source of Truth für
# Validierung — Connector schiebt locker rüber, Backend filtert
# unbekannte/falsche Werte raus.
#
# Neue Solver-Eingabe = (1) Eintrag hier + (2) Registry-Eintrag im
# Backend + (3) Worker liest via `read_extra(latest_tele.extra, key)`.
# UI bleibt unangetastet — diese Felder sind explizit nicht für die
# iOS-App gedacht.
CONF_ENTITY_VORLAUF_TEMP = "entity_vorlauf_temp_c"

# Phase 2b Refactor (2026-06-02): write-side Vorlauf-Setpoint-Entity.
# Bei modulierenden Heizungen (Backend setzt is_modulating_heating=True
# für device.type=heating, supports_cooling=False) sendet der Solver
# pro Tick einen `vorlauf_setpoint_c`-Wert (°C) per SSE. Der Connector
# dispatcht ihn via `climate.set_temperature(entity_vorlauf_setpoint,
# temperature=°C)` an die HA-Entity die der User hier mapped. Wenn
# leer / nicht gemapped: kein Dispatch, Solver-Empfehlung wird
# stillschweigend ignoriert (User kann später nachpflegen).
CONF_ENTITY_VORLAUF_SETPOINT = "entity_vorlauf_setpoint_c"

# Hold loop timing. Initial 10 s lets the apply call's effect
# propagate before the first rewrite. 30 s catches most auto-revert
# cycles (Kostal's is 60 s).
HOLD_INITIAL_DELAY = 10
HOLD_POLL_INTERVAL = 30

# v2.4: separate, tighter hold cadence for the charge-mode entity
# (input_select for battery Lademodus + wallbox Lademodus). User-
# reported: some inverters reset the mode after ~15 s if not
# refreshed, so the 30 s entity_control cadence is too slow for
# this path. Initial delay matches HOLD_INITIAL_DELAY so the
# debounce-after-write rhythm stays consistent.
CHARGE_MODE_HOLD_INITIAL_DELAY = 10
CHARGE_MODE_HOLD_INTERVAL = 15

# v2.4.2: safety guard. If no SSE event (ping or data) has arrived in
# this many seconds, the hold-loop assumes Crowdergy is offline or
# unreachable and bails — the inverter's native logic regains control
# rather than being stuck on the last commanded mode. Backend sends
# `{"type":"ping"}` every 15 s, so 60 s = 4 missed pings = honest
# "Crowdergy is gone" signal without false-triggering on a single
# packet loss.
SSE_STALE_THRESHOLD_S = 60

PLATFORMS = ["sensor", "switch", "binary_sensor"]

# ── Auto-Mapping (Phase 1) ────────────────────────────────────────────
# v3.1: erster Schritt im Config-Flow ist eine Wahl zwischen
# „Manuelles Setup" (klassischer Pfad, alle Entities einzeln) und
# „Auto-Setup" (Heuristik scannt HA, schlägt Devices + Slots vor;
# User bestätigt). Per ConfigEntry fix gewählt — wer wechseln will,
# entfernt die Integration und legt sie neu an.
CONF_SETUP_MODE = "setup_mode"
SETUP_MODE_MANUAL = "manual"
SETUP_MODE_AUTO = "auto"

# Confidence-Schwellen für die Heuristik (im entity_mapper):
# ≥ HEURISTIC_ACCEPT = sicher pre-fill, kein „prüfen"-Marker.
# < HEURISTIC_REJECT = unklar, zeigt „bitte prüfen" oder skippt den
#   Slot komplett.
# dazwischen = pre-fill mit Marker, in Phase 2 zusätzlich an LLM weiter.
HEURISTIC_ACCEPT = 0.80
HEURISTIC_REJECT = 0.40

# Phase-2-Gate (v3.2.0): Backend-LLM-Fallback ist aktiv. Wenn die
# Heuristik Slots offen lässt UND der Backend-Endpoint einen Anthropic-
# Key konfiguriert hat, wird Claude Haiku 4.5 die Restmenge versuchen
# zu klassifizieren. Kill-Switch falls man rein deterministisch laufen
# will: hier auf False setzen.
MAPPING_LLM_ENABLED = True
LLM_MIN_CONFIDENCE = 0.5
