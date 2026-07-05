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
  - **Connector-lokale Entity-Slots (gehen NICHT ans Backend, nur in
    HA-Scope) MÜSSEN zusätzlich in `_build_device_record`
    (`config_flow_mapping.py`) eingetragen werden** — das ist die explizite
    Allowlist, die die Entity-Map auf den Config-Entry persistiert. Fehlt
    der Key dort, wird er beim Submit STUMM verworfen (beim Re-Open weg,
    Dispatcher liest leer). Zweimal passiert: v3.28.0 (HC-Triade) und
    v3.33.0→.2 (Wallbox-Ladestrom `entity_wallbox_charge_current_a`). Regel:
    Schema-Touch ⇒ immer auch `_build_device_record` + Round-Trip-Test
    (`test_build_device_record_persists_*`).
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
- **WP-Temperatur-Modus (User-Vorgabe 2026-07-02, Branch
  `claude/heatpump-minmax-temp-mapping-mrz89x`): Wärmepumpen NIE hart
  an/aus schalten — nur gemappte Min-/Max-Zieltemperaturen.**
  Für heating/warmwater mit climate-/water_heater-`entity_control` und
  NUMERISCHEN value_on/value_off schreibt der Dispatch
  `set_temperature(value_on)` bei AN (Maximaltemperatur) und
  `set_temperature(value_off)` bei AUS (Minimaltemperatur) — es gibt in
  diesem Modus KEIN `set_hvac_mode("off")`/`set_operation_mode("off")`;
  die WP entscheidet selbst, wie lange der Verdichter läuft
  (Verallgemeinerung des etablierten number-`set_value`-WW-Musters).
  **Regeln:** (1) Erkennung = `is_temperature_control(domain, raw_value)`
  (`const.py`, numerisch + Thermal-Domain) — HVAC-Modi sind nie
  numerisch, daher kollisionsfrei; Legacy-Modus-Strings laufen
  unverändert. (2) Idempotenz-Guard + Hold-Drift-Repair vergleichen im
  Temperatur-Modus das `temperature`-ATTRIBUT, nie `state.state`
  (`_control_actual_state`; `state.state` trägt nur „heat" —
  String-Vergleich würde jeden Hold-Tick blind nachschreiben →
  Piep-/Write-Storm). `_states_match` vergleicht domain-übergreifend
  float-first. (3) `_read_is_on_state` mappt Max-Temp→True,
  Min-Temp→False, fremden Setpoint→None (Backend behält letzten Wert).
  (4) Config-Flow (`_values_schema(device_type=…)`): heating/warmwater
  + climate/water_heater bekommen AUSSCHLIESSLICH °C-NumberSelector-
  Felder (Range aus min_temp/max_temp/target_temp_step); `value_cool_on`
  bleibt Modus-Dropdown; aircon bewusst Modus-basiert (Kühlen =
  invertierte Temperatur-Semantik). Keine neuen Entity-Slots → kein
  `_build_device_record`-/`preset_spec`-/Backend-Change. Tests
  `test_temperature_control.py` (14). **Haftungsausschluss** prominent:
  README-Top-Sektion + `user`-/`device_values`-Step-Descriptions
  (strings.json + de.json, typografische „…“-Quotes beachten!).
- **HA-Entity-Generierung entschlackt (User 2026-07-03, Branch
  `claude/ha-connector-device-gen-d6w0de`): eigener `sensor`-Platform-Mirror
  ENTFERNT.** Der Connector legte pro Gerät ein eigenes HA-„Device"
  (`Crowdergy_<Name>`, `device_registry.py`) mit `switch` + `sensor` +
  (integration-weit) `binary_sensor` an. Die zwei **Spiegel-Sensoren**
  (`sensor.py`: `Crowdergy_Current Power` + `Crowdergy_State of Charge`)
  doppelten nur die schon vorhandenen echten Integrations-Entities des
  Users (Wert kam aus `coordinator.data`, das aus genau diesen Entities
  gelesen wird) → verwirrend („Solar Power" zweimal, nur per
  `Crowdergy_`-Präfix unterscheidbar). **`sensor.py` gelöscht, `sensor`
  aus `PLATFORMS` (`const.py`) raus.** BEHALTEN, weil echte, sonst nicht
  vorhandene Funktion: **(1)** der per-Gerät **`switch` „Crowdergy AI"**
  (`switch.py`) = HA-seitiger Steuerpunkt für den Crowdergize-Consent-Flag
  (POSTet `toggle_active`, spiegelt den iOS-Toggle), **(2)** das
  integration-weite **`binary_sensor.crowdergy_connected`**
  (`binary_sensor.py`) = SSE-Liveness für Übernahme-Automationen bei
  Crowdergy-Ausfall. **Kein Telemetrie-/Dispatch-Bezug** — die generierten
  Platform-Entities sind reine Sicht-/Bedien-Schicht; gelesen/geschaltet
  wird immer über die gemappten echten Entities. Kein Schema-/Backend-/
  Preset-Change; keine Entity-Slots berührt. Bestands-Sensor-Entities
  werden beim nächsten Setup verwaist (Alpha: ok). Tests grün (229; die 2
  `test_sse_client`-Flakes sind der bekannte aiodns/venv-Drift).
  **RELEASED v3.37.0 (2026-07-03, PR #33 → `main` `5fb0592`, `tag-release.yml`
  Run #27; Release-Notes `docs/releases/v3.37.0.md`).** HACS zieht das Release
  automatisch; OFFEN nur User-Hand: nach Update einmal re-provisionieren (oder
  HA neu starten), damit die Alt-`sensor.crowdergy_*`-Entities verschwinden.
- **Card-Delete-Hook destruktiv unter dem Hub-Modell (Bugfix, User-Report
  2026-07-03, Folge zu v3.38.0, Branch `claude/ha-connector-device-gen-d6w0de`):**
  User: „Wenn ich die alten `Crowdergy_<Name>`-Karten in HA lösche, geht auch
  das Crowdergy-Gerät weg." Root-Cause: `async_remove_config_entry_device`
  (`__init__.py`) war aus der per-Gerät-Device-Ära — es zog aus JEDER
  gelöschten Karte die `(DOMAIN, identifier)`-ID und rief
  `coordinator.delete_device_backend(id)` + drop aus `CONF_DEVICES` + reload.
  Unter dem Hub-Modell sind die alten Karten aber entity-lose LEICHEN (Switch
  ist auf den Hub `(DOMAIN, entry_id)` re-homed, Sensoren seit v3.37.0 weg) →
  eine „leere" Karte zu löschen löschte still das REALE Gerät am Backend +
  nahm seinen AI-Switch vom Hub. **Zwei-Teil-Fix:** (1) **proaktiver Prune**
  `_prune_legacy_device_cards` in `async_setup_entry` NACH
  `async_forward_entry_setups` — detacht alle Nicht-Hub-`(DOMAIN, …)`-Devices
  des Entries (`async_update_device(remove_config_entry_id=…)`) → HA räumt die
  leeren Karten selbst, User muss nichts von Hand löschen (sicher, weil die
  Switches schon re-homed sind). (2) **`async_remove_config_entry_device`
  nicht-destruktiv:** Hub-Karte (`identifier == entry_id`) → `return False`
  (Live-Device, nicht per Karte löschbar); jede andere `(DOMAIN, …)`-Leiche →
  `return True` OHNE Backend-Delete/Config-Change/Reload. **Echtes Löschen
  eines Geräts läuft weiter über den Options-Flow `remove_device`
  (Configure → Gerät entfernen: Backend-DELETE + `_remove_ha_device` +
  reload)** — der ist die einzige korrekte Delete-Route. **Regel: unter einem
  geteilten Hub-Device darf `async_remove_config_entry_device` NIE aus einer
  Karten-Löschung ein Backend-Device ableiten — die Karte ist nicht mehr 1:1
  ein Gerät.** `CONF_DEVICE_ID`/`CONF_DEVICES`-Import raus (nur noch im
  alten Hook genutzt). Tests `test_device_cards.py` (3: Hub refuse /
  Leiche-allow-ohne-Backend / Prune behält Hub). Full-Suite 230 grün.
  **RELEASED v3.40.0 (2026-07-03, PR #37 → `main` `af4ad99`, `tag-release.yml`;
  Release-Notes `docs/releases/v3.40.0.md`; v3.39.0 war parallel schon
  vergeben).** HACS zieht das Release automatisch; nach dem Update räumt der
  Prune die Alt-Karten selbst. ⚠️ Wer vor dem Fix Alt-Karten von Hand löschte,
  hat die Geräte backend-seitig verloren → im Wizard neu anlegen.
- **HA-Geräteliste: EIN „Crowdergy"-Hub-Gerät statt per-Gerät-Dubletten
  (User 2026-07-03, Folge zu v3.37.0, Branch
  `claude/ha-connector-device-gen-d6w0de`):** der Connector legte pro Gerät
  ein eigenes HA-Device `Crowdergy_<Name>` an (`device_registry.get_device_info`)
  → Doppelkarte neben dem echten Integrations-Gerät des Users, und nach dem
  Sensor-Entfernen leere Karten für read-only-Typen (solar/grid, die nie
  einen Switch bekommen). **Fix:** `get_device_info(device)` →
  `get_hub_device_info(entry)` — EIN integration-weites Device
  (`identifiers={(DOMAIN, entry.entry_id)}`, name „Crowdergy", model „Energy
  Manager"). ALLE per-Gerät-„Crowdergy AI"-Switches (`switch.py`, bekommt
  jetzt `device_info` + `entry` durchgereicht) UND das
  `binary_sensor.crowdergy_connected` hängen daran. **Naming-Falle: mit
  einem geteilten Hub-Device verlieren die Switches die Geräte-Kontext-
  Disambiguierung** — daher `_attr_has_entity_name=False` +
  `_attr_name = f"Crowdergy AI: {device_name}"` (vorher fixes „Crowdergy AI"
  auf per-Gerät-Device); `suggested_object_id` (`crowdergy_<slug>_ai`)
  unverändert → entity_ids stabil. `DEVICE_TYPE_MODELS` + der per-Typ-Modell-
  String entfielen (Hub braucht keinen Typ) → `test_type_registry.py` dropt
  die eine `DEVICE_TYPE_MODELS`-Assertion (aircon-Coverage bleibt über
  DEVICE_TYPES/CONTROLLABLE_TYPES/switch/NAME_HINTS gesichert). **Kein
  Telemetrie-/Dispatch-/Schema-/Backend-/Preset-Change.** Alte
  `Crowdergy_<Name>`-Devices verwaisen beim nächsten Setup (HA räumt
  entity-lose Devices; Alpha ok). Tests grün (227; +2 sse-Flakes wie gehabt).
  **RELEASED v3.38.0 (2026-07-03, PR #36 → `main` `8ad4a59`, `tag-release.yml`;
  Release-Notes `docs/releases/v3.38.0.md`).** HACS zieht das Release
  automatisch; OFFEN nur User-Hand: nach Update einmal re-provisionieren
  (oder HA neu starten), damit die Alt-`Crowdergy_<Name>`-Devices verschwinden.
- **Wallbox:** Pre-AI-Lademodus wird bei AI-OFF NICHT restauriert
  (Restore-Pfad entfernt, Backend-Spalte existiert nicht mehr).
- **Wallbox variabler Ladestrom (User 2026-06-20, released v3.33.0;
  Persistenz-Fix v3.33.2/PR #17; analog Batterie-Power-Setpoint):**
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
  Entflechtung steht) sind gelandet.
  **Phase-C-Extraktion gelandet (2026-06-22):** die 13 read/compose/decide-
  Helfer (`_get_state`, `_prefetch_device_states`, `_read_*`, `_compose_extra`,
  `_payload_hash`, `_should_send`, `_normalised_vehicle_status`) +
  die Send-/Extra-Field-Konstanten (`SEND_THRESHOLDS`,
  `IDENTICAL/PER_DEVICE_HEARTBEAT_INTERVAL`, `_SOLVER_EXTRA_FIELDS`,
  `_PREFETCH_SLOT_KEYS`) sind nach `telemetry_reader.py` als
  **`TelemetryReaderMixin`** ausgezogen (Mixin-Pattern wie
  `CommandDispatcherMixin`; `self` bleibt der Coordinator → kein Body-Edit).
  coordinator.py −471 Z. **Die Konstanten werden aus `coordinator.py`
  RE-EXPORTIERT** (`from .telemetry_reader import …`) — Tests/Vendoring
  importieren `coordinator._SOLVER_EXTRA_FIELDS` etc. per Modul-Attribut, der
  Zugriffspfad bleibt gültig (analog `config_flow._ENTITY_SELECTORS`). Bewusst
  NICHT mitgezogen: die **Consent-Gates** (`_consent`/`_remote_control_allowed`,
  cross-cutting, auch vom Dispatch genutzt), das `_async_update_data`-Tick
  selbst (HA-Interface) und der Auth-Cluster (s.o.). **Regel: neue Reader →
  `TelemetryReaderMixin`; neue read/compose-Konstante dort + Re-Export, falls
  ein Test sie als `coordinator.<NAME>` importiert.** Weitere Entflechtung
  (Lifecycle-/Loop-Wiring) bleibt optional offen.
  `house-consumption-chart.md` trägt jetzt `heatpump_total` (#43, Backend-
  Vertrag — kein Connector-Code, Slot-Schema deckt die Thermal-Typen seit
  #68).
- **Coordinator Per-Tick State-Prefetch (#56, 2026-06-22):** alle
  `_read_*`-Reader lesen HA-States jetzt über `_get_state(entity_id)` statt
  direkt `self.hass.states.get`. `_async_update_data` snapshottet je Gerät
  einmal `_prefetch_device_states(dev)` → `self._state_cache` (Slots aus
  `_PREFETCH_SLOT_KEYS` + die `_SOLVER_EXTRA_FIELDS`-Sensoren des Typs), sodass
  mehrfach referenzierte Entities (z.B. `entity_control` von is_on UND cool_on)
  nur 1× gelesen werden. **Strikt auf die SYNCHRONE Lese-Phase begrenzt:**
  Cache wird NACH der Entity-Extraktion gesetzt und direkt nach
  `_compose_extra` (vor dem ersten `await`/Send) wieder `None`-gesetzt — kein
  cross-coroutine-Bleed (Hold-Loops/Dispatch lesen immer live; `_get_state`
  default via `getattr(...,None)`). **Regel: neue `_read_*`-Reader IMMER
  `_get_state` nutzen (nie `hass.states.get` direkt), neue gelesene
  Entity-Slots in `_PREFETCH_SLOT_KEYS` eintragen.** Test:
  `test_async_update_data_roundtrip.test_prefetch_reads_shared_entity_once`.
- **#97 Loop-Konstanten + integration_domain dedupliziert (Code-Review 2,
  2026-07-02, PR connector#24 → `main` gemerged; kein Release nötig —
  reines Tech-Debt-/Latenz-Update, nächstes reguläres Release nimmt es
  mit):**
  (a) `HEARTBEAT_PING_INTERVAL`/`PER_DEVICE_MIRROR_INTERVAL`/
  `STATE_RESYNC_INTERVAL` sind jetzt EINZIG in `telemetry_composer.py`
  definiert (dort leben + lesen die Loops; die Rationale-Docstrings sind
  mitgezogen) und werden aus `coordinator.py` RE-EXPORTIERT — die
  Coordinator-Kopien waren seit dem #21-Split tot (Drift-Trap). **Regel
  (Erweiterung der telemetry_reader-Regel): Konstanten gehören ins Modul,
  dessen Code sie liest; `coordinator.<NAME>` bleibt via Re-Export
  gültig.** (b) der Contribute-Flow berechnet `integration_domain` nur
  noch EINMAL über `entity_mapper.dominant_integration_domain` (häufigste
  Domain); das frühere `_resolve_integration_domain`
  (first-resolvable, wurde sofort überschrieben) ist entfernt.
- **#98 charge_mode/cool_control state-gewatcht (gleiche PR, gemerged):**
  `_build_entity_map` registriert jetzt auch `CONF_ENTITY_CHARGE_MODE` +
  `CONF_ENTITY_COOL_CONTROL` — ein manueller Flip am HA-Select propagiert
  über den Event-Refresh (≤5 s) statt erst am 30-s-Heartbeat. **Regel:
  jeder Slot, den der Per-Tick-Read zurück ans Backend spiegelt, gehört
  in die `_build_entity_map`-Key-Liste.** Test `test_entity_watch.py`.
- **Uniform control-capability `control_entities_mapped` (User 2026-07-03,
  Branch `claude/battery-readonly-expandable-q86ra1`):** EIN Bool für ALLE
  steuerbaren Typen, aus dem iOS „Steuerbar"/„Nur lesend" rendert (statt der
  alten per-Typ-Signale). `device_field_spec._compute_is_controllable`
  (always/always, `types=CONTROLLABLE_TYPES`) leitet es aus der Präsenz der
  Steuer-Entity ab, die der `_apply_*`-Guard WIRKLICH braucht: wallbox
  `entity_charge_mode`; **battery `entity_battery_mode` + Aktiv/Passiv-Werte
  — der Power-Setpoint (`Zielleistung`) ALLEIN reicht NICHT, `_apply_battery_
  setpoint` skippt ohne Modus-Select**; heating/warmwater/generic
  `entity_control`; aircon `entity_control` ODER `entity_cool_control`. Nur das
  abgeleitete Bool geht ans Backend (Entity-IDs bleiben Connector-lokal, kein
  `_build_device_record`-Eintrag nötig — die gelesenen Keys sind schon
  persistiert). Backend-Spalte + iOS-Feld gehören dazu (`extra=\"forbid\"` ⇒
  Backend-Deploy VOR Connector-Release). Test `test_device_field_spec.py`
  (setpoint-only battery = False, mode+values = True, per-Typ, readonly-Typen
  tragen das Feld nicht). **RELEASED v3.36.0 (2026-07-03, PR #29 → `main`,
  `tag-release.yml` Run #25 grün):** der Connector-Release-Schritt der
  Deploy-Reihenfolge ist damit erledigt (Backend-Spalte war schon deployed);
  OFFEN nur noch — User re-provisioniert (oder editiert) je Gerät einmal,
  dann populiert das Flag.
- **Auto-Discovery deaktiviert + benötigte Integrationen am Profil-Pick
  (User 2026-07-03, PR connector#35 → `main` `557251b`, RELEASED v3.39.0
  via `tag-release.yml`):** Zwei Config-Flow-Änderungen. **(1) Auto-Setup
  aus (nicht gelöscht):** `async_step_location` routet jetzt direkt in
  `async_step_device_type` (manueller Per-Gerät-Pfad) + pinnt
  `setup_mode=manual` — die Heuristik erkennt Geräte im Feld nicht
  zuverlässig. Die Steps `setup_mode`/`auto_discover`/`auto_confirm` +
  `entity_mapper`-Discovery bleiben als **toter, aber erhaltener Code**
  (nur nicht mehr verdrahtet) → ohne Neubau reaktivierbar. Kein Test
  referenziert den Auto-Pfad, daher risikofrei. **(2) `required_integrations`:**
  der Contribute-Payload trägt jetzt zusätzlich zur häufigsten Domain
  (`dominant_integration_domain`) ALLE distinkten Integrationen der
  entity_map — neuer Helfer `entity_mapper.required_integration_domains`
  (sortierte distinkte Config-Entry-Domains). Der Profil-Picker
  (`config_flow_schemas._vendor_preset_pick_schema`) hängt an jedes
  Profil-Label „· benötigt: <Klarname[, …]>" — Quelle ist
  `preset["required_integrations"]` aus dem Lookup, Fallback auf den
  Einzelwert `integration_domain` (Alt-Backend). Klarnamen aus neuer
  `entity_mapper.INTEGRATION_DISPLAY_NAMES`-Map + `integration_display_name`
  (Slug→Title-Case-Fallback). **Regel: neue Klarnamen für Integrationen
  in `INTEGRATION_DISPLAY_NAMES` ergänzen (Slug-Fallback ist sicher, aber
  hässlich).** Backend-Hälfte (Feld speichern/ausliefern): PR backend#74,
  prod-deployed Run #67 — Reihenfolge Backend→Connector eingehalten. Kein
  Box-Change (ignoriert das additive Feld). Vertrag
  `docs/crowd-preset-store.md`. Tests `test_contribute_flow.py` (+3:
  Payload trägt Feld / Picker labelt Integrationen inkl.
  `integration_domain`-Fallback + unbekannter Slug / Display-Name
  bekannt+Fallback).
- **Crowd-Preset `required_helpers` — HA-Helfer-Provisionierung (User
  2026-07-05, Branch `claude/ha-helpers-profile-sharing-iicz4p`, PR
  connector#39 DRAFT):** ein Preset kann einen Slot auf einen selbst
  angelegten HA-Helfer mappen (`input_select`/`input_number`/`input_boolean`);
  ein Empfänger hat ihn nicht. **Produce:** `entity_mapper.
  required_helper_specs(hass, entity_map)` leitet je Slot, der auf einen
  `input_*`-Helfer zeigt, die Spec aus der LIVE-HA-Config ab (options/min/max/
  step/unit/friendly_name); native `select`/`number` sind KEINE Helfer → keine
  Spec. Contribute-Payload trägt `required_helpers` nur wenn welche existieren.
  **Apply — DURABLE LEHRE: der Connector kann `input_*`-Helfer NICHT
  zuverlässig selbst anlegen.** Er läuft INNERHALB der HA, aber die
  input_*-Storage-Collection wird von HA nirgends abrufbar in `hass.data`
  abgelegt (anders als z. B. `zone`, das seine Collection unter
  `hass.data[DOMAIN]` ablegt — bei input_* ist das die EntityComponent, nicht
  die Collection). Es gibt keine stabile API + keine WS-Connection von innen.
  Deshalb KEIN Fake-Anlegen: der Profil-Picker labelt „· HA-Helfer nötig: …"
  (`config_flow_schemas._preset_required_helpers_label`) und die Helfer-Slot-
  IDs werden im Entity-Step als Vorschlag vorbefüllt; der User legt die Helfer
  1× in HA an. **Die BOX legt sie automatisch an** (steuert HA von AUSSEN per
  WS-Collection-`<domain>/create`). **Regel: HA-Helfer programmatisch anlegen
  geht nur von AUSSEN (Box/WS), NICHT aus einem HACS-Component heraus.**
  Backend-Hälfte (Feld speichern/ausliefern): PR backend#90. Vertrag
  `docs/crowd-preset-store.md` (HelperSpec + per-Consumer-Pflicht). Tests
  `test_contribute_flow.py` (+5).
- **UI-Text-Stil (User-Vorgabe 2026-07-02, kompletter Rewrite strings.json +
  de.json):** Config-Flow-Texte **fachlich statt technisch** und kurz; JEDE
  Seite beginnt mit 1–2 Sätzen, WOFÜR die Zuordnung gebraucht wird („Damit
  Crowdergy … kann"). Schaltwerte-Seiten (`*_values`) tragen den
  Ziel-Temperatur-Tipp (An = höchste, Aus = niedrigste gewünschte Temperatur,
  z. B. WW An=55/Aus=40). Einstiegsseiten (Pairing-Step + Options-Menü)
  verlinken [crowdergy.de](https://crowdergy.de) — HA rendert Markdown in
  Step-Descriptions. Vendor-Presets heißen user-facing **„Geräteprofil"**,
  `entity_control_hold` heißt **„Befehl wiederholen"**. Regel: `strings.json`
  (EN) und `translations/de.json` IMMER synchron (gleiche Keys +
  Placeholders); neue Texte im selben Stil.
- **Brand-Icon licht/dunkel-tauglich (2026-07-02):** `brand/icon.png` (256)
  + `icon@2x.png` (512) sind eine **abgerundete Kachel** (22 %-Eckradius,
  transparente Ecken) mit dezentem hellem Rand — lesbar auf hellem UND
  dunklem HA-Hintergrund. Nie wieder ein voll-deckendes randloses Quadrat.
  ACHTUNG: das in HA sichtbare Icon serviert brands.home-assistant.io aus
  dem `brands`-Fork (`custom_integrations/theothergas/`) — neue PNGs dorthin
  kopieren + Upstream-PR (User-/Mac-Hand, Repo nicht im Remote-Scope).
- **Config-Flow Edit-Felder:** `vol.Optional(..., description=
  {"suggested_value": ...})`, NIE `default=` (HA re-injected, Felder
  werden unlöschbar).
- **API-URL NICHT mehr im interaktiven Setup-Flow (released v3.33.5, #22):**
  der `user`-Step (`async_step_user`) zeigt KEIN API-URL-Feld mehr — der
  HACS-Connector pairt immer gegen `DEFAULT_API_URL`. `CONF_API_URL` bleibt
  intern (Entry-`data` = `DEFAULT_API_URL`), damit Coordinator/Reauth/Refresh
  unverändert laufen → nicht entfernen. **Self-Hosted/Dev-Backends gehen NUR
  noch über den headless `provision_box`-Pfad** (`async_step_import` +
  `provisioning.validate_provision_data` akzeptieren weiter optional `api_url`);
  bewusst KEINE UI-Escape-Hatch (User 2026-06-27). `api_url`-Labels/-Hilfetexte
  sind aus `strings.json`/`de.json` raus; Step-Beschreibung gekürzt.
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

**CI-Trigger-Modell (2026-06-21, connector#19):** `test.yml` läuft NICHT mehr
auf `push→main`. Der **Mac ist Default-Test-Runner** über den
`scripts/githooks/pre-push`-Hook (einmalig pro Klon aktivieren:
`git config core.hooksPath scripts/githooks`; Notausgang `git push
--no-verify`). GitHub-CI feuert nur auf **PRs** (Arbeit weg vom Mac, z.B.
Claude Code on the web) + einem nächtlichen `schedule`-Backstop auf `main`
(04:17 UTC).

**Remote-Session (Claude Code on the web):** der SessionStart-Hook
(`.claude/hooks/session-start.sh`) baut `.venv` (Python 3.12,
`requirements-test.txt`). Tests dann via `.venv/bin/pytest`.
`tests/test_sse_client.py::test_start_is_idempotent` und
`test_stop_cancels_running_task` haben einen bekannten
umgebungsabhängigen Error (aiodns/aiohttp-Drift in frischen venvs) — auf
sauberem Stand gegenprüfen, bevor man eigene Änderungen verdächtigt.

**JSON-Assets immer hart validieren (Lektion v3.33.1):** ein nicht
escaptes `"` in `translations/de.json` (Wallbox-Ladestrom-Hilfetext)
ließ HA beim Setup mit `orjson.JSONDecodeError` abbrechen — Hassfest
fängt das NICHT. `tests/test_json_assets.py` parst jetzt jede
ausgelieferte `*.json`. Deutsche Hilfetexte tragen typografische
Anführungszeichen `„…"` (NICHT gerade `"` ohne Escape).

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
