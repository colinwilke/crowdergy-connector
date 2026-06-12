# Crowd-Preset-Store — Vertrag

**Status: implementiert** (Backend-Staging/Kuration 2026-06-12, Backlog #9).
Dieses Dokument ist der verbindliche Vertrag zwischen den drei Parteien:

| Partei | Rolle | Code |
|---|---|---|
| **Connector** (dieses Repo, public) | Contribute-Kanal („Setup teilen") + Lookup im Onboarding; trägt das **Slot-Schema** | `preset_spec.py` (SSOT), `config_flow.async_step_contribute_preset_form`, `box_services.box_list_presets` |
| **Backend** (privat) | Store-**Daten**, Staging/Promotion, Kuration — alles hinter Auth (Moat-Regel) | `app/routers/crowd_presets.py` |
| **Box** (privat) | Konsument im Per-Device-Wizard; wendet Presets deterministisch an | `box-manager/app/devices.py` |

Grundsatz: Im public Repo liegt nur das **Schema**. Store-Daten,
Promotion-Logik und Kuration bleiben im Backend hinter Auth
(`/crowd-presets/lookup` ist auth-pflichtig, Entscheidung E-3).

## Preset-Identität & Inhalt

Ein Preset ist eindeutig über den natürlichen Key
**`(device_type, vendor, model)`** (Backend speichert vendor/model wie
eingereicht, keine Normalisierung). Inhalt:

| Feld | Typ | Semantik |
|---|---|---|
| `entity_map` | `{slot: entity_id}` | Entity-Slots des Contributors. **Nicht portabel as-is** — Entity-ID-Präfixe tragen den kundenseitigen Gerätenamen. Konsumenten lösen per verankertem Suffix-Match neu auf (exakter Treffer zuerst; sonst längster `_`-Boundary-Suffix ≥ 6 Zeichen; kürzester Kandidat nur bei reiner Messnamens-Erweiterung, echte Mehrdeutigkeit → `AMBIGUOUS_MAPPING`, nie raten). |
| `value_map` | `{slot: string}` \| `null` | Select-Optionen/Flags der Integration (z.B. `value_battery_mode_active: "External"`). **Verbatim** übernehmen. Boolean-Flags als String `"true"`; nicht gesetzte Slots fehlen (tragen keine Information). `null`/fehlend = Beitrag ohne Value-Slots (Connector < v3.26). |
| `integration_domain` | string \| `null` | HA-Integration der gemappten Entities (aus der Entity-Registry über den ConfigEntry hergeleitet — nie aus Entity-ID-Präfixen). `null` = Legacy-Beitrag; die Box filtert solche Presets raus (sie bietet nur Integrationen ihrer kuratierten Support-Tabelle an). |
| `status` | `"staged"` \| `"approved"` | Siehe Lifecycle. `rejected` erscheint nie im Lookup. |
| `contribution_count` | int | Alle gezählten Beiträge für den Key (auch Mehrfach-Beiträge desselben Users). |
| `updated_at` | ISO-Datetime | Letzte **Mapping**-Änderung (nicht: letzter Beitrag, nicht: Status-Übergang). Basis für den „Verbessertes Profil verfügbar"-Prompt (Backlog #28). |
| `helper_yaml` | string \| `null` | Optionaler HA-Helper-Schnipsel (≤ 4096 Zeichen). |

### Slot-Schema (SSOT: `preset_spec.py`)

`PRESET_SLOT_SPEC` definiert je `device_type`, welche Slots portabel
sind: Pflicht-Entity-Slots (`entity_current_power_kw` überall — ohne
auflösbaren Power-Slot registriert die Box kein Gerät), optionale
Entity-Slots (nicht auflösbar → `missing_slots` an die GUI) und
Value-Slots. `split_device_record()` zerlegt einen Device-Record
entsprechend für den Contribute-Beitrag.

Bewusst NICHT im Preset (installationsspezifisch): `entity_climate`/
`entity_water_heater` (Form-only, kollabieren auf `entity_control`),
`entity_outdoor_temp_c`, `shares_hardware_with_device_id`,
`included_in_haushalt`, Standort-Felder.

**Validierungs-Arbeitsteilung:** Das Backend prüft nur die Shape
(string→string, ≤ 32 Keys je Map, Längen-Bounds). Die Slot-SEMANTIK
erzwingen die Konsumenten beim Anwenden — `box_add_device` lehnt per
Default-DENY (`MAPPABLE_ENTITY_DOMAINS` in `const.py`) jeden
Entity-Slot ab, der nicht explizit zugelassen ist. Invariante
(Test-gesichert): jeder Entity-Slot in `PRESET_SLOT_SPEC` steht in
`MAPPABLE_ENTITY_DOMAINS`.

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
  es ist ab dem ersten Beitrag im Lookup sichtbar. Konsumenten
  kennzeichnen staged Presets („Community"-Badge in der Box-GUI,
  Konzept 2026-06-11).
* **Solange staged gilt newest-wins:** der jeweils neueste Beitrag
  überschreibt `entity_map`/`value_map`/`helper_yaml` (und bumpt
  `updated_at`). `integration_domain` wird nur überschrieben, wenn der
  Beitrag das Feld mitliefert (ein Legacy-Client „entkoppelt" kein
  Box-taugliches Preset).
* **Promotion zählt UNTERSCHIEDLICHE User** (`CROWD_PRESET_THRESHOLD`,
  Default 3, per Env überschreibbar) — Mehrfach-Beiträge desselben
  Accounts promoten nicht. Der Kurator kann jederzeit früher approven.
* **Ab `approved` ist das Mapping EINGEFROREN:** weitere Beiträge
  erhöhen nur `contribution_count`; `updated_at` bleibt stehen.
  Begründung: Steuer-Slots schalten reale Hardware — ein einzelner
  späterer Beitrag darf ein kuratiertes Mapping nicht still
  umschreiben. Mapping-Fixes laufen über den Kurator:
  `stage` → Fix-Beitrag (newest-wins) → `approve`.
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
  "entity_map": {"entity_current_power_kw": "sensor.plenticore_battery_power"},
  "value_map": {"value_battery_mode_active": "External"},
  "integration_domain": "kostal_plenticore",
  "notes": "optional, ≤ 280 Zeichen",
  "helper_yaml": null
}
```

Response: `{"status": "staged" | "approved" | "rejected",
"contribution_count": n}`. `value_map` ist optional — **Backends vor
diesem Vertrag lehnen Requests mit dem Feld ab** (`extra="forbid"`,
422); der Connector schickt es deshalb nur mit, wenn es belegt ist
(Deploy-Reihenfolge: Backend vor Connector-Release).

### `GET /api/v1/crowd-presets/lookup?device_type=…[&vendor=…]`

Liefert staged + approved Presets (rejected nie); Sortierung:
approved zuerst, innerhalb gleichen Status `contribution_count DESC`.
Items tragen alle Felder der Inhalts-Tabelle oben.

### Kuration (nur `users.is_curator`, manuell per psql vergeben)

* `GET /api/v1/crowd-presets/curation/queue` — staged Presets, älteste
  Mapping-Änderung zuerst, mit `distinct_contributors` + Contribution-
  Notes als Entscheidungs-Kontext.
* `POST /api/v1/crowd-presets/curation/decide` —
  `{device_type, vendor, model, decision: "approve" | "reject" | "stage"}`.
  Nicht-Kurator → 403 `FORBIDDEN_NOT_CURATOR`; unbekannter Key → 404
  `PRESET_NOT_FOUND`.

## Kompatibilitäts-Toleranzen (beide Richtungen, getestet gelebt)

* **Box/Connector gegen ALT-Backend:** fehlendes `status`-Feld wird
  als `approved` behandelt (Box: `p.get("status") or "approved"`);
  fehlendes `value_map` = keine Value-Slots. Contribute ohne
  `value_map` bleibt gegen Alt-Backends gültig.
* **NEU-Backend gegen Alt-Clients:** `value_map` ist optional;
  Beiträge ohne das Feld (Connector < v3.26) bleiben gültig.
  Alpha-Bestands-Presets (Sofort-Promotion, Threshold=1) wurden bei
  der Migration auf `approved` gehoben — sie waren bereits live im
  Einsatz.
* **Box-Pin:** Value-Slots mit Punkt in Werten brauchen vendored
  Connector ≥ v3.24.0 (Box pinnt aktuell v3.25.0 ✓).

## Konsumenten-Pflichten beim Anwenden

1. Entity-Slots gegen die EIGENE Installation auflösen (Suffix-Match,
   s.o.); `value_map` verbatim.
2. Steuer-Slots schalten reale Hardware: bereits registrierte Geräte
   NIE stumm auf ein neueres Preset re-applien (Re-Apply nur mit
   explizitem User-Prompt, Backlog #28; `updated_at` liefert das
   Vergleichs-Signal).
3. Herkunft (`vendor`/`model`/`integration_domain`/`source`) pro Gerät
   persistieren.
