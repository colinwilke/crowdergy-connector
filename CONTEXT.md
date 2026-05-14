# crowdergy-connector

## Stand: 2026-05-14

### Fertig (mit Dateinamen)
- **Integration-Setup** (`async_setup_entry` / `async_unload_entry`): `custom_components/theothergas/__init__.py`
- **Konstanten** (Domain, Default-API-URL, Config-Keys, Plattform-Liste): `custom_components/theothergas/const.py`
- **Coordinator** (`DataUpdateCoordinator`-Subklasse, 60 s Heartbeat, State-Change-Listener, Telemetrie-Push, JWT-Refresh, Power-W→kW-Konvertierung, Command-Dispatch): `custom_components/theothergas/coordinator.py`
- **Sensor-Plattform** (`current_power_kw` mit `SensorDeviceClass.POWER`; `soc_percent` mit `SensorDeviceClass.BATTERY` für Batterien): `custom_components/theothergas/sensor.py`
- **Switch-Plattform** (`TheOtherGasActiveSwitch` mit `async_turn_on/off` → `set_active`-Command): `custom_components/theothergas/switch.py`
- **Config-Flow** (3 Schritte + Device-Loop: Login → Location → Device-Add; Options-Flow zum nachträglichen Hinzufügen/Entfernen): `custom_components/theothergas/config_flow.py`
- **Device-Registry-Mapping** (`solar|battery|wallbox|grid|heatpump|generic`): `custom_components/theothergas/device_registry.py`
- **HACS-Manifest** (`hacs.json` mit `render_readme`, `homeassistant: 2024.6.0`, `country: DE`)
- **Release**: Tag `v1.0.0` (initialer Release + HACS-Vorbereitung)

### In Arbeit (was offen ist)
- Keine offenen `TODO`/`FIXME` im Code; Funktionsumfang für v1 vollständig
- Command-Set begrenzt auf `set_active` — `set_flex` / weitere Steuerbefehle noch nicht implementiert

### Bekannte Probleme / TODOs
- **Domain noch `theothergas`** (Legacy) — Migration auf `crowdergy` ausstehend (Manifest, `const.py`, Strings, Ordner-Rename → Breaking Change für bestehende Installs)
- **Default-API-URL hardcoded**: `DEFAULT_API_URL = "https://api.theothergas.de"` in `const.py` — Override nur über Config-Entry, kein UI-Feld
- **Kein Retry/Backoff** bei fehlgeschlagenem `PATCH /devices/{id}/telemetry` — Fehler nur geloggt, kein Re-Enqueue
- **Token-Refresh nur reaktiv** (auf 401) — kein proaktives Refresh vor Ablauf → Heartbeat kann fehlschlagen, wenn Token zwischen Intervallen abläuft
- **HACS**: Repo nicht im Default-Index — User müssen Custom-Repo manuell hinzufügen
- **Brand-Icon** nicht in `home-assistant/brands`-Repo eingereicht → kein Icon in HA-UI/HACS
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
  - `POST /api/v1/devices/{id}/commands` — `{action: "set_active", value: bool}`
  - Auth: Bearer-JWT im `Authorization`-Header (`access_token`)
- **iOS (crowdergy-ios):** keine direkte Verbindung — iOS bekommt Connector-Daten via Backend-WS-Broadcast

### Plattform-Anforderungen (`manifest.json`)
- `homeassistant: 2024.6.0`
- `requirements: ["httpx>=0.24.0"]`
- Domain: `theothergas` (Legacy-Name; siehe oben)
- Version: `1.0.0`
