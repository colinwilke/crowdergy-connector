# Connector-Tests (Cluster E, 2026-06-09)

Pytest-Setup für die HACS-Custom-Component, basierend auf
`pytest-homeassistant-custom-component`.

## Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r ../requirements-test.txt
pytest
```

(Python ≥3.12 ist HA-Pflicht.)

## Test-Strategie

Drei Schichten:

1. **Pure-Logic-Tests** (`test_device_field_spec.py`,
   `test_seconds_until_next_run.py`) — keine HA-Harness nötig.
   Wir testen Module die völlig stateless sind: `build_payload`,
   `_SOLVER_EXTRA_FIELDS`-Registry, Berechnungs-Helfer. Diese
   laufen auf jedem Python-3.12 ohne HA-Install.

2. **Coordinator-State-Tests** (TODO Sprint 2). Brauchen die
   `hass`-Fixture aus pytest-homeassistant-custom-component.
   Test-Beispiele:
   - `_should_send`-Hash-Dedup wirft nach 90 s nicht aus
   - state-resync schreibt bei drift gegen den Backend-Wert
   - hold-loop AUTO skipt bei state==expected (kein Service-Call)
   - hold-loop SSE-Stale-Bail (Cluster B-Fix)

3. **End-to-End-SSE-Tests** (TODO Sprint 3). Brauchen einen
   `aioresponses`-Stub für `/api/v1/stream` + Telemetry-Frames.

## Was NICHT getestet wird

- HA-Service-Calls (`hass.services.async_call(...)`) gegen echte
  Entities. pytest-homeassistant-custom-component bietet ein
  mock-Layer, das reicht für Logik-Tests.
- Modbus-Roundtrips (User-Side, KWR-Hub).
- HACS-Release-Mechanik (separat per Tag-Release).
