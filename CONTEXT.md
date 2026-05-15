# crowdergy-connector

## Stand: 2026-05-15

### Fertig (mit Dateinamen)
- **Integration-Setup** (`async_setup_entry` / `async_unload_entry`): `custom_components/theothergas/__init__.py`
- **Konstanten** (Domain, Default-API-URL, Config-Keys, Plattform-Liste, `CHARGE_MODE_OPTIONS`): `custom_components/theothergas/const.py`
- **Coordinator** (`DataUpdateCoordinator`-Subklasse, 30 s Heartbeat, State-Change-Listener, Telemetrie-Push, JWT-Refresh, Power-W→kW-Konvertierung, **SSE-Listener** auf `/api/v1/stream` mit reconnect/backoff, Command-Dispatch): `custom_components/theothergas/coordinator.py`
- **Sensor-Plattform** (`current_power_kw`, `soc_percent`, `vehicle_status`, `charge_mode`): `custom_components/theothergas/sensor.py`
- **Switch-Plattform** (`async_turn_on/off` → `toggle_active`-Command): `custom_components/theothergas/switch.py`
- **Config-Flow** (Login → Location → **zwei Schritte pro Gerät**: erst Typ+Name, dann typ-spezifische Entity-Auswahl; Options-Flow + Edit-Device-Flow in der gleichen Struktur): `custom_components/theothergas/config_flow.py`. Schemas pro Typ in `_TYPE_FIELDS`; Batterie/Wallbox-Formulare in zwei Sektionen "Leistungsdaten (nur lesend)" und "Steuerungsparameter (werden von Crowdergy regelmäßig gesetzt)"
- **Device-Registry-Mapping** (`solar|battery|wallbox|grid|heatpump|generic`): `custom_components/theothergas/device_registry.py`
- **Brand-Icons** lokal unter `custom_components/theothergas/brand/` (seit HA 2026.3 reicht das, kein PR an `home-assistant/brands` mehr nötig)
- **HACS-Manifest** (`hacs.json` mit `render_readme`, `homeassistant: 2024.6.0`, `country: DE`)
- **Device-Removal sauber HA-seitig**: `async_remove_config_entry_device` in `__init__.py` (HA-eigener Löschen-Knopf) plus `_remove_ha_device()` im Options-Flow-Remove-Pfad — beide löschen den DeviceRegistry-Eintrag, nicht nur die Crowdergy-DB
- **Release**: aktuell `v1.5.0` (Zwei-Schritt-Config-Flow mit typ-spezifischen Schemas, Sektions-Gruppierung, HA-Device-Cleanup beim Entfernen, Switch-Mapping für Sonstiges-Typ via `entity_is_active`)

### In Arbeit (was offen ist)
- Keine offenen `TODO`/`FIXME` im Code
- Pytest-homeassistant-Test-Suite noch nicht aufgesetzt (Tier 3 der Test-Roadmap)

### Bekannte Probleme / TODOs
- **Domain noch `theothergas`** (Legacy) — Migration auf `crowdergy` ausstehend (Manifest, `const.py`, Strings, Ordner-Rename → Breaking Change für bestehende Installs)
- **Default-API-URL hardcoded**: `DEFAULT_API_URL = "https://api.theothergas.de"` in `const.py` — Override nur über Config-Entry, kein UI-Feld
- **Kein Retry/Backoff** bei fehlgeschlagenem `PATCH /devices/{id}/telemetry` — Fehler nur geloggt, kein Re-Enqueue
- **Token-Refresh nur reaktiv** (auf 401) — kein proaktives Refresh vor Ablauf → Heartbeat kann fehlschlagen, wenn Token zwischen Intervallen abläuft
- **HACS**: Repo nicht im Default-Index — User müssen Custom-Repo manuell hinzufügen
- **Brand-Icon** liegt lokal im `brand/`-Ordner (HA 2026.3+ unterstützt das); ein PR an `home-assistant/brands` wurde geschlossen, weil nicht mehr nötig
- **Refresh-Tokens** stehen im Klartext in `config_entries` (HA-Standardpraxis, aber nicht ideal)
- **Keine Tests** — kein `tests/`-Verzeichnis

### Abhängigkeiten zu anderen Repos
- **Backend (crowdergy-backend) wird aufgerufen** an `https://api.theothergas.de` (überschreibbar):
  - `POST /api/v1/auth/login` — `{email, password}` → `{access_token, refresh_token, user_id}`
  - `POST /api/v1/auth/refresh` — `{refresh_token}` → neue Tokens
  - `POST /api/v1/devices` — `{name, type, district, city, region}` → Device-Objekt mit `id`
  - `DELETE /api/v1/devices/{id}`
  - **`PATCH /api/v1/devices/{id}/telemetry`** — Payload:
    ```json
    {
      "power_kw": float,        // 0.0 wenn Quelle null
      "is_online": true,        // immer true
      "is_active": bool,        // aus Entity oder Default true
      "soc_percent": float      // optional, nur Battery
    }
    ```
  - `POST /api/v1/devices/{id}/commands` — App→HA-Befehle: für `set_soc_min`/`set_soc_max`/`set_charge_mode`/`toggle_active`
  - **`GET /api/v1/stream?token=…`** (SSE) — empfängt downstream Command-Frames vom Backend (`{type:"command", action, device_id, value}`) und setzt sie auf HA-Entities um. Heartbeat: `{type:"ping"}` alle 15 s
  - Auth: Bearer-JWT im `Authorization`-Header (`access_token`)
- **iOS (crowdergy-ios):** keine direkte Verbindung — iOS bekommt Connector-Daten via Backend-SSE-Broadcast

### Plattform-Anforderungen (`manifest.json`)
- `homeassistant: 2024.6.0`
- `requirements: ["httpx>=0.24.0"]` (Coordinator nutzt zusätzlich `aiohttp` aus HA's `aiohttp_client` für den SSE-Stream)
- Domain: `theothergas` (Legacy-Name; siehe oben)
- Version: `1.4.0`
