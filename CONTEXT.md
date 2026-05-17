# crowdergy-connector

## Stand: 2026-05-16

### Fertig (mit Dateinamen)
- **Integration-Setup** (`async_setup_entry` / `async_unload_entry` / `async_remove_config_entry_device`): `custom_components/theothergas/__init__.py`. Removal löscht backend-seitig und droppt den HA-DeviceRegistry-Eintrag.
- **Konstanten** — `CONF_ENTITY_CONTROL`/`CONF_VALUE_ON`/`CONF_VALUE_OFF` für universellen On/Off-Pfad, `CONF_ENTITY_CHARGE_MODE` Wallbox-spezifisch, plus Read-Entities: `custom_components/theothergas/const.py`
- **Coordinator** (`DataUpdateCoordinator`, 30 s Heartbeat, State-Change-Listener, Telemetrie-Push, JWT-Refresh, **SSE-Listener** auf `/api/v1/stream`, Bootstrap von `_active_state` + `_on_state` via GET `/devices`): `custom_components/theothergas/coordinator.py`
  - `_apply_device_state(is_on)` — schreibt `entity_control` auf `value_on`/`value_off`; bei Switch/Light/Fan/Input-Boolean impliziter `turn_on`/`turn_off` ohne Werte, bei Number/Select/Climate typ-passende Service-Aufrufe
  - `_apply_charge_mode(mode)` — Wallbox-Lademodus → `entity_charge_mode` als select-option
  - Telemetrie-Mirror konsumiert `is_active` (Crowdergize-Cache + HA-Switch-Spiegel) und `is_on` (triggert `_apply_device_state`)
- **Sensor-Plattform** (`current_power_kw`, `soc_percent`, `vehicle_status`, `charge_mode`): `custom_components/theothergas/sensor.py`
- **Switch-Plattform** (`CrowdergyActiveSwitch` benannt "Crowdergize", nur für controllable Typen erzeugt): `custom_components/theothergas/switch.py`
- **Config-Flow** (Login → Location → drei Schritte pro Gerät): `custom_components/theothergas/config_flow.py`
  - Schritt 1: Typ + Name
  - Schritt 2: typ-spezifische Entities (Read-Section "Leistungsdaten (nur lesend)" + Control-Section "Steuerung (Crowdergize)"). Wallbox bekommt `entity_charge_mode` + `entity_control` parallel; andere controllable nur `entity_control`.
  - Schritt 3: `value_on`/`value_off` typ-bewusst — Select → Dropdown der Options, Number → NumberSelector min/max/step, Climate → hvac_modes-Dropdown, Switch/Light/Fan/Input-Boolean → übersprungen (implizite turn_on/off)
  - Standort-Step nutzt Nominatim-Reverse-Geocoding aus `hass.config.latitude/longitude` als Default
- **Device-Typen**: `solar / battery / wallbox / grid / heatpump / generic / haushalt`. Crowdergize-fähig: battery / wallbox / heatpump / generic.
- **Brand-Icons** lokal unter `custom_components/theothergas/brand/`
- **HACS-Manifest** (`hacs.json` mit `render_readme`, `homeassistant: 2024.6.0`, `country: DE`)
- **Release**: aktuell **`v1.12.0`** (Crowdergize-Consent + is_on, universal entity_control + value_on/off, Wallbox-Lademodus parallel, typ-aware Step 3, Switch-Skip, Nominatim-Auto-Fill, Haushalt-Typ)

### In Arbeit (was offen ist)
- Pytest-homeassistant-Test-Suite noch nicht aufgesetzt (Tier 3 der Test-Roadmap)
- Smart-Auto-Controller, der bei aktivem Crowdergize automatisch `set_device_state`-Commands fährt — kommt im Backend (Roadmap)

### Bekannte Probleme / TODOs
- **Domain noch `theothergas`** (Legacy) — Migration auf `crowdergy` ausstehend (Manifest, `const.py`, Strings, Ordner-Rename → Breaking Change für bestehende Installs)
- **Default-API-URL hardcoded**: `DEFAULT_API_URL = "https://api.theothergas.de"` in `const.py` — Override nur über Config-Entry, kein UI-Feld
- **Kein Retry/Backoff** bei fehlgeschlagenem `PATCH /devices/{id}/telemetry` — Fehler nur geloggt, kein Re-Enqueue
- **Token-Refresh nur reaktiv** (auf 401) — kein proaktives Refresh vor Ablauf
- **HACS**: Repo nicht im Default-Index — User müssen Custom-Repo manuell hinzufügen
- **Refresh-Tokens** stehen im Klartext in `config_entries` (HA-Standardpraxis, aber nicht ideal)
- **Keine Tests** — kein `tests/`-Verzeichnis

### Abhängigkeiten zu anderen Repos
- **Backend (crowdergy-backend) wird aufgerufen** an `https://api.theothergas.de` (überschreibbar):
  - `POST /api/v1/auth/login` → `{access_token, refresh_token, user_id}`
  - `POST /api/v1/auth/refresh` → neue Tokens
  - `GET /api/v1/devices` — Bootstrap der `is_active`/`is_on`-Caches beim Coordinator-Start
  - `POST /api/v1/devices` — Device-Anlegen
  - `PUT /api/v1/devices/{id}` — Typ/Name-Update beim Edit
  - `DELETE /api/v1/devices/{id}`
  - **`PATCH /api/v1/devices/{id}/telemetry`** — schreibt `power_kw`, `is_online`, `soc_percent`, `vehicle_status` (kein `is_active`/`is_on` mehr, das ist Backend-eigen)
  - **`GET /api/v1/stream?token=…`** (SSE) — empfängt Telemetrie-Mirror-Frames mit `is_active`/`is_on`/`charge_mode` und Command-Frames für `set_charge_mode`
  - Auth: Bearer-JWT im `Authorization`-Header
- **iOS (crowdergy-ios):** keine direkte Verbindung — iOS bekommt Connector-Daten via Backend-SSE-Broadcast

### Plattform-Anforderungen (`manifest.json`)
- `homeassistant: 2024.6.0`
- `requirements: ["httpx>=0.24.0"]` (Coordinator nutzt zusätzlich `aiohttp` aus HA's `aiohttp_client` für den SSE-Stream)
- Domain: `theothergas` (Legacy-Name; siehe oben)
- Version: `1.12.0`
