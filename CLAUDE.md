# crowdergy-connector — CLAUDE.md

Kurz-Memory für Claude-Sessions und Menschen: **repo-spezifische Regeln,
Kommandos, Stolpersteine.** Kein Backlog, keine Historie (`git log`,
PRs, Issues, `docs/releases/`). Architektur-Karte: `CONTEXT.md`.
Verträge: `docs/crowd-preset-store.md`, `docs/house-consumption-chart.md`,
`docs/costs-today.md`. Aktuelle Version: `manifest.json`.

## SSOT — was lebt wo (gilt für alle Crowdergy-Repos)

| Was | Wo | Regel |
|---|---|---|
| Roadmap + Backlog — **alle** offenen Punkte **aller** Repos | GitHub Issues in `colinwilke/crowdergy-backend` (+ Projects-Board) | Kategorie-Label `00`–`10` (Connector = `04 Connector`), Gate = Milestone, Epic = Parent-Issue, `Entscheidung` (geschlossen = entschieden), `prio:` = Vorschlag |
| Regeln, Kommandos, Stolpersteine je Repo | `<repo>/CLAUDE.md` | kurz; keine Historie, keine offenen Punkte |
| Architektur-Karte je Repo | `<repo>/CONTEXT.md` (Box: nur `CLAUDE.md`) | Ist-Zustand, kein Changelog |
| Verträge/Specs | `<repo>/docs/` | contract-first bei Cross-Repo-Änderungen |
| Historie | `git log`, PRs, geschlossene Issues, Release-Notes | nicht in Markdown wiederholen |

Wer im Code oder in Markdown einen offenen Punkt findet: Issue im
Backend-Repo anlegen, nicht hier notieren (dieses Repo ist **public** —
keine internen Details in eigenen Issues/PR-Texten). Alte Backlog-Nummern
in Kommentaren: Mapping in colinwilke/crowdergy-backend#257.

## Zusammenarbeit (Colin + Udo, seit 2026-09-04)

- **Colin ist Maintainer aller Repos.** Kleine, grüne Änderungen direkt auf
  `main`; riskante Arbeit über `claude/<thema>-<id>` + PR.
- **Udo arbeitet ausschließlich über PRs** (Draft ok). Colin reviewt und
  merged. Udo merged nichts und gibt nichts frei.
- Claude-Sessions handeln im Namen dessen, der sie startet.
- **Releases (Manifest-Bump + Tag) nur auf Zuruf von Colin.** Reihenfolge
  bei neuen Telemetrie-Feldern: **Backend zuerst deployen** — das Backend
  ist `extra="forbid"`, ein Alt-Backend 422t sonst die ganze Payload.
- Memory-Änderungen (`CLAUDE.md`/`CONTEXT.md`) dürfen direkt auf `main`.
- Alpha-Phase: Entry-data/-options dürfen breaking ändern (Test-Nutzer
  re-provisionieren). **Nie gelockert:** Consent vor jeder Steuerung,
  `MAPPABLE_ENTITY_DOMAINS`-Default-DENY, nie stumm ein Preset re-applien
  (Steuer-Slots schalten reale Hardware).

## Repo-Karte

HACS Custom-Component, HA-Domain `theothergas` (Rename → Issue #220),
public, MIT. `custom_components/theothergas/`:

| Modul | Verantwortung |
|---|---|
| `coordinator.py` | Auth/Refresh, `DataUpdateCoordinator`-Tick, Frame-Dispatch; **Auth-Cluster bleibt hier** (Tests patchen Modul-Attribute) |
| `command_dispatcher.py` | `CommandDispatcherMixin`: `_apply_*`, Hold-Loops, Write-Clamp, Circuit-Breaker, Übersteuerungs-Erkennung, Lease-Expiry |
| `telemetry_reader.py` | `TelemetryReaderMixin`: `_read_*`, `_should_send`, `_measurement_liveness`, Prefetch-Cache |
| `telemetry_composer.py` | Loops (Heartbeat, Device-Mirror, State-Resync), Loop-Konstanten |
| `state_mirror.py` | `DeviceStateMirror` (Zustände, Hold-Tasks, Write-Zähler, Override-Uhr, `last_written_value`) |
| `sse_client.py` | Reconnecting SSE-Listener (Bearer-Header, Half-Open-Detection, 401-Backoff) |
| `config_flow*.py` | Pairing-Code-Onboarding, Per-Gerät-Anlage, Profil-Pick, Contribute; `_schemas`/`_presets`/`_mapping`-Splits |
| `device_field_spec.py` | **SSOT** der Device-Felder im create/update-Roundtrip |
| `preset_spec.py` | **SSOT** der Crowd-Preset-Slots (public Teil des Store-Vertrags) |
| `const.py` | `DEVICE_TYPES`, `CONTROLLABLE_TYPES` (SSOT), `MAPPABLE_ENTITY_DOMAINS` (Allowlist), `GERMAN_STATES`, Intervalle, `is_temperature_control` |
| `entity_mapper.py` | Registry-Identität, Preset-Auflösung, Integrations-Klarnamen |
| `provisioning.py` / `box_services.py` | Box-Pfade — **nur mit `theothergas:`-YAML-Key aktiv** |
| `switch.py` / `binary_sensor.py` | „Crowdergy AI"-Switch je Gerät + `crowdergy_connected` am **einen** Hub-Device |

Public-Repo-Disziplin: hier liegt nur das Slot-Schema; Store-Daten und
Kuration bleiben im Backend, Box-Know-how im privaten Box-Repo.

## Kommandos

- **Tests:** Python ≥ 3.12. Remote-Session: `.claude/hooks/session-start.sh`
  baut `.venv`; `.venv/bin/pytest`. Zwei bekannte Umgebungs-Flakes in
  `tests/test_sse_client.py` (aiodns/aiohttp-Drift) werden in CI
  deselektiert — auf sauberem Stand gegenprüfen, bevor man eigene
  Änderungen verdächtigt.
- **CI:** `test.yml` auf PRs + nächtlichem Backstop, nicht auf push→`main`.
  Mac: pre-push-Hook `git config core.hooksPath scripts/githooks`
  (Notausgang `--no-verify`).
- **Release:** Manifest-Bump **nur beim Release auf `main`**, durch den, der
  taggt — nie auf Feature-Branches (Versions-Kollision); vorher prüfen, ob
  der Tag auf origin schon belegt ist. Tag + GitHub-Release über
  `tag-release.yml` (`workflow_dispatch`; der Session-Git-Proxy blockt
  Tag-Pushes still). **`sha`-Input = voller 40-Zeichen-SHA oder leer** —
  eine abgekürzte SHA lässt `actions/checkout` scheitern und der
  Tag-Schritt wird still geskippt. Release-Notes `docs/releases/vX.Y.Z.md`.
  HACS zieht Releases automatisch. Box-Pin nachziehen ist ein eigener
  Schritt (`crowdergy-box/CONNECTOR_VERSION`).
- **HA-Debug (Colins Instanz):**
  `set -a; source ~/.config/crowdergy/ha.env; set +a;
  curl -H "Authorization: Bearer $HA_TOKEN" "$HA_URL/api/states/sensor.X"`.
  Der User betreibt `modbus.write_register` selbst (Hub `KWR`); der
  Connector schreibt nur in HA-Helper/Entities.

## Harte Regeln

### SSOT im Code
- Neue Backend-Device-Felder **nur** in `device_field_spec.py`;
  Capability-Bools aus Connector-lokalen Entities als `_compute_*` dort
  (nie die Entity-ID ans Backend). Backend-Schema-Feld muss vor dem
  Release existieren.
- **Connector-lokale Entity-Slots zusätzlich in `_build_device_record`**
  (`config_flow_mapping.py`) — fehlt der Key, wird er beim Submit stumm
  verworfen (zweimal passiert). Schema-Touch ⇒ `_build_device_record` +
  Round-Trip-Test `test_build_device_record_persists_*`.
- Mapping-Slots nur in `MAPPABLE_ENTITY_DOMAINS` (Read-Slots sensor/
  binary_sensor, Control-Slots schreibbare Domains). Preset-Slots nur in
  `preset_spec.PRESET_SLOT_SPEC`. `_ENTITY_SELECTORS` bestimmt, was ein
  Slot anbietet; `device_class`-Filter nur an eindeutigen Read-Slots (nie
  an climate/water_heater/Control-Slots).
- Slots, die der Tick ans Backend spiegelt, gehören in die
  `_build_entity_map`-Key-Liste (Event-Refresh ≤ 5 s statt Heartbeat).
- Neue Reader → `TelemetryReaderMixin` mit `_get_state` (nie
  `hass.states.get` direkt), gelesene Slots in `_PREFETCH_SLOT_KEYS`.
  Konstanten leben im Modul, das sie liest, und werden aus
  `coordinator.py` re-exportiert (Tests importieren `coordinator.<NAME>`).

### Steuerung
- **Consent:** Telemetrie-Consent gated nur Energiedaten (Heartbeat,
  Version, Polling nicht); Remote-Control-Consent zentral in allen
  `_apply_*` (inkl. Resync, Self-Heal, Hold, Lease).
- **Wärmepumpen nie hart an/aus:** heating/warmwater mit climate/
  water_heater und numerischen `value_on`/`value_off` ⇒ `set_temperature`
  (AN = Max, AUS = Min), nie `set_hvac_mode("off")`. Idempotenz, Hold und
  Resync vergleichen das `temperature`-**Attribut**, nie `state.state`;
  `_read_is_on_state`: Max → True, Min → False, fremd → None. Aircon bleibt
  Modus-basiert.
- **Clamp und Vergleich sind eine Einheit:** jeder numerische Write wird
  gegen die Grenzen der Ziel-Entity geklemmt (`_clamp_write_value`, loggt
  WARNING), und Idempotenz-Guard/Hold/Readback vergleichen gegen den
  **geklemmten** Wert. Kollabieren An- und Aus-Wert auf dieselbe Zahl →
  `None` + `control_value_rejected`.
- **„Wert steht in der Entity" ≠ „Befehl wirkt":** Bezugsgröße ist
  `state.last_written_value` (jeder neue Schreibpfad pflegt sie); der
  optionale Slot `entity_effective_setpoint` hängt über
  `_with_effect_slot()` an **jedem** Steuer-Typ; gemeldet wird erst, was
  `CONTROL_EFFECT_MIN_MISMATCH_S` anhält; unlesbarer Sensor = keine
  Aussage.
- **Schreib-Circuit-Breaker** `_write_allowed` (500/h je Entity) sitzt an
  jedem Write-Pfad. **Übersteuerungs-Erkennung nur im AUTO-Hold**
  (ALWAYS ist für Auto-Reset-Register): Drift ohne eigenen Write in
  `LOCAL_OVERRIDE_GRACE_S` → 2-h-Pause; der Vergleich ist exakte
  Gleichheit ohne Toleranz; die WARNING mit `actual`/`expected` ist die
  **einzige** Quelle für die Ursache (das Backend sieht nur das Boolean).
- **Jedes Cloud-Kommando ist eine Lease:** der SSE-Stale-Bail des
  charge_mode-Holds startet den Lease-Expiry (`COMMAND_LEASE_TTL_S`) →
  einmal Safe-Default (wallbox → Solar nur wenn gemappt, battery →
  passive). Auf toter Cloud nie `lock`/`power` schreiben. Thermal-Hold
  bewusst ohne Lease.
- Wallbox-Dispatch: Phase **vor** Strom **vor** Modus; „Auto" nie
  schreiben; Solar/Lock tragen keinen Strom. Batterie: Setpoint vor Modus,
  ±10-W-Toleranz. Pre-AI-Lademodus wird bei AI-OFF nicht restauriert.
- **Telemetrie erfindet nichts:** `_measurement_liveness` (alle Mess-Slots
  tot, Steuer-Slots zählen nicht) sendet `is_online: false` und lässt
  `power_kw` **weg** statt zu nullen. Der Mirror strippt alle Δ-Felder
  (`_DELTA_FIELDS`). Zähler-Δ per High-Water-Mark (Dip ≥ 0,9 = Rauschen,
  < 0,9 = Reset, Baseline nur bei Reset neu). Kategorische Felder
  (`is_online`, `write_breaker`, `local_override`, `control_*`) stehen in
  `_should_send`.
- **Hub-Modell:** ein HA-Device „Crowdergy"; `async_remove_config_entry_
  device` darf aus einer Karten-Löschung nie ein Backend-Delete ableiten.
  Echtes Löschen nur über den Options-Flow `remove_device`.

### Config-Flow & Texte
- Edit-Felder `vol.Optional(..., description={"suggested_value": …})`,
  **nie** `default=` (HA re-injected, Felder werden unlöschbar).
- Keine API-URL im UI-Flow (`DEFAULT_API_URL`); Self-Hosted nur über den
  `provision_box`-Import-Pfad. Auto-Discovery ist nicht verdrahtet (Code
  bleibt).
- Texte fachlich und kurz; jede Seite beginnt mit dem Wofür.
  „Geräteprofil" statt Preset, „Befehl wiederholen" statt Hold-Modus.
  Typografische „…“-Quotes (ein rohes `"` crasht HA beim Setup).
  **Platzhalter nie in spitzen Klammern** (HAs Markdown-Sanitizer
  verschluckt `<wp>`). **`strings.json`-Touch ⇒ `translations/en.json`
  und `de.json` mitziehen** — HA liest zur Laufzeit nur `translations/`.
  Guards: `tests/test_json_assets.py`.
- Neue Integrations-Klarnamen in `INTEGRATION_DISPLAY_NAMES`; Bundesland
  über `GERMAN_STATES` (Spiegel des Backends), Stadt/Stadtteil Freitext.
- Preset-Auflösung: Identity-Matching **nie** über `unique_id` (PII, das
  Backend 400t); der Resolver rät nie (mehrdeutig → verbatim). HA-Helfer
  (`input_*`) kann der Connector nicht anlegen — nur die Box von außen;
  der Picker informiert und befüllt vor.
- Brand-Icon liegt in `brand/`; sichtbar erst nach PR im `brands`-Fork.

## Stolpersteine

1. `_authenticated_config_request` baut den httpx-Client im Executor
   (HA-Blocking-Call-Warnung, live gefunden).
2. Bestehende `Crowdergy_<Name>`-Alt-Devices/-Sensoren verschwinden erst
   nach Re-Provisionieren oder HA-Neustart; vor v3.40.0 von Hand gelöschte
   Karten haben das Backend-Gerät mitgelöscht.
3. `region` ist ein Plain-Data-Feld (kein Entity-Slot) — braucht keinen
   `_build_device_record`-Eintrag.
4. Sekunden-kurze Actions-`failure` = GitHub-Spending-Limit, keine rote
   Suite.
