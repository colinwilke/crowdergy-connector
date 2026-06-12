# crowdergy-connector — aktueller Stand

Release-Stand: **v3.25.0 auf main**; **v3.26.0 vorbereitet auf Branch
`claude/connector-arch-abstraction-cnrs9w`** (Pairing-Code-Onboarding +
Shared-API-Client, Tag steht aus — Backend mit `/connector/*`-Routen
idealerweise zuerst deployen, der Claim hat aber einen
`/box/claim`-Fallback). Box pinnt v3.25.0. HACS Custom-Component (Domain
`theothergas`, Legacy-Name) für Home Assistant: spiegelt Backend-Dispatch
in HA-Entities und pusht Telemetrie zurück. Regeln + Backlog:
`CLAUDE.md` hier bzw. `crowdergy-ios/CLAUDE.md` (Index).

## Modul-Struktur (`custom_components/theothergas/`)

| Datei | Verantwortung |
|---|---|
| `api_client.py` | **DER eine Auth-Pfad** (ab v3.26.0): `CrowdergyAuthSession` (Token-Paar, Single-Flight-Refresh mit CAS, 401-retry-once, lazy Executor-Client), `authenticated_request` (re-used die Coordinator-Session wenn vorhanden), `claim_pairing_code` (kanonisch `/connector/claim`, Fallback `/box/claim`), `fetch_account_email` |
| `coordinator.py` | Hot-Path: `DataUpdateCoordinator`-Loop, Entity-Reader, Frame-Dispatch (`_handle_ws_message`), Apply-Handler (`_apply_device_state`, `_apply_cool_state`, `_apply_battery_setpoint`), Hold-Loop-Spawning; Auth delegiert als dünne Wrapper an `self.api` (CrowdergyAuthSession) |
| `sse_client.py` | Reconnecting SSE-Listener auf `/api/v1/stream`, Bearer-Header (kein `?token=`), Queue (maxsize 512), Auth-Refresh-Callback, Half-Open-Detection (60-s-Read-Timeout), 401-Backoff + Reauth-Flow nach 5 Zyklen |
| `state_mirror.py` | `DeviceStateMirror`: active/on/cool-State + Hold-Tasks + `last_sse_event_at` |
| `telemetry_composer.py` | Background-Loops (Heartbeat, Device-Mirror mit eigenem `_last_mirror_at`, State-Resync), `bootstrap_active_state()`, `push_outdoor_temp()`; Mirror ist auf Telemetrie-Consent gegated |
| `device_field_spec.py` | SSOT für Device-Felder im create/update-Roundtrip (`build_payload(mode, …)`) |
| `preset_spec.py` | Slot-Schema für Crowd-Presets (public Teil des Store-Vertrags) |
| `config_flow.py` | Pairing-Code-Onboarding (`user`-Step) + Reauth per Code, 3-Step-Geräte-Anlage + Nominatim-Reverse-Geocoding + Import-Flow für Box-Provisioning; Options-Flow persistiert sofort |
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

## Config-Flow

- **Onboarding per Pairing-Code (ab v3.26.0, Email/Passwort komplett
  entfernt):** `user`-Step nimmt Code aus der Crowdergy-App (+ optionale
  API-URL für Self-Hosted, teil-erledigt Backlog #17), claimt via
  `claim_pairing_code` mit der HA-Instance-ID als `client_id`,
  `unique_id` = Backend-User-ID, Titel via best-effort `GET /users/me`
  (Fallback User-ID-Präfix). HA sieht nie Credentials; funktioniert auch
  für Sign-in-with-Apple-Accounts. Reauth (`reauth_confirm`) fragt einen
  frischen Code ab und lehnt Codes fremder Accounts ab
  (`reauth_account_mismatch`, fremde Tokens werden verworfen).
  Bestands-Entries aus der Login-Ära refreshen unverändert weiter.
- Geräte-Anlage: 3 Steps: (1) Typ + Name, (2) typ-spezifische Entities
  Read/Control, (3) `value_on`/`value_off` typ-bewusst. Edit-Felder mit
  `suggested_value`, nie `default=`.
- Standort via Nominatim-Reverse-Geocoding aus
  `hass.config.latitude/longitude`.
- `DEVICE_TYPES`: solar / battery / wallbox / grid / heating / warmwater /
  aircon / generic / haushalt. Crowdergize-fähig: battery / wallbox /
  heating / warmwater / aircon / generic.
- Brand-Trennung: UI „Crowdergy AI", Switch intern `crowdergize`
  (`CrowdergyActiveSwitch`).

## Energie-Zähler-Konvention (E2E, ab v3.21.3)

`entity_energy_total` = Bezug/Entladen-Zähler (device → home),
`entity_energy_discharged_total` = Einspeisung/Laden-Zähler. Telemetrie
schickt explizite `energy_kwh_in_delta` + `energy_kwh_out_delta` (ab
v3.21.4); das Backend deriviert den signed Wert.

## Backend-API (alle Bearer im `Authorization`-Header)

`POST /connector/claim` (unauth, Pairing-Code → Token-Paar; Fallback
`POST /box/claim` für alte Backends), `POST /auth/refresh`,
`GET /users/me` (Entry-Titel), `GET|POST /devices`,
`PUT|DELETE /devices/{id}`, `PATCH /devices/{id}/telemetry`,
`POST /devices/{id}/commands`, `GET /api/v1/stream` (SSE),
`POST /users/me/heartbeat`, `POST /users/me/outdoor`,
Crowd-Preset-Lookup/Contribute. Alle authentifizierten Calls laufen
durch `CrowdergyAuthSession.async_request` (401 → Single-Flight-Refresh
→ ein Retry). `POST /auth/login` wird nicht mehr benutzt.

## Crowdergy-Box-Integration (nur mit `theothergas:`-YAML-Key aktiv)

- **`provision_box`** (`__init__.py` + `provisioning.py` + Import-Flow):
  Token-Paar aus dem Box-Claim → Entry ohne UI; `unique_id` =
  Backend-User-ID, Re-Pairing aktualisiert Tokens statt Duplikat und
  merged Consent-Options im `already_configured`-Pfad. `consent_*`-Felder
  landen atomar als Entry-Options (kein Default-True-Fenster).
- **`box_services.py`**: `box_list_presets` (proxied approved
  Vendor-Preset-Lookup inkl. `integration_domain`, status, capabilities),
  `box_add_device` (Registrierung über `device_field_spec`; Value-Slots
  mit Punkt in Werten ab v3.24.0), `box_set_consent` (Entry-Options).
- **Consent-Gates:** ohne `consent_telemetry` kein Telemetrie-/
  Outdoor-Push; ohne `consent_remote_control` keine Steuer-Frames.
  Fehlende Flags = Alt-Verhalten (True).
- **Contribute** schickt `integration_domain` mit
  (`entity_mapper.dominant_integration_domain`); Store-Vertrag:
  `docs/crowd-preset-store.md` (Backend-Umsetzung offen, Backlog #9).
- httpx-Clients werden IMMER im Executor gebaut (synchroner CA-Load →
  HA-Blocking-Call-Warnung, live gefunden); ab v3.26.0 zentral in
  `api_client.py`. `_authenticated_config_request` in `config_flow.py`
  ist nur noch ein Alias auf `api_client.authenticated_request`.

## Tests (`tests/`)

pytest, Python ≥ 3.12. Pure-Logic-Unit-Tests: `test_device_field_spec`
(SSOT-Regression-Guard), `test_api_client` (Single-Flight/CAS,
401-retry-once, Claim-Fallback-Vertrag), `test_config_flow_pairing`
(user-Step + Reauth inkl. Account-Mismatch), `test_sse_client`,
`test_state_mirror`, `test_seconds_until_next_run`, `test_provisioning`,
`test_box_services`, `test_integration_domain`. 2 bekannte
aiodns-Drift-Errors (siehe `CLAUDE.md`). Coordinator-/Full-Flow-
Integration offen (Backlog #21).

## Offene Punkte

Siehe Backlog #16–#22 in `crowdergy-ios/CLAUDE.md` (Domain-Rename,
API-URL-Override, Telemetry-Retry, proaktives Token-Refresh,
HACS-Default-Index, FEAT-5-Rest, Kostal-Template). Außerdem:
Refresh-Tokens liegen im Klartext in `config_entries` (HA-Standard).

## Abhängigkeiten / Plattform

- **Backend** wird aufgerufen (s.o.); **iOS**: keine Direktverbindung
  (Daten via Backend-SSE-Broadcast); **Box** vendored dieses Repo per
  Git-Tag (`crowdergy-box/CONNECTOR_VERSION`, aktuell v3.25.0).
- `manifest.json`: Version `3.26.0`, Domain `theothergas`,
  `iot_class: cloud_push`, `requirements: ["httpx>=0.24.0"]`.
