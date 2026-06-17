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
   (solar, grid, battery, wallbox), mit Vollständigkeits-Gate.
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
| `integration_domain` | string \| `null` | HA-Integration der gemappten Entities (aus der Entity-Registry über den ConfigEntry hergeleitet — nie aus Entity-ID-Präfixen). `null` = Legacy-Beitrag; die Box filtert solche Presets raus. |
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
  `entity_charge_mode`

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

1. Entity-Slots gegen die EIGENE Installation auflösen (Suffix-Match,
   s.o.); `value_map` verbatim.
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

## Admin-Ausbau (Design-Vorschlag 2026-06-12 — Stufe 2, noch nicht umgesetzt)

Die implementierte Kurations-API oben ist Stufe 1. Der weitergehende
Admin-Bereich bleibt als abgestimmtes Design dokumentiert:

- **Erreichbarkeit:** Admin-Router nur auf localhost binden, Zugriff
  per SSH-Tunnel (bestehendes Staging-Muster) — kein öffentlicher
  Admin-Endpunkt, solange es genau einen Admin gibt.
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
- **`helper_yaml`-Pfad:** Feld existiert im Vertrag, die Box wendet
  es nicht an — Presets mit Steuer-Slots auf HA-HELPERS funktionieren
  auf der Box erst mit Helper-Provisionierung; native
  Integration-Entities (Select/Number) funktionieren heute.
- **Wallbox auf der Box:** Spec + box_add_device können es; es fehlt
  eine Wallbox-Integration in der kuratierten Support-Tabelle
  (`crowdergy-box/box-manager/app/integrations.py`).
- **Fingerprint-/Median-Aggregation** statt newest-wins, sobald
  Mehrfach-Beiträge real auftreten.
