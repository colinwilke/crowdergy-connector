# Crowd-Preset-Store — Vertrag

**Status: implementiert** (Connector v3.24.0 „Mapping-Dictionary" +
Backend-Staging/Kuration 2026-06-12, Backlog #9). Dieses Dokument ist
der verbindliche Vertrag zwischen den drei Parteien; es konsolidiert
das User-Konzept „Device-Mapping-Store" (2026-06-11) mit der
umgesetzten Backend-Seite (2026-06-12).

| Partei | Rolle | Code |
|---|---|---|
| **Connector** (dieses Repo, public) | Contribute-Kanal („share setup") + Lookup im Onboarding; trägt das **Slot-Schema** | `preset_spec.py` (SSOT), `config_flow` Contribute-/Picker-Steps, `box_services.{box_list_presets,box_add_device,box_update_device}` |
| **Backend** (privat) | Store-**Daten**, Staging/Promotion, Kuration — alles hinter Auth (Moat-Regel) | `app/routers/crowd_presets.py` |
| **Box** (privat) | Konsument im Per-Device-Wizard; wendet Presets deterministisch an | `box-manager/app/devices.py` |

Grundsatz: Im public Repo liegt nur das **Schema**. Store-Daten,
Promotion-Logik und Kuration bleiben im Backend hinter Auth
(`/crowd-presets/lookup` ist auth-pflichtig, Entscheidung E-3).

## Konzept

1. **Self-Hosted-HA-User submitten ihre Mappings** („share setup" im
   Options-Flow) — seit v3.24.0 für alle preset-fähigen Typen
   (solar, grid, battery, wallbox; seit #68 auch heating, warmwater,
   aircon), mit Vollständigkeits-Gate.
2. **Staging statt Sofort-Veröffentlichung als Endzustand:** jede
   Submission ist eine Contribution-Zeile; das aggregierte Preset hat
   einen Status `staged` → `approved`. Staged Presets werden bewusst
   MIT angeboten — Connector-Picker und Box-GUI kennzeichnen sie als
   „Community, noch unbestätigt", mehr Gating passiert clientseitig
   nicht.
3. **Die Box bietet Presets je Device-Type an** — der Wizard fragt
   Gerätetyp → Hersteller/Modell und zieht dann ALLE Slots des
   Presets (kW + kWh + Steuerung …).

## Preset-Identität & Inhalt

Ein Preset ist eindeutig über den natürlichen Key
**`(device_type, vendor, model)`** — Backend speichert vendor/model
wie eingereicht (Normalisierung auf `lower()` + Key-Erweiterung um
`integration_domain` ist als spätere Migration denkbar, siehe
„Offen / Folgearbeit").

| Feld | Typ | Semantik |
|---|---|---|
| `entity_map` | `{slot: entity_id}` | Entity-Slots des Contributors. **Nicht portabel as-is** — Entity-ID-Präfixe tragen den kundenseitigen Gerätenamen. Konsumenten lösen per verankertem Suffix-Match neu auf (exakter Treffer zuerst; sonst längster `_`-Boundary-Suffix ≥ 6 Zeichen; kürzester Kandidat nur bei reiner Messnamens-Erweiterung, echte Mehrdeutigkeit → `AMBIGUOUS_MAPPING`, nie raten). |
| `value_map` | `{slot: string}` \| `null` | Select-Optionen/Flags der Integration (z.B. `value_battery_mode_active: "External"`). **Verbatim** übernehmen. Boolean-Flags als String `"true"`; nicht gesetzte Slots fehlen. `null`/fehlend = Beitrag ohne Value-Slots (Connector < v3.24). |
| `integration_domain` | string \| `null` | HA-Integration der gemappten Entities (aus der Entity-Registry über den ConfigEntry hergeleitet — nie aus Entity-ID-Präfixen). Bei gemischten Setups die **häufigste** Domain. `null` = Legacy-Beitrag; die Box filtert solche Presets raus. |
| `required_integrations` | `[string]` \| `null` | **Alle DISTINKTEN** HA-Integrationen, die die `entity_map` braucht, damit sie vollständig auflösbar ist (Superset von `integration_domain`, sortiert). Der Connector zeigt sie neuen Usern beim Profil-Pick an („dafür brauchst du folgende Integration(en): …", Klarname mit Slug-Fallback). `null` = Legacy-Beitrag (Connector < 2026-07). Die Box ignoriert das Feld (filtert weiter über `integration_domain`). |
| `required_helpers` | `[HelperSpec]` \| `null` | **Strukturierte Specs der HA-Helper** (`input_select`/`input_number`/`input_boolean`), die die `entity_map` referenziert, die ein empfangender User aber NICHT hat — vom Contributor selbst angelegt (z. B. `input_select.hausbatterie_lademodus`, das eine Modbus-Write-Automation treibt). Consumer (Box/Connector) legen die fehlenden Helfer VOR dem Mapping an und wiren den Slot auf die frisch erzeugte Entity. **Bewusst KEIN Roh-YAML/Template** — nur die drei `input_*`-Typen, damit kein ausführbarer Code an eine config-schreibende Provisionierung wandert (Box-Invariante „kein beliebiger Code"). Der Hardware-Pfad HINTER einem Helfer (Modbus-Automation, Register-Plan, host/slave) ist installations-spezifisch und gehört NICHT hierher (kuratiertes Vendor-Package). `null` = kein helferbasierter Slot / Legacy-Beitrag. |
| `entity_identity_map` | `{slot: Identity}` \| `null` | **Registry-Identität je Entity-Slot** (seit 2026-07-19) — die namens-UNABHÄNGIGE Auflösungs-Grundlage: `{platform, translation_key?, original_name?}`. `platform` = HA-Integration, die die Entity erzeugt hat (Registry `platform`, Pflicht je Eintrag); `translation_key` = stabiler per-Entity-Key der Integration (bevorzugt); `original_name` = Integrations-Default-Name VOR jedem User-Rename (Fallback für Integrationen ohne translation_key). Vom Connector beim Contribute aus der Entity-Registry hergeleitet; Slots ohne Registry-Eintrag (input_*-Helfer, Templates) fehlen. **PII-Regel: `unique_id` wird NIE transportiert** (trägt oft Geräte-Seriennummern) — das Backend lehnt sie hart mit 400 ab. `null` = Legacy-Beitrag → Consumer nutzen nur den Suffix-Match. |
| `status` | `"staged"` \| `"approved"` | Siehe Lifecycle. `rejected` erscheint nie im Lookup. |
| `contribution_count` | int | Alle gezählten Beiträge für den Key (auch Mehrfach-Beiträge desselben Users). |
| `updated_at` | ISO-Datetime | Letzte **Mapping**-Änderung (nicht: letzter Beitrag, nicht: Status-Übergang). Basis für den „Verbessertes Profil verfügbar"-Prompt (Backlog #28). |
| `helper_yaml` | string \| `null` | Optionaler HA-Helper-Schnipsel (≤ 4096 Zeichen). Wird von der Box noch nicht angewendet (siehe „Offen"). |

### Slot-Schema / Mapping-Dictionary (SSOT: `preset_spec.py`)

`PRESET_SLOT_SPEC` definiert je `device_type` die portablen Slots in
drei Arten:

| Art | Transport | Semantik |
|---|---|---|
| `entity` | `entity_map` | HA-Entity des Beitragenden; Konsument löst per Suffix-Match neu auf |
| `value` | `value_map` | Integrationsspezifische Strings (Select-Optionen, Hold-Modus) — auf jeder Installation identisch |
| `flag` | `value_map`, serialisiert `"true"`, nur wenn gesetzt | Vorzeichen-Konventionen u.ä.; fehlend = Default (False) |

`required` markiert das funktionale Minimum (Vollständigkeits-Gate im
Contribute-Flow; das Backend prüft das bewusst NICHT, s.u.):

- **solar**: `entity_current_power_kw`, `entity_energy_total`
- **grid**: `entity_current_power_kw`, `entity_energy_total`
- **battery**: `entity_current_power_kw`, `entity_soc_percent`,
  `entity_battery_mode`, `value_battery_mode_active`,
  `value_battery_mode_passive`, `entity_battery_power_setpoint_w`
- **wallbox**: `entity_current_power_kw`, `entity_energy_total`,
  `entity_charge_mode`. Optional: `entity_wallbox_charge_current_a`
  (Number-Entity Ampere 6–16) — wenn gemappt, wählt der AI im
  „An"/Power-Modus den Ladestrom variabel statt nur volle Leistung
  (2026-06-20).
- **heating / warmwater / aircon** (steuerbare thermische Lasten, #68):
  `entity_current_power_kw`, `entity_control`. Optional: `value_on` /
  `value_off` (Steuerwerte — bei binären Steuer-Entities wie
  switch/input_boolean/light/fan **leer lassen**, der Connector schaltet
  implizit per turn_on/turn_off; nur select/climate/water_heater brauchen
  die Strings), `entity_current_temp_c`, `entity_energy_total`,
  `invert_power_sign`. Zusätzlich heating/aircon: `entity_cool_control`,
  `value_cool_on`, `value_cool_off` (Kühl-Seite — `supports_cooling`
  leitet das Backend daraus + dem aircon-Typ ab, ist KEIN Preset-Slot);
  nur heating: `entity_vorlauf_setpoint_c`, `entity_vorlauf_temp_c`
  (modulierende WP). Pflicht ist also nur Leistung + Steuer-Entity
  (analog wallbox = kW + Lademodus).

kWh-Zähler bei battery/grid sind bewusst optional (nicht jede
Integration exposed getrennte Lade-/Entlade-Zähler); Capabilities
werden in Box-GUI/Picker angezeigt. Härter gaten = `required=True` in
`preset_spec.py`, der Rest der Pipeline folgt der Spec.

`extract_preset_maps()` zerlegt einen Device-Record Allowlist-getrieben
in `(entity_map, value_map)` — **das ist die Anonymisierungs-Schicht**:
nichts außerhalb der Spec verlässt die Installation
(installationsspezifisch und daher NIE im Preset: Standort-Felder,
`shares_hardware_with_device_id`, `included_in_haushalt`,
`entity_outdoor_temp_c`, Form-only-Felder wie `entity_climate`).

**Validierungs-Arbeitsteilung:** Das Backend prüft nur die Shape
(string→string, ≤ 32 Keys je Map, Längen-Bounds, `entity_map`-Werte
müssen wie `<domain>.<object_id>` aussehen). Die Slot-SEMANTIK
erzwingen die Konsumenten beim Anwenden — `box_add_device` prüft nach
Slot-Art: Entity-Slots gegen `MAPPABLE_ENTITY_DOMAINS` (Default-DENY),
Value-/Flag-Slots gegen `PRESET_VALUE_SLOTS`, unbekannte Keys werden
abgelehnt. Invarianten Test-gesichert (`test_preset_spec`).

### `required_helpers` — HelperSpec (HA-Helfer-Provisionierung)

Ein Contributor mappt manchmal einen Slot auf einen **selbst angelegten
HA-Helfer** statt eine native Integrations-Entity — z. B.
`entity_battery_mode → input_select.hausbatterie_lademodus`, den seine
eigene Modbus-Write-Automation liest. Ein empfangender User hat diesen
Helfer nicht → der Slot löst per Suffix-Match nie auf. `required_helpers`
trägt je solchem Slot eine **strukturierte** Spec, aus der die Consumer
den Helfer nachbauen:

```json
[
  {"slot": "entity_battery_mode", "type": "input_select",
   "options": ["Extern", "Automatik", "Laden", "Entladen"],
   "name": "Batterie-Lademodus"},
  {"slot": "entity_battery_power_setpoint_w", "type": "input_number",
   "min": -10000, "max": 10000, "step": 100, "unit": "W"}
]
```

HelperSpec-Felder (Backend validiert die Shape, Zahlen → float):

| Feld | Typ | Für | Semantik |
|---|---|---|---|
| `slot` | str (≤ 64) | alle | Entity-Slot, den der Helfer backt (Key in `entity_map`) |
| `type` | str | alle | `input_select` \| `input_number` \| `input_boolean` — **Allowlist, sonst 400** (kein Template/Automation → kein Code) |
| `name` | str \| — | optional | Anzeigename des Helfers (Fallback: Consumer generiert einen) |
| `options` | `[str]` (1..64) | `input_select` | die Auswahl-Optionen (Pflicht) |
| `min` / `max` | number | `input_number` | Range (Pflicht, `max` > `min`) |
| `step` | number > 0 | `input_number` | optionale Schrittweite |
| `unit` | str (≤ 32) | `input_number` | optionale Einheit |

**Grenze (bewusst):** `required_helpers` provisioniert nur den Helfer
selbst. Die **Hardware-Brücke dahinter** (Modbus-Automation,
Register-Plan, host/slave) ist installations-spezifisch und wird NICHT
über einen anonymen Crowd-Beitrag verteilt — sie gehört in ein
kuratiertes Vendor-Package (z. B. Kostal-Builtin-Modbus, Backlog #22).
Für read-only Template-Sensoren, die nur eine Ableitung berechnen, ist
`required_helpers` (v1) noch nicht zuständig (Template = Code → separater,
kuratierter Pfad).

### `entity_identity_map` — namens-unabhängige Entity-Auflösung

Entity-IDs tragen den frei wählbaren Gerätenamen des Contributors als
Präfix (`sensor.solar_battery_power` vs
`sensor.wechselrichter_battery_power`) — der Suffix-Match neutralisiert
das nur, solange der Empfänger die INTEGRATIONS-Hälfte der object_id
unangetastet lässt (ein Entity-Rename bricht ihn). Die Registry-Identität
löst beides:

```json
"entity_identity_map": {
  "entity_current_power_kw": {
    "platform": "kostal_plenticore",
    "translation_key": "battery_power"
  },
  "entity_battery_mode": {
    "platform": "kostal_plenticore",
    "original_name": "Battery Operating Mode"
  }
}
```

**Auflösungs-Leiter beim Anwenden (Box `match_entity_identity` /
Connector `resolve_preset_entities`), fail-safe eskalierend:**

1. **Exakte ID** existiert beim Empfänger → verwenden.
2. **Identität:** same-domain-Entities der `platform`, erst
   `translation_key`-Gleichheit, wenn das LEER ausgeht (älterer
   Integrations-Stand) `original_name`-Gleichheit. Genau EIN Treffer →
   auflösen; mehrere (echtes Multi-Inverter-Setup) → Box:
   `AMBIGUOUS_MAPPING` mit Kandidaten (#29-GUI-Picker), Connector:
   Contributor-ID verbatim belassen (der Mensch wählt im Entity-Step).
   **Nie raten.**
3. **Suffix-Match** (Legacy-Verhalten) für Presets ohne Identität bzw.
   Slots, die die Identity-Leiter nicht auflöst.

Die Identität wird beim Contribute erfasst (`entity_mapper.
entity_identity_map`), nur-wenn-mitgeliefert auf staged Presets
überschrieben (ein Alt-Connector entfernt sie nicht wieder) und vom
Kurator-Upsert unbedingt geschrieben (Admin-Seite schickt das Feld
send-always). Backend-Validierung: Key-Allowlist
`{platform, translation_key, original_name}` — **`unique_id` → 400**
(Seriennummern-PII).

## Lifecycle: Staging → Approval → Kuration

```
Contribute ──► staged ──(≥ CROWD_PRESET_THRESHOLD distinct User)──► approved
                 ▲  │                                                  │
                 │  └────────────── Kurator: approve ──────────────────┤
                 │                                                     │
                 └── Kurator: stage ◄──────────────┐                   │
                                                   │                   │
                            Kurator: reject ──► rejected ◄─────────────┘
```

* **Jeder Beitrag erzeugt/aktualisiert sofort ein `staged` Preset** —
  ab dem ersten Beitrag im Lookup sichtbar (gekennzeichnet).
* **Solange staged gilt newest-wins:** der jeweils neueste Beitrag
  überschreibt `entity_map`/`value_map`/`helper_yaml` (und bumpt
  `updated_at`). `integration_domain` wird nur überschrieben, wenn der
  Beitrag das Feld mitliefert (ein Legacy-Client „entkoppelt" kein
  Box-taugliches Preset).
* **Promotion zählt UNTERSCHIEDLICHE User** (`CROWD_PRESET_THRESHOLD`,
  Default **3**, per Env überschreibbar — User-Entscheidung
  2026-06-12; das frühere „anfangs Threshold=1" ist damit obsolet,
  weil staged Presets ohnehin angeboten werden). Mehrfach-Beiträge
  desselben Accounts promoten nicht. Der Kurator kann jederzeit
  früher approven.
* **Ab `approved` ist das Mapping EINGEFROREN:** weitere Beiträge
  erhöhen nur `contribution_count`; `updated_at` bleibt stehen.
  Begründung: Steuer-Slots schalten reale Hardware — ein einzelner
  späterer Beitrag darf ein kuratiertes Mapping nicht still
  umschreiben. Mapping-Fixes laufen über den Kurator:
  `stage` → Fix-Beitrag (newest-wins) → `approve`.
  `approved → rejected` ist erlaubt (Notbremse); bereits angewendete
  Geräte behalten ihr Mapping (Re-Apply nur via Prompt, Backlog #28).
* **`rejected`** (nur per Kurator) verschwindet aus dem Lookup und
  wird von weiteren Beiträgen NICHT resurrected (Beiträge werden als
  Audit-Zeilen trotzdem gespeichert, Response-Status `rejected`).
* **Kurator-Beiträge sind AUTORITATIV (Install-Box-Workflow,
  2026-07-16):** ein Contribute eines Accounts mit `is_curator=true`
  wird sofort `approved` (kein Threshold) und überschreibt das Mapping
  auch über den `approved`-Freeze und ein `rejected` hinweg — dieselbe
  Semantik wie der Kurator-Upsert `PUT /curation/preset`, nur über den
  normalen Connector-Contribute-Flow. Damit mappt die Install-Box
  (privates Box-Repo, `docs/install-box.md`) ein Gerät beim Kunden und
  die Kunden-Box zieht das Preset live, ohne separaten Approve-Schritt.
  Ein Mapping-Update bumpt `updated_at` → Bestands-Boxen bekommen den
  Re-Apply-Prompt (#28). Community-Beiträge sind unverändert (staged /
  Threshold / Freeze).

## API (Backend, alle auth-pflichtig, Bearer-Header)

### `POST /api/v1/crowd-presets/contribute`

```json
{
  "device_type": "battery",
  "vendor": "KOSTAL",
  "model": "Plenticore plus 8.5",
  "entity_map": {"entity_current_power_kw": "sensor.x_battery_power"},
  "value_map": {"value_battery_mode_active": "External", "battery_setpoint_invert_sign": "true"},
  "integration_domain": "kostal_plenticore",
  "required_helpers": [
    {"slot": "entity_battery_mode", "type": "input_select",
     "options": ["Extern", "Automatik"], "name": "Lademodus"}
  ],
  "entity_identity_map": {
    "entity_current_power_kw": {
      "platform": "kostal_plenticore", "translation_key": "battery_power"
    }
  },
  "notes": "optional, ≤ 280 Zeichen",
  "helper_yaml": null
}
```

Response: `{"status": "staged" | "approved" | "rejected",
"contribution_count": n}`.

- `value_map` ist optional (Solar-Presets tragen meist keins;
  Connector < v3.24 schickt das Feld nie; der Connector schickt es
  nur mit, wenn es belegt ist).
- **Das Backend lehnt unbekannte Felder NICHT mit 422 ab**
  (`extra="ignore"`) — ein neuerer Connector darf Felder schicken,
  die ein älteres Backend noch nicht kennt.
- Backend-Validierung: Shape only (s.o.). Pflicht-Slot-
  Vollständigkeit gated der Contribute-Flow clientseitig; ein
  Required-Set-Spiegel im Backend wäre Schema-Drift-Risiko und kommt
  (wenn überhaupt) als Auto-Check in der Kurations-Queue.

### `GET /api/v1/crowd-presets/lookup?device_type=X[&vendor=Y]`

Liefert staged + approved Presets (rejected nie) mit allen Feldern der
Inhalts-Tabelle; Sortierung: `approved` vor `staged`, innerhalb dessen
`contribution_count DESC` (Clients verlassen sich nicht darauf, zeigen
aber in Reihenfolge an).

### Kuration (Stufe 1, implementiert 2026-06-12)

Gate: `users.is_curator` (manuell per psql, kein Self-Service) —
normales User-JWT, kein separater Auth-Pfad. Nicht-Kurator → 403
`FORBIDDEN_NOT_CURATOR`.

* `GET /api/v1/crowd-presets/curation/queue` — staged Presets,
  älteste Mapping-Änderung zuerst, mit `distinct_contributors` +
  Contribution-Notes als Entscheidungs-Kontext.
* `POST /api/v1/crowd-presets/curation/decide` —
  `{device_type, vendor, model, decision: "approve" | "reject" | "stage"}`;
  unbekannter Key → 404 `PRESET_NOT_FOUND`. Contribution-Audit-Zeilen
  ziehen mit (approve: pending→active, reject: →rejected, stage:
  →pending).
* `GET /api/v1/crowd-presets/curation/all` — wie `/queue`, aber ALLE
  Status (staged/approved/rejected) für die Admin-Tabelle; sortiert nach
  Status-Gruppe (staged, approved, rejected), dann device_type/vendor/model.
* `PUT /api/v1/crowd-presets/curation/preset` — Kurator-Direkt-Upsert
  (`PresetEditRequest`: wie Contribute minus `notes`, plus optionalem
  `status`). Schreibt entity_map/value_map/helper_yaml/integration_domain/
  required_integrations DIREKT auf `VendorPreset` — **bewusst auch über
  den `approved`-Freeze hinweg** (der Kurator ist autoritativ; genau dafür
  ist die Bearbeiten-Funktion da). Unbekannter Key → neue Zeile
  (Default-Status `approved`, override via `status`). Key-Felder
  unveränderlich; „Umbenennen" = neue Zeile + alte rejecten.
  `contribution_count` bleibt unangetastet.
* `GET /api/v1/crowd-presets/admin` — self-contained HTML-Tabelle
  (Kurator-Login → Liste → Inline-Edit/Neu-Anlegen/Ablehnen). Der Shell
  ist auth-frei, ALLE Datenoperationen laufen curator-gated. Bewusst NUR
  am API-Host `api.theothergas.de` (die crowdergy.de-nginx-CSP würde das
  Inline-JS blocken; die App selbst setzt keine CSP). „Löschen" in der UI
  = `decide reject` (kein Hard-Delete).
* **Empirische Validierung:** der Self-Hosted-Preset-Picker ist das
  Test-Vehikel — ein staged Preset gegen die eigene Hardware (KOSTAL)
  anwenden, bevor es approved wird.

## Kompatibilitäts-Toleranzen (beide Richtungen)

* **Box/Connector gegen ALT-Backend:** fehlendes `status`-Feld wird
  als `approved` behandelt; fehlendes `value_map` = keine Value-Slots
  (Werte-Steps zeigen keine Vorschläge). Contribute ohne `value_map`
  bleibt gegen Alt-Backends gültig.
* **NEU-Backend gegen Alt-Clients:** `value_map` optional; Beiträge
  ohne das Feld bleiben gültig. Alpha-Bestands-Presets
  (Sofort-Promotion) wurden bei Migration `20260612_0001` auf
  `approved` gehoben (sie waren live im Einsatz/verifiziert).
* **Box-Pin:** Battery-/Wallbox-Presets (Value-Slots, auch mit Punkt
  im Wert) brauchen vendored Connector **≥ v3.24.0**.
* **Deploy-Reihenfolge:** Backend-Migration `20260612_0001` vor dem
  Connector-Tag ≥ v3.26.0 ausrollen.

## Konsumenten-Pflichten beim Anwenden

1. Entity-Slots gegen die EIGENE Installation auflösen (exakt →
   Registry-Identität → Suffix-Match, s. „entity_identity_map");
   `value_map` verbatim. **`required_helpers` beim Anwenden
   berücksichtigen — je nach Consumer verschieden:**
   - **Box** (steuert HA von außen per WebSocket): legt fehlende Helfer
     VOR dem Mapping automatisch an (`<domain>/create`, mit der
     Contributor-object_id → exakter Match), idempotent (State-Check → kein
     Dublett), fail-soft (Create-Fehler → Slot bleibt `missing`, kein
     Wizard-Abbruch).
   - **Connector** (läuft INNERHALB der HA): kann `input_*`-Helfer NICHT
     zuverlässig selbst anlegen (keine stabile HA-API — die Storage-
     Collection ist nicht abrufbar). Er INFORMIERT den User am Profil-
     Picker („· HA-Helfer nötig: …") und füllt die Helfer-Slot-IDs im
     Entity-Step als Vorschlag vor; der User legt die Helfer einmal in HA
     an (Einstellungen → Geräte & Dienste → Helfer).
2. Steuer-Slots schalten reale Hardware: bereits registrierte Geräte
   NIE stumm auf ein neueres Preset re-applien (Re-Apply nur mit
   explizitem User-Prompt, Backlog #28; `updated_at` liefert das
   Vergleichs-Signal). **Box-Implementierung (2026-06-17):** der
   box-manager vergleicht `lookup.updated_at > device.crowd_preset_updated_at`
   (`GET /setup/preset-updates`) und re-applied auf User-Klick
   (`POST /setup/device/{id}/reapply-preset`) IN PLACE über den
   Connector-Service **`box_update_device`** (PUT statt POST → kein
   Backend-Duplikat; das Gerät behält `device_id`, Telemetrie-Historie
   und Topologie). Statisch (Heuristik-Fallback) gemappte Geräte bekommen
   denselben Prompt, sobald erstmals ein kuratiertes Preset auftaucht.
3. Herkunft (`vendor`/`model`/`integration_domain`/`source`) pro Gerät
   persistieren (Box zusätzlich: `crowd_preset_updated_at` = `updated_at`
   des angewandten Presets, Baseline für #28).

## Admin-Ausbau (Stufe 2 — teilweise umgesetzt 2026-07-04)

Die Kurator-Tabelle (`GET /admin` + `/curation/all` + `PUT /curation/preset`,
s.o.) realisiert den Kern von Stufe 2: **alle Status listen, Maps inline
bearbeiten, neue Zeile anlegen, ablehnen**. Bewusste Abweichungen vom
Ur-Design (User-Entscheid 2026-07-04):

- **Erreichbarkeit:** die Tabelle ist am **öffentlichen API-Host** erreichbar
  (curator-gated statt localhost/SSH-Tunnel), damit sie ohne Tunnel im
  Browser läuft (CORS ist zu → same-origin-Zwang; die App hat keine CSP).
  Das Ur-Design „nur localhost binden" ist damit überholt.
- **Kein Hard-Delete** — „Löschen" = `reject` (Profil verschwindet aus dem
  Lookup, Zeile bleibt sichtbar + wieder freigebbar). Ein echter DELETE
  (DSGVO/Abuse) bleibt offen.

Noch offen aus dem Ur-Design:

- **Erreichbarkeit (Ur-Fassung, überholt):** Admin-Router nur auf localhost
  binden, Zugriff per SSH-Tunnel (bestehendes Staging-Muster) — kein
  öffentlicher Admin-Endpunkt, solange es genau einen Admin gibt.
- **Endpoints:** `GET /admin/crowd-presets?status=…` (Queue inkl.
  approved/rejected), Detail mit allen Roh-Contributions +
  Konsens-Diff (Fingerprint-Vergleich: je Slot der Messnamens-Suffix,
  wie Box-`match_entity`), approve/reject mit `reason` + optional
  korrigierten Maps (`source=admin_edited`, Original bleibt),
  `DELETE` für Preset/einzelne Contributions (**Hard-Delete nur für
  DSGVO-/Abuse-Fälle** — Normalfall ist `reject`; Löschung wird als
  Audit-Ereignis ohne Inhalt protokolliert).
- **Auto-Checks** (Ampel vor dem Augen-Review): Pflicht-Slots
  vollständig, Entity-Domain plausibel pro Slot, Suffix-Matchbarkeit
  (object_id braucht `_`-Suffix ≥ 6 Zeichen), value_map-Werte nicht
  leer, notes-PII-Heuristik.
- **User-Übersicht** (datenminimal: Betriebs-Metadaten ja,
  Energiedaten/Standort-Detail/Hashes/Tokens nein; Admin-Abrufe
  auditieren): `GET /admin/users[/{id}]` mit Devices-Metadaten,
  Box-/Connector-Status, Contributions. Stufe-2-Aktionen:
  Force-Logout, Box-Trennung, Account-Löschung (Art. 17).
- **Audit:** jede Statusänderung append-only (wer/wann/was/Grund).
- **UI:** server-rendered Mini-Seite unter `/admin` (Jinja) oder
  CLI-Script; später Benachrichtigung „N neue staged Presets" +
  Telemetrie-Plausibilitäts-Loop nach Approve.

## Konsument iOS: `apply-preset` (Backlog #38, implementiert 2026-06-15)

Der iOS-Preset-Picker (Backlog #37) hat keine HA-Scope — er wendet ein
Preset über das **Backend** an, nicht über den Connector:

### `POST /api/v1/devices/{device_id}/apply-preset`

Body `{"vendor": str, "model": str}` (auth-pflichtig, User-JWT). Server:

1. Preset über `(device.type, vendor, model)` in `vendor_presets` suchen
   (status ∈ {staged, approved}); sonst **404** `PRESET_NOT_FOUND`.
2. **Provenance stempeln:** `device.vendor`, `device.model`,
   `device.crowd_preset_updated_at = preset.last_updated_at` (= das
   `updated_at` des Lookups). Letzteres trägt den „Verbessertes Profil
   verfügbar"-Vergleich (`lookup.updated_at > device.crowd_preset_updated_at`).
3. **`value_map`-Arbeitsteilung (Steuer-Slots → reale Hardware):**
   - Das **Backend** schreibt NUR die value_map-Slots, deren Key EXAKT
     einer schreibbaren Device-Spalte entspricht (Default-DENY-Allowlist:
     `charge_mode_value_lock/power/solar`, `value_cool_on/off`) — 1:1,
     verbatim, ungefährlich.
   - Non-1:1-/inverter-abhängige Slots (Batterie-Modi
     `value_battery_mode_active/passive` → `battery_value_charge/idle/
     discharge/passive`, Flags) schreibt das Backend BEWUSST NICHT — die
     Auflösung lebt im Connector-`device_field_spec`.
   - `entity_map` ignoriert das Backend komplett (Connector-Owned).
4. **SSE-Broadcast** `{"type": "device_update", "device_id": …}` an den
   User-Stream + `kick_user_solve`.
5. Response: aktualisiertes `DeviceResponse` (jetzt inkl. `vendor`,
   `model`, `crowd_preset_updated_at`).

**Connector-Folgearbeit (offen):** Der Connector soll on-`device_update`
das Preset neu aus dem Store ziehen und den **vollen** `value_map` (inkl.
Batterie-Modi) via `device_field_spec` auf HA anwenden — erst damit ist
ein iOS-`apply-preset` für Batterie-Geräte end-to-end. Backlog-Item in
`crowdergy-ios/CLAUDE.md` Cluster C. Bis dahin: Provenance + die
Exakt-Match-Slots wirken, der Rest braucht den Connector-Hook.

## Offen / Folgearbeit

- **Rate-Limit auf `/crowd-presets/lookup`** (+ moderat auf
  `/contribute`) + ToS-Klausel „Preset-Daten nicht für Drittprodukte"
  (Backlog #10) — die Store-DATEN sind das Asset.
- **Key-Normalisierung:** `lower(vendor/model)` + ggf.
  `integration_domain` in den Identitäts-Key (Migration; heute:
  verbatim-Key, Duplikate kuratierbar).
- **Account-Löschung vs. Contributions:** heute löscht der
  FK-CASCADE die Contribution-Zeilen mit dem Account; das Aggregat
  (Preset) bleibt. Das Design „user_id anonymisieren statt löschen"
  (Mapping-Daten sind technisch) ist offen — braucht eine bewusste
  DSGVO-Entscheidung.
- **`helper_yaml`-Pfad:** das rohe YAML-Feld existiert im Vertrag, wird
  aber von keinem Consumer angewendet (Roh-YAML = Code-Fläche). Der
  strukturierte Nachfolger ist **`required_helpers`** (s. o.) —
  input_select/input_number/input_boolean werden seit 2026-07 von Box +
  Connector provisioniert. `helper_yaml` bleibt nur als Kurator-Notiz-
  Feld; ein kuratierter Template-Sensor-/Modbus-Package-Pfad ist separat
  (Backlog #22).
- **Wallbox auf der Box:** Spec + box_add_device können es; es fehlt
  eine Wallbox-Integration in der kuratierten Support-Tabelle
  (`crowdergy-box/box-manager/app/integrations.py`).
- **Fingerprint-/Median-Aggregation** statt newest-wins, sobald
  Mehrfach-Beiträge real auftreten.
