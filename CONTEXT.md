# crowdergy-connector

## Stand: 2026-06-11 — Release **v3.21.2**

HACS Custom-Component (Domain `theothergas`, Legacy-Name) für Home Assistant. Spiegelt Backend-Dispatch
in HA-Entities und pusht Telemetrie zurück. Zentrale Architektur-Doku: `crowdergy-ios/CLAUDE.md`.

### Modul-Struktur (`custom_components/theothergas/`)
Der früher monolithische `coordinator.py` wurde im **FEAT-5-Refactor (v3.10→v3.12)** schrittweise zerlegt:

| Datei | Verantwortung |
|---|---|
| `coordinator.py` | Hot-Path: Auth/Refresh, `DataUpdateCoordinator`-Loop, Entity-Reader, Frame-Dispatch (`_handle_ws_message`), Apply-Handler (`_apply_device_state`, `_apply_cool_state`, `_apply_battery_setpoint`), Hold-Loop-Spawning |
| `sse_client.py` (**v3.11.0**) | Reconnecting SSE-Listener auf `/api/v1/stream`, **Bearer-Header** (kein `?token=`), Queue (maxsize 512), Auth-Refresh-Callback, Half-Open-Detection (60-s-Read-Timeout) |
| `state_mirror.py` (**v3.10.0**) | `DeviceStateMirror`-Dataclass: active/on/cool-State + Hold-Tasks + `last_sse_event_at` |
| `telemetry_composer.py` (**v3.12.0**) | Background-Loops (Heartbeat, Device-Mirror, State-Resync), `bootstrap_active_state()`, `push_outdoor_temp()` |
| `device_field_spec.py` (**v3.9.0**) | **Single-Source-of-Truth** für Device-Felder im create/update-Roundtrip (`build_payload(mode, …)`) |
| `config_flow.py` | 3-Step-Onboarding pro Gerät + Nominatim-Reverse-Geocoding |
| `const.py` | Domain, `DEVICE_TYPES`, `CONF_ENTITY_*`-Keys, Intervalle |
| `sensor.py` / `switch.py` / `binary_sensor.py` | Plattformen |
| `device_registry.py` / `entity_mapper.py` | Device-Registry-Glue + Entity-Mapping-Helfer |

### Coordinator / Sync-Stack
- **SSE** (primär): `sse_client.py` füttert eine `asyncio.Queue`, Coordinator konsumiert per Background-Task.
  Half-Open-Erkennung über 60-s-Read-Timeout (Backend pingt alle 15 s → 4 verpasste Pings = Reconnect). **Seit v3.9.2 Frame-Logging.**
- **Frame-Dispatch** (`_handle_ws_message`): `type=telemetry` → `is_active`/`is_on`/`cool_on`/`vorlauf_setpoint_c`
  (manuelle App-Befehle landen hier); `type=command` → `set_charge_mode` (Battery/Wallbox); `ping` → noop
- **Battery-Dispatch** (`_apply_battery_setpoint`): schreibt `entity_battery_mode` (Select) +
  `entity_battery_power_setpoint` (Number), idempotent
- **Hold-Loops**: laufen seit **Cluster B / #11** als `hass.async_create_background_task` (nicht raw `asyncio.create_task`)
  → ordentliches HA-Shutdown. `entity_control` 30-s-Cadence, `entity_charge_mode` 15-s-Cadence; beide bailen bei SSE-Stale (>60 s).
- **Resync-Backstop**: periodisches `GET /devices` erkennt Drift nach SSE-Drop und re-applied
- **Heartbeat-Ping**: leichtgewichtiger `POST /me/heartbeat` für iOS-Connection-Dot

### Config-Flow
- 3 Steps: (1) Typ + Name, (2) typ-spezifische Entities Read/Control, (3) `value_on`/`value_off` typ-bewusst
- **`vol.Optional(key, description={"suggested_value": …})`**, NICHT `default=` (sonst Felder unlöschbar, HA re-injected)
- Standort via Nominatim-Reverse-Geocoding aus `hass.config.latitude/longitude`
- `DEVICE_TYPES`: solar / battery / wallbox / grid / heating / warmwater / aircon / generic / haushalt.
  Crowdergize-fähig: battery / wallbox / heating / warmwater / aircon / generic
- **Brand-Trennung**: UI sagt **Crowdergy AI**, Switch intern `crowdergize` (`CrowdergyActiveSwitch`)

### Backend-API (alle Bearer im `Authorization`-Header)
`POST /auth/login`, `POST /auth/refresh`, `GET|POST /devices`, `PUT|DELETE /devices/{id}`,
`PATCH /devices/{id}/telemetry`, `POST /devices/{id}/commands`, **`GET /api/v1/stream` (SSE, Bearer-Header)**,
`POST /users/me/heartbeat`, `POST /users/me/outdoor`. Auth-Wrapper refresht bei 401 und retried einmal.

### Tests (`tests/`, **Cluster E v3.10.0+**)
pytest-Setup (`conftest.py`) mit Pure-Logic-Unit-Tests: `test_device_field_spec` (SSOT-Regression-Guard),
`test_sse_client` (Queue/Backpressure/Token-Callback), `test_state_mirror`, `test_seconds_until_next_run`.
Coordinator-/Full-Flow-Integration noch offen.

### Recent Changes (v3.9.2 → v3.21.2)
- **v3.21.2** (2026-06-11, getaggt + GitHub-Release live): Crowd-Contribution sendet
  `integration_domain` mit — leitet Domain aus erstem gemappten Entity via HA-Entity-Registry
  → `ConfigEntry.domain` ab. Vorher landeten alle Submissions mit `NULL` und wurden vom
  Box-Manager `SUPPORTED_INTEGRATIONS`-Filter rejected (Colins KOSTAL-Solar-Submission war
  betroffen — auf prod backfilled).
- v3.17–v3.21.1 — Box-Phase (provision_box / box_services / consent-Gates) — siehe Box-Sektion unten
- **v3.9.2** SSE Half-Open-Detection (60-s-Timeout) + Frame-Logging
- **v3.10.0** FEAT-5 Phase A: `DeviceStateMirror`-Extraktion + Test-Skeleton
- **v3.10.1** Call-Site-Migration auf State-Mirror
- **v3.11.0** FEAT-5 Phase B: SSE-Client in eigenes Modul
- **v3.12.0** FEAT-5 Phase D Step 1: Telemetry-Composer extrahiert
- Cluster B: Hold-Loops als HA-Background-Tasks (#11), Wallbox-AI-off-Cleanup, DELETE via Coordinator

### Crowdergy Box (v3.17–v3.21.1, 2026-06-10, Branch claude/trusting-planck-4f9txj)
Die Box (Repo `crowdergy-box`) provisioniert den Connector headless. Alle
Box-Pfade sind nur aktiv, wenn `theothergas:` per YAML geladen ist —
normale HACS-Installationen unverändert.
- **`provision_box`** (`__init__.py` + `provisioning.py` + Import-Flow in
  `config_flow.py`): Token-Paar aus dem Box-Claim → Entry ohne UI;
  unique_id = Backend-User-ID, Re-Pairing aktualisiert Tokens statt
  Duplikat. Optionale `consent_*`-Felder landen **atomar** als
  Entry-Options (kein Default-True-Fenster).
- **`box_services.py`**: `box_list_presets` (proxied approved
  Vendor-Preset-Lookup inkl. `integration_domain`), `box_add_device`
  (Registrierung über `device_field_spec`, blockt camera/person/
  device_tracker/media_player), `box_set_consent` (Entry-Options).
- **Consent-Gates im Coordinator**: ohne `consent_telemetry` kein
  Telemetrie-/Outdoor-Push (`_async_update_data` Early-Return); ohne
  `consent_remote_control` werden eingehende Steuer-Frames ignoriert
  (`_handle_ws_message`). Default True (Flags fehlen) = Alt-Verhalten.
- **Contribute** schickt `integration_domain` mit
  (`entity_mapper.dominant_integration_domain`, Registry → Config-Entry).
- v3.21.1: `_authenticated_config_request` baut den httpx-Client im
  Executor (Blocking-Call-Warnung in HA 2026.5.4, live gefunden).
- Tests: `tests/test_provisioning.py`, `test_box_services.py`,
  `test_integration_domain.py` (HA-Harness; braucht Python 3.12+).
- **TODO: Tag `v3.21.1` nach Merge anlegen** — die Box pinnt per Git-Tag
  (`crowdergy-box/CONNECTOR_VERSION` + `scripts/vendor-connector.sh`).

### Bekannte Probleme / TODOs
- **Domain noch `theothergas`** (Legacy) — Migration auf `crowdergy` ausstehend (Breaking Change: Manifest,
  `const.py`, Strings, Ordner-Rename)
- **Default-API-URL hardcoded** `https://api.theothergas.de` in `const.py` (Override nur über Config-Entry)
- Kein Retry/Backoff bei fehlgeschlagenem `PATCH …/telemetry` (nur geloggt)
- Token-Refresh nur reaktiv (auf 401), kein proaktives Refresh vor Ablauf
- Refresh-Tokens im Klartext in `config_entries` (HA-Standard)
- HACS-Repo nicht im Default-Index — User müssen Custom-Repo manuell hinzufügen
- FEAT-5 Phase C/D noch nicht abgeschlossen (weitere Coordinator-Entflechtung offen)

### Abhängigkeiten / Plattform
- **Backend (crowdergy-backend)** wird aufgerufen (s.o.); **iOS**: keine Direktverbindung — iOS bekommt
  Connector-Daten via Backend-SSE-Broadcast
- `manifest.json`: Version `3.12.0`, Domain `theothergas`, `iot_class: cloud_push`, `requirements: ["httpx>=0.24.0"]`
  (aiohttp seit FEAT-5 Phase B entfallen)
