# crowdergy-connector — Projekt-Memory

> **Standing Rule (User-Vorgabe): `CONTEXT.md` (Detail-Stand) und diese Datei
> am Ende JEDER Arbeitssitzung aktualisieren.**

Detaillierter Stand: `CONTEXT.md`. Multi-Repo-Index, Vereinbarungen und der
**projektweite Backlog (SSOT)**: `crowdergy-ios/CLAUDE.md`. Connector-Items
dort: Cluster C (#16–#22).

## SSOT-Regeln (immer einhalten)

- **Neue Backend-Device-Felder NUR in `device_field_spec.py`** (Roundtrip
  create/update), nie direkt in `_register_device`/`_update_device_backend`.
- **Connector-lokale Entity-Slots** (gehen NICHT ans Backend, nur HA-Scope)
  **MÜSSEN zusätzlich in `_build_device_record` (`config_flow_mapping.py`)**
  stehen — die Allowlist, die die Entity-Map auf den Config-Entry persistiert.
  Fehlt der Key, wird er beim Submit STUMM verworfen (Re-Open weg, Dispatcher
  liest leer). **Regel: Schema-Touch ⇒ immer auch `_build_device_record` +
  Round-Trip-Test (`test_build_device_record_persists_*`).**
- **Neue Mapping-Slots NUR in der Allowlist `MAPPABLE_ENTITY_DOMAINS`**
  (Default-DENY; Read-Slots nur sensor/binary_sensor, Control-Slots nur
  schreibbare Domains).
- `CONTROLLABLE_TYPES` in `const.py` = SSOT für steuerbare Typen.
- **Preset-Slots NUR in `preset_spec.PRESET_SLOT_SPEC`**; Entity-Slots dort
  müssen in `MAPPABLE_ENTITY_DOMAINS` stehen (Test-gesichert). Vertrag:
  `docs/crowd-preset-store.md`.
- **Entity-Picker-Mess-Typ (#46):** `_ENTITY_SELECTORS` ist SSOT, WAS ein Slot
  anbietet — in `config_flow_schemas.py` definiert, aus `config_flow.py`
  re-exportiert (`config_flow._ENTITY_SELECTORS` bleibt gültig). Eindeutige
  Read-Slots tragen `device_class`-Filter (power/energy/battery/temperature) →
  kW/kWh-Typsicherheit (flacher `device_class=`-kwarg neben `domain` in
  `EntitySelectorConfig`). **NUR an eindeutigen Read-Slots** — NIE an
  Multi-Domain-/Control-Slots (climate/water_heater tragen keine
  sensor-device_class → würden hart ausgeblendet).

## Entscheidungen (gelten weiter)

- **Consent-Semantik:** Telemetrie-Consent gated NUR Energiedaten.
  Liveness-Traffic (Heartbeat/Version/Device-Polling) ist bewusst NICHT gegated
  (dokumentiert in `services.yaml` + `telemetry_composer`). Remote-Control-
  Consent wird ZENTRAL in allen `_apply_*` geprüft (inkl. Resync/Self-Heal/Hold).
- **Box-Services nur mit `theothergas:`-YAML-Key** (bewusst Breaking für
  Self-Hosted ohne Key). Normale HACS-Installationen sind unberührt.
- **WP-Temperatur-Modus (Wärmepumpen NIE hart an/aus — nur gemappte
  Min-/Max-Zieltemperaturen):** heating/warmwater mit climate-/water_heater-
  `entity_control` und NUMERISCHEN value_on/value_off schreiben
  `set_temperature(value_on)` bei AN (Max) / `(value_off)` bei AUS (Min); KEIN
  `set_hvac_mode("off")`. Erkennung `is_temperature_control(domain, raw_value)`
  (`const.py`; HVAC-Modi nie numerisch → kollisionsfrei, Legacy-Modus-Strings
  unverändert). Idempotenz-Guard + Hold-Drift-Repair vergleichen das
  `temperature`-ATTRIBUT, nie `state.state` (`_control_actual_state`; sonst
  Write-Storm). `_read_is_on_state` mappt Max→True/Min→False/fremd→None.
  Config-Flow: heating/warmwater + climate/water_heater → °C-NumberSelector
  (aircon bewusst Modus-basiert). Keine neuen Slots. **Haftungsausschluss**
  prominent (README-Top + user-/device_values-Step-Descriptions).
- **HA-Entity-Generierung entschlackt:** kein eigener `sensor`-Platform-Mirror
  (`sensor` aus `PLATFORMS`). BEHALTEN: per-Gerät **`switch` „Crowdergy AI"**
  (POSTet `toggle_active`, spiegelt iOS-Toggle) + integration-weites
  **`binary_sensor.crowdergy_connected`** (SSE-Liveness). Reine Sicht-/Bedien-
  Schicht, kein Telemetrie-/Dispatch-Bezug.
- **EIN „Crowdergy"-Hub-Gerät statt per-Gerät-Dubletten:**
  `get_hub_device_info(entry)` (`identifiers={(DOMAIN, entry_id)}`, name
  „Crowdergy"). Alle „Crowdergy AI"-Switches + der binary_sensor hängen daran.
  Naming-Falle: mit geteiltem Hub-Device `_attr_has_entity_name=False` +
  `_attr_name = f"Crowdergy AI: {device_name}"` (sonst keine Disambiguierung);
  `suggested_object_id` stabil.
- **Card-Delete unter dem Hub-Modell:** `async_remove_config_entry_device` darf
  NIE aus einer Karten-Löschung ein Backend-Device ableiten (Karte ≠ 1:1
  Gerät). Hub-Karte → `return False`; andere `(DOMAIN,…)`-Leiche → `return True`
  OHNE Backend-Delete; `_prune_legacy_device_cards` detacht die leeren
  Alt-Karten. **Echtes Löschen NUR über den Options-Flow `remove_device`**
  (Backend-DELETE + `_remove_ha_device` + reload).
- **Wallbox:** Pre-AI-Lademodus wird bei AI-OFF NICHT restauriert.
- **Wallbox variabler Ladestrom:** optionale Number-Entity
  `entity_wallbox_charge_current_a` (Ampere 6–16, Control-Slot, KEIN
  device_class-Filter). Ist sie gemappt, leitet
  `device_field_spec._compute_supports_charge_current` das Bool
  `wallbox_supports_charge_current` ab. Dispatch: `set_charge_mode` trägt im
  „An"/Power-Modus `current_a`; `_apply_charge_mode` schreibt Strom
  (`number.set_value`) ZUERST, dann Modus-Select (analog Battery: Setpoint vor
  Modus); der Hold-Loop re-schreibt beide zusammen. Solar/Lock tragen nie Strom.
- **Uniform control-capability `control_entities_mapped`:** EIN Bool für ALLE
  steuerbaren Typen (iOS „Steuerbar"/„Nur lesend"), aus der Präsenz der
  Steuer-Entity, die der `_apply_*`-Guard braucht: wallbox
  `entity_charge_mode`; battery `entity_battery_mode` + Aktiv/Passiv-Werte
  (Setpoint ALLEIN reicht NICHT); heating/warmwater/generic `entity_control`;
  aircon `entity_control` ODER `entity_cool_control`
  (`device_field_spec._compute_is_controllable`). Nur das Bool geht ans Backend.
- **Auto-Discovery deaktiviert (nicht gelöscht):** `async_step_location` routet
  direkt in `async_step_device_type` + pinnt `setup_mode=manual`. Die
  setup_mode/auto_discover/auto_confirm-Steps bleiben als toter, reaktivierbarer
  Code.
- **`required_integrations`:** Contribute-Payload trägt zusätzlich zur
  häufigsten Domain (`dominant_integration_domain`) ALLE distinkten
  Integrationen der entity_map (`entity_mapper.required_integration_domains`).
  Profil-Picker labelt „· benötigt: <Klarname>" (Klarnamen in
  `entity_mapper.INTEGRATION_DISPLAY_NAMES` — **neue dort ergänzen**,
  Slug-Fallback ist sicher aber hässlich).
- **`required_helpers` — DURABLE LEHRE: der Connector kann `input_*`-Helfer
  NICHT selbst anlegen** (läuft in HA, die Storage-Collection ist von innen
  nicht abrufbar). Produce: `entity_mapper.required_helper_specs(hass,
  entity_map)` leitet je Slot auf einen `input_*`-Helfer die Spec aus der
  Live-HA-Config ab (native select/number sind KEINE Helfer). Apply: KEIN
  Fake-Anlegen — Profil-Picker labelt „· HA-Helfer nötig: …", Slot-IDs
  vorbefüllt, User legt 1× an. **Die BOX legt sie automatisch an (WS-Collection
  `<domain>/create`). Regel: HA-Helfer programmatisch anlegen geht nur von
  AUSSEN (Box/WS), NICHT aus einem HACS-Component.**
- **Coordinator-Entflechtung (#21):** Auth/HTTP-Cluster bleibt in
  `coordinator.py` — Tests patchen `coordinator.asyncio`/importieren `_jwt_exp`
  per Modul-Attribut. read/compose/decide-Helfer + Send-/Extra-Konstanten leben
  in `telemetry_reader.TelemetryReaderMixin` (Mixin wie `CommandDispatcherMixin`;
  `self` bleibt der Coordinator). Consent-Gates + `_async_update_data` bewusst
  NICHT mitgezogen. **Regel: neue Reader → `TelemetryReaderMixin`; neue
  Konstante gehört ins Modul, dessen Code sie liest, + Re-Export falls ein Test
  sie als `coordinator.<NAME>` importiert** (Loop-Konstanten analog in
  `telemetry_composer.py`; Zugriffspfad bleibt via Re-Export gültig).
- **Per-Tick State-Prefetch (#56):** alle `_read_*`-Reader lesen über
  `_get_state(entity_id)`, nie `self.hass.states.get` direkt.
  `_async_update_data` snapshottet je Gerät einmal `_prefetch_device_states`
  → `self._state_cache` (Slots aus `_PREFETCH_SLOT_KEYS` + `_SOLVER_EXTRA_
  FIELDS`), sodass mehrfach referenzierte Entities nur 1× gelesen werden.
  **Strikt auf die synchrone Lese-Phase begrenzt** (Cache vor erstem `await`
  wieder `None` → kein cross-coroutine-Bleed). **Regel: neue Reader immer
  `_get_state`; neue gelesene Slots in `_PREFETCH_SLOT_KEYS`.**
- **State-Watch (#98):** `_build_entity_map` registriert auch
  `CONF_ENTITY_CHARGE_MODE` + `CONF_ENTITY_COOL_CONTROL` → manueller HA-Flip
  propagiert via Event-Refresh (≤5 s). **Regel: jeder Slot, den der Per-Tick-
  Read zurück ans Backend spiegelt, gehört in die `_build_entity_map`-Key-Liste.**
- **UI-Text-Stil:** Config-Flow-Texte fachlich statt technisch + kurz; jede
  Seite beginnt mit 1–2 Sätzen WOFÜR. Schaltwerte-Seiten (`*_values`) tragen
  den Ziel-Temperatur-Tipp (An = höchste, Aus = niedrigste). Vendor-Presets
  heißen user-facing **„Geräteprofil"**, `entity_control_hold` **„Befehl
  wiederholen"**. **Regel: `strings.json` (EN) und `translations/de.json` IMMER
  synchron** (gleiche Keys + Placeholders); deutsche Hilfetexte tragen
  typografische „…" (NICHT gerade `"` ohne Escape).
- **Brand-Icon licht/dunkel-tauglich:** `brand/icon.png` (256) + `icon@2x.png`
  (512) = abgerundete Kachel (transparente Ecken, dezenter Rand), nie ein
  randloses Vollflächen-Quadrat. Das in HA sichtbare Icon serviert
  brands.home-assistant.io aus dem `brands`-Fork
  (`custom_integrations/theothergas/`) — neue PNGs dorthin + Upstream-PR
  (User-/Mac-Hand).
- **Config-Flow Edit-Felder:** `vol.Optional(..., description={"suggested_
  value": ...})`, NIE `default=` (HA re-injected → Felder unlöschbar).
- **API-URL NICHT im interaktiven Setup-Flow:** der `user`-Step zeigt kein
  API-URL-Feld — HACS pairt immer gegen `DEFAULT_API_URL`. `CONF_API_URL`
  bleibt intern (Entry-`data` = `DEFAULT_API_URL`) → nicht entfernen.
  Self-Hosted/Dev-Backends nur über den headless `provision_box`-Pfad
  (`async_step_import` + `provisioning.validate_provision_data` akzeptieren
  optional `api_url`).
- **Entry-Schema [Alpha: gelockert]:** Entry-data/-options dürfen breaking
  ändern (Test-User re-provisionieren); keine Load-Time-Migration nötig. Alte
  Stabilitätsgarantie greift erst mit echten Prod-Usern.
- **Release-Prozess:** Manifest-Bump + Tag NUR beim Release auf `main`, durch
  den, der das Tag schneidet — NIE auf Feature-Branches (Versions-Kollision).
  GitHub-Release via `tag-release.yml` bzw. User; vor dem Taggen prüfen, ob der
  Tag auf origin schon belegt ist. Aktuelle Version: `manifest.json`.
- **Public-Repo-Disziplin:** Dieses Repo ist public (HACS = Contribute-Kanal
  für den Crowd-Preset-Store). Hier liegt nur das Slot-SCHEMA
  (`preset_spec.py`); Store-Daten/Kuration bleiben im Backend hinter Auth,
  Box-Know-how im privaten Box-Repo.

## Tests

Nur mit **Python ≥ 3.12** (`requirements-test.txt`-Kopf).

- **JSON-Assets hart validieren:** ein nicht escaptes `"` in `de.json` ließ HA
  mit `orjson.JSONDecodeError` abbrechen (Hassfest fängt das NICHT).
  `tests/test_json_assets.py` parst jede ausgelieferte `*.json`.
- **CI-Trigger-Modell:** `test.yml` läuft NICHT auf `push→main`. Der **Mac ist
  Default-Test-Runner** über `scripts/githooks/pre-push` (einmalig aktivieren:
  `git config core.hooksPath scripts/githooks`; Notausgang `git push
  --no-verify`). GitHub-CI feuert nur auf **PRs** + nächtlichem `schedule`-
  Backstop auf `main` (04:17 UTC).
- **Remote-Session (Claude Code on the web):** der SessionStart-Hook
  (`.claude/hooks/session-start.sh`) baut `.venv` (Python 3.12). Tests via
  `.venv/bin/pytest`. `tests/test_sse_client.py::test_start_is_idempotent` und
  `test_stop_cancels_running_task` haben einen bekannten umgebungsabhängigen
  Error (aiodns/aiohttp-Drift in frischen venvs) — auf sauberem Stand
  gegenprüfen, bevor man eigene Änderungen verdächtigt.

## Agent-Ownership (Interferenz-Schutz)

- **Schreib-Ownership dieses Repos: die Remote-/Web-Session** (zusammen mit
  backend + box). Der lokale Mac-/Xcode-Agent besitzt `crowdergy-ios`.
- Fremde Agents: dieses Repo LESEN ja (API-Verträge), schreiben nein —
  Ausnahme: CLAUDE.md/CONTEXT.md-Memory-Updates.
- **Trunk-based:** Owner-Agent pusht klein + grün direkt auf `main` (Tests
  grün). `claude/...`-Branches nur für Riskantes/Experimentelles und
  kurzlebig (same-session mergen/löschen). Bei fremder Release-Übernahme
  komplett stillstehen. Voller Workflow: SSOT `crowdergy-ios/CLAUDE.md`.
