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
| `state_mirror.py` | `DeviceStateMirror`: active/on/cool-State + Hold-Tasks + `last_sse_event_at` |
| `telemetry_composer.py` | Background-Loops (Heartbeat, Device-Mirror mit eigenem `_last_mirror_at`, State-Resync), `bootstrap_active_state()`, `push_outdoor_temp()`; Mirror ist auf Telemetrie-Consent gegated |
| `device_field_spec.py` | SSOT für Device-Felder im create/update-Roundtrip (`build_payload(mode, …)`) |
| `preset_spec.py` | Slot-Schema für Crowd-Presets (public Teil des Store-Vertrags, SSOT): `PRESET_SLOT_SPEC` je Typ (solar/grid/battery/wallbox; Slot-Arten entity/value/flag + `required`), `PRESET_VALUE_SLOTS` (box_add_device-Allowlist), `extract_preset_maps()` + `missing_required_labels()` für den Contribute-Pfad |
| `config_flow.py` | Pairing-Code-Onboarding (User erzeugt Code in der App → Flow claimt `POST /api/v1/connector/claim`, kein Email/Passwort im UI; Reauth ebenso per Code) + 3-Step-Geräte-Anlage + Nominatim-Reverse-Geocoding + Import-Flow für Box-Provisioning; Options-Flow persistiert sofort |
| `provisioning.py` / `box_services.py` | Box-Pfade (nur mit `theothergas:`-YAML-Key) |
| `const.py` | Domain, `DEVICE_TYPES`, `CONTROLLABLE_TYPES` (SSOT), `CONF_ENTITY_*`, `MAPPABLE_ENTITY_DOMAINS` (Allowlist), Intervalle |
| `sensor.py` / `switch.py` / `binary_sensor.py` | Plattformen |
| `device_registry.py` / `entity_mapper.py` | Registry-Glue + Entity-Mapping (`dominant_integration_domain`) |

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
- **Battery-Dispatch** (`_apply_battery_setpoint`): schreibt
  `entity_battery_mode` (Select) + `entity_battery_power_setpoint`
  (Number), idempotent mit ±10-W-Toleranz.
- **Hold-Loops:** als `hass.async_create_background_task` (sauberes
  HA-Shutdown). `entity_control` 30-s-, `entity_charge_mode`
  15-s-Cadence; beide bailen bei SSE-Stale (>60 s). Self-Heal respektiert
  SSE-Stale. Climate-Guard liest das `temperature`-Attribut.
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
- **Contribute** (alle vier preset-fähigen Typen, Vollständigkeits-
  Gate `contribute_incomplete`) schickt `integration_domain` mit
  (`entity_mapper.dominant_integration_domain`) und
  `entity_map`/`value_map` per `preset_spec.extract_preset_maps`
  (Allowlist = Anonymisierung; `value_map` nur wenn belegt —
  Alt-Backend-kompatibel). Store-Vertrag: `docs/crowd-preset-store.md` —
  Lookup liefert `status` (staged/approved), `value_map`, `updated_at`;
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
`test_connector_pairing_alias` (/connector/* = /box/*-Alias).
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
