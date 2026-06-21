# crowdergy-connector — Projekt-Memory

> **Standing Rule (User-Vorgabe 2026-06-10): `CONTEXT.md` (Detail-Stand)
> und diese Datei am Ende JEDER Arbeitssitzung automatisch aktualisieren.**

Detaillierter Stand: `CONTEXT.md`. Multi-Repo-Index, Vereinbarungen und der
**projektweite Backlog (SSOT)**: `crowdergy-ios/CLAUDE.md`. Connector-Items
dort: Cluster C (#16–#22).

## Repo-Regeln & getroffene Entscheidungen

- **SSOT-Regeln (immer einhalten):**
  - Neue Backend-Device-Felder NUR in `device_field_spec.py` (Roundtrip
    create/update), nie direkt in `_register_device`/`_update_device_backend`.
  - Neue Mapping-Slots NUR in der Allowlist `MAPPABLE_ENTITY_DOMAINS`
    (Default-DENY; Read-Slots nur sensor/binary_sensor, Control-Slots nur
    schreibbare Domains).
  - `CONTROLLABLE_TYPES` in `const.py` ist SSOT für steuerbare Typen.
  - Preset-Slots (was in einen Crowd-Preset-Beitrag gehört) NUR in
    `preset_spec.PRESET_SLOT_SPEC`; Entity-Slots dort müssen in
    `MAPPABLE_ENTITY_DOMAINS` stehen (Test-gesichert). Vertrag:
    `docs/crowd-preset-store.md`.
  - **Entity-Picker-Mess-Typ (#46):** `_ENTITY_SELECTORS` ist SSOT dafür,
    WAS ein Slot anbietet — seit dem #50-Split in `config_flow_schemas.py`
    definiert und aus `config_flow.py` re-exportiert (Zugriffspfad
    `config_flow._ENTITY_SELECTORS` bleibt gültig). Eindeutige
    Read-Slots tragen einen `device_class`-Filter (power/energy/battery/
    temperature) → kW/kWh-Typsicherheit (Power-Slot zeigt nur W/kW,
    kWh-Zähler-Slot nur Energie). HA-Form: flacher `device_class=`-kwarg
    neben `domain` in `EntitySelectorConfig`. **NUR an eindeutigen
    Read-Slots** — NIE an Multi-Domain-/Control-Slots (climate/
    water_heater tragen keine sensor-device_class → würden ausgeblendet;
    `CONF_ENTITY_CURRENT_TEMP` & Co. bewusst ungefiltert). Hard-Filter:
    Entities ohne passende device_class werden versteckt (ok, moderne
    Integrationen inkl. kostal_plenticore setzen sie).
- **Consent-Semantik (entschieden):** Telemetrie-Consent gated NUR
  Energiedaten. Liveness-Traffic (Heartbeat/Version/Device-Polling) ist
  bewusst NICHT gegated — dokumentiert in `services.yaml` +
  `telemetry_composer`. Remote-Control-Consent wird ZENTRAL in allen
  `_apply_*` geprüft (inkl. Resync/Self-Heal/Hold).
- **Box-Services nur mit `theothergas:`-YAML-Key** (bewusst Breaking für
  Self-Hosted ohne Key). Normale HACS-Installationen sind von allen
  Box-Pfaden unberührt.
- **Wallbox:** Pre-AI-Lademodus wird bei AI-OFF NICHT restauriert
  (Restore-Pfad entfernt, Backend-Spalte existiert nicht mehr).
- **Wallbox variabler Ladestrom (User 2026-06-20, Branch
  `claude/laughing-turing-4tiimd`, analog Batterie-Power-Setpoint):**
  Optionale Number-Entity `CONF_ENTITY_WALLBOX_CHARGE_CURRENT`
  (`entity_wallbox_charge_current_a`, Ampere 6–16) im Wallbox-
  Control-Step (`config_flow_schemas._entities_schema` + `_ENTITY_SELECTORS`,
  domain number/input_number, KEIN device_class-Filter — Control-Slot).
  Mappt der User sie, leitet `device_field_spec._compute_supports_charge_current`
  daraus das Bool `wallbox_supports_charge_current` ab und sendet es im
  create/update-Roundtrip (Entity-ID selbst bleibt Connector-lokal); das
  Backend lässt den Solver dann den Strom variabel wählen. Dispatch
  (`command_dispatcher`): das `set_charge_mode`-Command trägt im
  **„An"/Power-Modus** zusätzlich `current_a` — `_apply_charge_mode`
  schreibt den Strom (`number.set_value`) ZUERST, dann den Modus-Select
  (analog Battery: Setpoint vor Modus). `state.held_charge_current` cached
  ihn, die `_charge_mode_hold_loop` re-schreibt Strom+Modus zusammen.
  **Solar/Lock tragen nie einen Strom; Solarmode unverändert.** Leer =
  „An" = volle Leistung (altes Verhalten). Preset-Slot
  `entity_wallbox_charge_current_a` (optional). Tests:
  `test_sync_stack_characterization` (Dispatch forwards current),
  `test_hold_loops_and_eviction` (write-current-before-mode, held-current
  re-write, Solar clears held current). Backend-Seite: PR backend (gleiche
  Branch).
- **#21 Coordinator-Entflechtung — Auth-Cluster NICHT extrahieren (durable
  Lehre, PR connector#16):** die Auth/HTTP-Methoden (`_authenticated_request`,
  `_patch_telemetry_with_retry`, `_refresh_access_token`, `_jwt_exp`) bleiben
  in `coordinator.py` — die Tests patchen `coordinator.asyncio`/importieren
  `_jwt_exp` per **Modul-Attribut**; ein Verschieben in ein eigenes Modul
  bräche die Mocks für marginalen Lesbarkeitsgewinn. Erst Full-Flow-
  Integrationstests (`test_async_update_data_roundtrip.py` — Entity-Read→
  Payload→Send→Energie-Bookkeeping; die Non-Regression-Harness, die VOR der
  Entflechtung steht) sind gelandet; die Extraktion selbst bleibt offen.
  `house-consumption-chart.md` trägt jetzt `heatpump_total` (#43, Backend-
  Vertrag — kein Connector-Code, Slot-Schema deckt die Thermal-Typen seit
  #68).
- **Config-Flow Edit-Felder:** `vol.Optional(..., description=
  {"suggested_value": ...})`, NIE `default=` (HA re-injected, Felder
  werden unlöschbar).
- **Entry-Schema-Regel — [Alpha: gelockert, 2026-06-15]:** In der Alpha
  dürfen Entry-data/-options breaking ändern (Test-User
  re-provisionieren); KEINE „Feld fehlt = Altverhalten"-Load-Time-
  Migration mehr nötig. Die alte Stabilitätsgarantie (additiv/migrierbar,
  Pin-Bump ohne Re-Provisionierung) greift erst mit echten Prod-Usern
  wieder.
- **Release-Prozess:** Manifest-Bump + Tag passieren **nur beim Release
  auf `main`**, durch den, der das Tag schneidet — NIE auf
  Feature-Branches (sonst Versions-Kollision: mehrere Branches belegen
  dieselbe Nummer für verschiedene Arbeit). GitHub-Release via
  `tag-release.yml` bzw. User; vor dem Taggen prüfen, ob der Tag auf
  origin schon belegt ist. Aktuelle Version: `manifest.json`; Release-
  „Stand": SSOT `crowdergy-ios/CLAUDE.md`.
- **Public-Repo-Disziplin:** Dieses Repo ist public (HACS =
  Contribute-Kanal für den Crowd-Preset-Store). Hier liegt nur das
  Slot-SCHEMA (`preset_spec.py`); Store-Daten/Kuration bleiben im Backend
  hinter Auth, Box-Know-how bleibt im privaten Box-Repo.

## Tests

Nur mit **Python ≥ 3.12** (`requirements-test.txt`-Kopf).

**Remote-Session (Claude Code on the web):** der SessionStart-Hook
(`.claude/hooks/session-start.sh`) baut `.venv` (Python 3.12,
`requirements-test.txt`). Tests dann via `.venv/bin/pytest`.
`tests/test_sse_client.py::test_start_is_idempotent` und
`test_stop_cancels_running_task` haben einen bekannten
umgebungsabhängigen Error (aiodns/aiohttp-Drift in frischen venvs) — auf
sauberem Stand gegenprüfen, bevor man eigene Änderungen verdächtigt.

## Agent-Ownership (Interferenz-Schutz, User-Vorgabe 2026-06-10)

- **Schreib-Ownership dieses Repos: die Remote-/Web-Session** (zusammen mit
  backend + box). Der lokale Mac-/Xcode-Agent besitzt `crowdergy-ios`.
- Fremde Agents: dieses Repo LESEN ja (API-Verträge), schreiben nein —
  Ausnahme: CLAUDE.md/CONTEXT.md-Memory-Updates.
- **Trunk-based (User 2026-06-11; geschärft 2026-06-15):** Owner-Agent
  pusht klein + grün direkt auf `main` (Tests grün). `claude/...`-Branches
  nur für Riskantes/Experimentelles und **kurzlebig** (same-session
  mergen/löschen). Handoff-Fenster: bei fremder Release-Übernahme komplett
  stillstehen. Voller Workflow + Parallel-Agent-/Alpha-Regeln: SSOT
  `crowdergy-ios/CLAUDE.md` (Abschnitte „Agent-Ownership & Branches" +
  „Alpha-Phase").
