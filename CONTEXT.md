# crowdergy-connector — aktueller Stand

HACS Custom-Component (Domain `theothergas`, Legacy-Name) für Home
Assistant: spiegelt Backend-Dispatch in HA-Entities und pusht
Telemetrie zurück. Regeln/Stolpersteine: `CLAUDE.md` hier; Backlog,
Versions-/Release-„Stand": `crowdergy-ios/CLAUDE.md` (SSOT). Aktuelle
Version: `manifest.json`.

## Modul-Struktur (`custom_components/theothergas/`)

| Datei | Verantwortung |
|---|---|
| `coordinator.py` | Hot-Path: Auth/Refresh, `DataUpdateCoordinator`-Loop, Entity-Reader, Frame-Dispatch (`_handle_ws_message`), Apply-Handler (`_apply_device_state`, `_apply_cool_state`, `_apply_battery_setpoint`), Hold-Loop-Spawning |
| `sse_client.py` | Reconnecting SSE-Listener auf `/api/v1/stream`, Bearer-Header (kein `?token=`), Queue (maxsize 512), Auth-Refresh-Callback, Half-Open-Detection (60-s-Read-Timeout), 401-Backoff + Reauth-Flow nach 5 Zyklen |
| `state_mirror.py` | `DeviceStateMirror`: active/on/cool-State + Hold-Tasks + `last_sse_event_at`; seit dem Sicherheitsbündel (2026-08-25) auch `entity_write_counts`/`write_breaker_devices` (#136), `local_override_until`/`last_own_write_at` (#140) |
| `telemetry_composer.py` | Background-Loops (Heartbeat, Device-Mirror mit eigenem `_last_mirror_at`, State-Resync), `bootstrap_active_state()`, `push_outdoor_temp()`; Mirror ist auf Telemetrie-Consent gegated. **Der Mirror strippt JEDES Energie-Δ-Feld (`_DELTA_FIELDS` = signed `energy_kwh_delta` + unsigned `energy_kwh_in/out_delta`-Paar) — der Re-Send refresht nur die Freshness-Clock, doppelte Δ-kWh würden vom Backend ein zweites Mal gezählt (#62).** |
| `device_field_spec.py` | SSOT für Device-Felder im create/update-Roundtrip (`build_payload(mode, …)`) |
| `preset_spec.py` | Slot-Schema für Crowd-Presets (public Teil des Store-Vertrags, SSOT): `PRESET_SLOT_SPEC` je Typ (solar/grid/battery/wallbox + heating/warmwater/aircon seit #68; Slot-Arten entity/value/flag + `required`), `PRESET_VALUE_SLOTS` (box_add_device-Allowlist), `extract_preset_maps()` + `missing_required_labels()` für den Contribute-Pfad |
| `config_flow.py` | Pairing-Code-Onboarding (User erzeugt Code in der App → Flow claimt `POST /api/v1/connector/claim`, kein Email/Passwort im UI; Reauth ebenso per Code) + manueller Per-Gerät-Anlage (Auto-Setup seit v3.39.0 deaktiviert, `location`→`device_type` direkt) + Nominatim-Reverse-Geocoding + Import-Flow für Box-Provisioning; Options-Flow persistiert sofort. Contribute schickt `required_integrations`, der Profil-Picker labelt die benötigten Integrationen |
| `provisioning.py` / `box_services.py` | Box-Pfade (nur mit `theothergas:`-YAML-Key) |
| `const.py` | Domain, `DEVICE_TYPES`, `CONTROLLABLE_TYPES` (SSOT), `CONF_ENTITY_*`, `MAPPABLE_ENTITY_DOMAINS` (Allowlist), Intervalle |
| `sensor.py` / `switch.py` / `binary_sensor.py` | Plattformen |
| `device_registry.py` / `entity_mapper.py` | Registry-Glue + Entity-Mapping (`dominant_integration_domain`, `required_integration_domains` = alle distinkten Integrationen des Mappings, `INTEGRATION_DISPLAY_NAMES`/`integration_display_name` = Klarnamen). **Auto-Discovery (`discover_devices`) ist im Config-Flow NICHT mehr verdrahtet (v3.39.0) — Code bleibt für spätere Reaktivierung.** |

## Coordinator / Sync-Stack

- **SSE (primär):** `sse_client.py` füttert eine `asyncio.Queue`,
  Coordinator konsumiert per Background-Task. Half-Open-Erkennung über
  60-s-Read-Timeout (Backend pingt alle 15 s → 4 verpasste Pings =
  Reconnect). Frames werden geloggt.
- **Frame-Dispatch:** `type=telemetry` →
  `is_active`/`is_on`/`cool_on`/`vorlauf_setpoint_c` (manuelle
  App-Befehle); `type=command` → `set_charge_mode` (Battery/Wallbox);
  `type=device_update` → `_apply_preset_from_backend` (iOS-`apply-preset`
  hat ein Vendor-Preset gestempelt → Gerät+Preset ziehen, vollen
  `value_map` in die Device-Config mergen, entkoppelter Reload);
  `ping` → noop. Ohne Remote-Control-Consent werden Steuer-Frames
  ignoriert; das Gate sitzt zentral in allen `_apply_*`.
- **Sicherheitsbündel (2026-08-25, RELEASED v3.46.0):** `command_dispatcher._clamp_write_value` klemmt
  jeden numerischen Write gegen die Entity-Grenzen (#135);
  `_write_allowed` = Schreib-Circuit-Breaker je Entity/Stunde (#136,
  `WRITE_BREAKER_MAX_PER_HOUR`); der AUTO-`_hold_loop` erkennt
  Fremd-Drift ohne eigenen Write in `LOCAL_OVERRIDE_GRACE_S` als
  Nutzer-Eingriff und pausiert das Gerät `LOCAL_OVERRIDE_HOLD_S`
  (#140). Beide Zustände gehen als `write_breaker`/`local_override`
  in die Telemetrie (Backend `/me/health`; Backend MUSS zuerst
  deployed sein — `extra="forbid"`).
- **WP-Temperatur-Modus** (heating/warmwater, 2026-07-02): sind
  value_on/value_off NUMERISCH und `entity_control` climate/
  water_heater, schreibt `_apply_device_state` via `set_temperature`
  Ziel-Temperaturen (AN = Max, AUS = Min) statt harter Modus-Writes —
  KEIN `set_hvac_mode("off")`/`set_operation_mode("off")` mehr; die WP
  regelt Laufzeit/Takten selbst (number/input_number war schon immer
  so via `set_value`). Idempotenz/Hold/Resync vergleichen dann das
  `temperature`-Attribut (`_control_actual_state`), `_read_is_on_state`
  mappt Max-Temp→AN / Min-Temp→AUS / fremder Wert→None. Helper
  `is_temperature_control`/`temperature_control_value` in `const.py`;
  Config-Flow bietet für diese Typen °C-NumberSelector-Felder.
  Nicht-numerische Werte = Legacy-Modus-Pfad (unverändert).
- **Battery-Dispatch** (`_apply_battery_setpoint`): schreibt
  `entity_battery_mode` (Select) + `entity_battery_power_setpoint`
  (Number), idempotent mit ±10-W-Toleranz.
- **Wallbox-Ladestrom** (2026-06-20, analog Battery-Setpoint): optionale
  Number-Entity `entity_wallbox_charge_current_a` (Ampere 6–16). Wenn
  gemappt, leitet `device_field_spec` daraus `wallbox_supports_charge_current`
  ab → Backend-Solver wählt den Strom variabel. `_apply_charge_mode`
  bekommt `charge_current_a` (vom `set_charge_mode`-Command, NUR
  Power/„An"-Modus) und schreibt ihn ZUERST (`number.set_value`), dann den
  Modus-Select; `state.held_charge_current` cached ihn für die
  Hold-Loop-Re-Writes. Solar/Lock stromlos, Solarmode unverändert.
- **Hold-Loops:** als `hass.async_create_background_task` (sauberes
  HA-Shutdown). `entity_control` 30-s-, `entity_charge_mode`
  15-s-Cadence; beide bailen bei SSE-Stale (>60 s). Self-Heal respektiert
  SSE-Stale. Climate-Guard liest das `temperature`-Attribut. Der
  charge_mode-Stale-Bail startet seit v3.45.0 einen One-shot-**Lease-
  Expiry-Task** (`_charge_mode_lease_expiry`, `COMMAND_LEASE_TTL_S`
  900 s): nach 15 min ohne SSE-Event EIN Safe-Default-Write (wallbox →
  Solar wenn gemappt, battery → passive) gegen stickige Mode-Selects.
- **Resync-Backstop:** periodisches `GET /devices` erkennt Drift nach
  SSE-Drop und re-applied (consent-gated).
- **Heartbeat:** leichter `POST /me/heartbeat` für den
  iOS-Connection-Dot (Liveness, bewusst nicht consent-gated).
- **Resilienz:** Telemetry-`PATCH` (primärer Send *und*
  Mirror) läuft über `_patch_telemetry_with_retry` — bounded
  Retry/Backoff (3 Versuche, 0.5→1.0 s) auf transiente Fehler
  (Transport-Error + 5xx/429); permanente 4xx inkl. 404/410-Eviction
  gehen ohne Retry durch. Access-Token wird proaktiv ~120 s vor `exp`
  erneuert (`_jwt_exp` liest den exp-Claim unverifiziert,
  `_maybe_proactive_refresh` vor jedem Call in `_authenticated_request`),
  single-flight via `seen_token`-CAS — der reaktive 401-Pfad bleibt als
  Fallback.
- **Telemetry-`extra`-Bag (`_compose_extra` + `_SOLVER_EXTRA_FIELDS`):**
  per-Typ registrierte Read-Sensoren laufen als JSONB-`extra` mit
  (Reader `temp`→°C, `power`→kW). Heute: `vorlauf_temp_c` (heating/
  warmwater, solver) + die HC-Flow-Triade `hc_pv/_battery/_grid_power_kw`
  (solar/battery/grid) + optional `pv_to_battery_power_kw` (battery) für
  den Hausverbrauchs-Chart (vom Backend gelesen, NICHT solver-gelesen).
  Keys spiegeln 1:1 Backend-`SOLVER_FIELDS`. Vertrag:
  `docs/house-consumption-chart.md`.
- **Kein Connector-Code, aber Vertrags-Heimat:** `docs/costs-today.md`
  (#71, contract-first) beschreibt den Backend-Endpoint
  `GET /me/costs/today` (kumulierte EUR-Netzbezugs-Kosten heute, iOS-
  `PriceSavingsSheet`-Kurve). Die Kosten rechnet das Backend aus
  vorhandener Grid-`energy_kwh_delta`-Telemetrie + Tarif-Config — der
  Connector liefert dafür nichts Neues. Backend+Vertrag auf `main` +
  prod-deployed 2026-06-21.

## Config-Flow

- **Onboarding per Pairing-Code:** der User erzeugt in der App einen Code,
  der Flow claimt damit `POST /api/v1/connector/claim` (Token-Paar, keine
  Email/Passwort-Felder im UI). `/box/claim` bleibt Alias. Reauth läuft
  ebenfalls über einen neuen Pairing-Code (Account-Mismatch-Guard).
- **Geräte-Anlage in 3 Steps:** (1) Typ + Name, (2) typ-spezifische
  Entities Read/Control, (3) `value_on`/`value_off` typ-bewusst.
  Edit-Felder mit `suggested_value`, nie `default=`.
- Standort via Nominatim-Reverse-Geocoding aus
  `hass.config.latitude/longitude`.
- `DEVICE_TYPES`: solar / battery / wallbox / grid / heating / warmwater /
  aircon / generic / haushalt. Crowdergize-fähig: battery / wallbox /
  heating / warmwater / aircon / generic.
- **Geräte-Topologie:** kein `included_in_haushalt`-Flag mehr — die
  Haushalt-Zugehörigkeit liegt als generischer `parent_device_id`-Baum
  im Backend (pro Gerät in der App konfiguriert), nicht im Connector.
- Brand-Trennung: UI „Crowdergy AI", Switch intern `crowdergize`
  (`CrowdergyActiveSwitch`).

## Energie-Zähler-Konvention (E2E, ab v3.21.3)

`entity_energy_total` = Bezug/Entladen-Zähler (device → home),
`entity_energy_discharged_total` = Einspeisung/Laden-Zähler. Telemetrie
schickt explizite `energy_kwh_in_delta` + `energy_kwh_out_delta` (ab
v3.21.4); das Backend deriviert den signed Wert.

**Δ-Berechnung (`_counter_delta`, High-Water-Mark):** der Per-Tick-Δ
ist `current − prev` gegen den letzten GESENDETEN Stand
(`_prev_energy_kwh`/`_prev_energy_kwh_discharged`, fortgeschrieben nur
bei erfolgreichem Send). Ein Rückschritt des `total_increasing`-Zählers
regressiert die Baseline NICHT: kleiner Dip (≥ `ENERGY_RESET_RATIO`=0.9
des letzten Werts) = Sensor-Rauschen → Δ=0, Baseline gehalten; großer
Sturz (< 90 %) = echter Meter-Reset → Δ=0, Baseline neu auf `current`.
**Bug-Fix:** vorher wurde jeder Dip mit `in_delta=0` quittiert, ABER die
Baseline auf den niedrigeren Wert fortgeschrieben → der Re-Anstieg
zählte doppelt. Bei jitternden Solar-Zählern + dem quasi jeden Tick
sendenden aktiven Inverter (Power-Schwelle 50 W) summierte sich das zu
~10–15 % zu hohen PV-kWh (zusätzlich zum bereits gefixten Mirror-Double-
Count #62).

## Backend-API (alle Bearer im `Authorization`-Header)

`POST /auth/login`, `POST /auth/refresh`, `GET|POST /devices`,
`PUT|DELETE /devices/{id}`, `PATCH /devices/{id}/telemetry`,
`POST /devices/{id}/commands`, `GET /api/v1/stream` (SSE),
`POST /users/me/heartbeat`, `POST /users/me/outdoor`,
Crowd-Preset-Lookup/Contribute. Auth-Wrapper refresht proaktiv vor
Token-Ablauf UND reaktiv bei 401 (retry einmal).

## Crowdergy-Box-Integration (nur mit `theothergas:`-YAML-Key aktiv)

- **`provision_box`** (`__init__.py` + `provisioning.py` + Import-Flow):
  Token-Paar aus dem Box-Claim → Entry ohne UI; `unique_id` =
  Backend-User-ID, Re-Pairing aktualisiert Tokens statt Duplikat und
  merged Consent-Options im `already_configured`-Pfad. `consent_*`-Felder
  landen atomar als Entry-Options (kein Default-True-Fenster).
- **`box_services.py`**: `box_list_presets` (proxied approved
  Vendor-Preset-Lookup inkl. `integration_domain`, status, capabilities,
  `updated_at`), `box_add_device` (Registrierung über `device_field_spec`,
  POST; Value-Slots mit Punkt in Werten ab v3.24.0), `box_update_device`
  (#28 Re-Apply: PUT eines bestehenden Geräts in place — kein
  Backend-Duplikat, gleiche `device_id`/Historie/Topologie),
  `box_set_consent` (Entry-Options). Domain-Allowlist (`_validate_entity_
  mapping`) geteilt von add + update.
- **Consent-Gates:** ohne `consent_telemetry` kein Telemetrie-/
  Outdoor-Push; ohne `consent_remote_control` keine Steuer-Frames.
  Fehlende Flags = Alt-Verhalten (True).
- **Contribute** (alle sieben preset-fähigen Typen — solar/grid/battery/
  wallbox + heating/warmwater/aircon, Vollständigkeits-
  Gate `contribute_incomplete`) schickt `integration_domain` mit
  (`entity_mapper.dominant_integration_domain`) und
  `entity_map`/`value_map` per `preset_spec.extract_preset_maps`
  (Allowlist = Anonymisierung; `value_map` nur wenn belegt —
  Alt-Backend-kompatibel). Seit 2026-07-19 zusätzlich
  `entity_identity_map` (`entity_mapper.entity_identity_map`: je Slot
  platform + translation_key/original_name aus der Registry, NIE
  unique_id) — der Profil-Pick löst die Preset-Entity-IDs damit
  namens-unabhängig gegen die eigene Installation auf
  (`entity_mapper.resolve_preset_entities`: exakt → Identität →
  Suffix-Match; mehrdeutig/unauflösbar → verbatim-Prefill).
  Store-Vertrag: `docs/crowd-preset-store.md` —
  Lookup liefert `status` (staged/approved), `value_map`, `updated_at`,
  `entity_identity_map`;
  staged = Badge „Community, noch unbestätigt" in Picker + Box-GUI.
- `_authenticated_config_request` baut den httpx-Client im Executor
  (HA-Blocking-Call-Warnung, live gefunden).

## Tests (`tests/`)

pytest, Python ≥ 3.12. **CI `test.yml` läuft auf push→main + PRs**
(`cache-dependency-path: requirements-test.txt`); die zwei
aiodns-Drift-Tests `test_sse_client::test_start_is_idempotent` +
`test_stop_cancels_running_task` werden in CI deterministisch
deselektiert (siehe `CLAUDE.md`). Pure-Logic-Unit-Tests:
`test_device_field_spec` (SSOT-Guard), `test_preset_spec`
(Vertrags-Invarianten), `test_contribute_flow`, `test_state_mirror`,
`test_provisioning`, `test_box_services`, `test_integration_domain`,
`test_connector_pairing_alias` (/connector/* = /box/*-Alias),
`test_json_assets` (hard-parst jede ausgelieferte `*.json` —
Regressions-Guard nach dem v3.33.1-`de.json`-Crash, s.u.).
Coordinator-/Full-Flow-Integration offen (Backlog Cluster C).

## Offene Punkte

→ Backlog Cluster C in `crowdergy-ios/CLAUDE.md` (SSOT; hier bewusst nicht
re-listet). Bekannte Architektur-Eigenschaft (kein Backlog-Item):
Refresh-Tokens liegen im Klartext in `config_entries` (HA-Standard).

## Abhängigkeiten / Plattform

- **Backend** wird aufgerufen (s.o.); **iOS**: keine Direktverbindung
  (Daten via Backend-SSE-Broadcast); **Box** vendored dieses Repo per
  Git-Tag (`crowdergy-box/CONNECTOR_VERSION`).
- `manifest.json` (SSOT für die Version): Domain `theothergas`,
  `iot_class: cloud_push`, `requirements: ["httpx>=0.24.0"]`.
