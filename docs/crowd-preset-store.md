# Crowd-Preset-Store — Mapping-Dictionary mit Staging

Stand 2026-06-11 (User-Konzept „Device-Mapping-Store"). Dieses Dokument
ist der **Vertrag zwischen Connector (ab v3.24.0), Backend und Box** —
das Backend-Repo war in der Implementierungs-Session nicht im Scope,
Connector + Box sind bereits gegen diesen Vertrag gebaut und tolerieren
das heutige Backend (fehlende Felder ⇒ Altverhalten).

## Konzept

1. **Self-Hosted-HA-User submitten ihre Mappings** („share setup" im
   Options-Flow) — seit v3.24.0 für alle preset-fähigen Typen
   (solar, grid, battery, wallbox), nicht mehr solar-only.
2. **Staging statt Sofort-Veröffentlichung als Endzustand:** jede
   Submission landet als Contribution-Zeile; der aggregierte Preset
   hat einen Status `staged` → `approved`. Promotion, wenn genug
   VERSCHIEDENE User dasselbe submitten (Threshold). **Anfangs wird
   der Approval-Schritt übersprungen** (Threshold = 1 bzw. Lookup
   liefert auch `staged` aus) — die Clients sind darauf vorbereitet:
   Connector-Picker und Box-GUI kennzeichnen `staged` als „Community,
   noch unbestätigt", mehr Gating passiert clientseitig nicht.
3. **Die Box bietet approvte (anfangs: alle) Presets je Device-Type
   an** — der Wizard fragt Gerätetyp → Hersteller/Modell und zieht
   dann ALLE Slots des Presets (kW + kWh + Steuerung …), nicht mehr
   pauschal drei kWh-Geräte pro Hersteller.

## Mapping-Dictionary (Slot-Schema)

Single Source of Truth: `custom_components/theothergas/preset_spec.py`
(`PRESET_SLOT_SPEC`). Pro Gerätetyp drei Slot-Arten:

| Art | Transport | Semantik |
|---|---|---|
| `entity` | `entity_map` (slot → entity_id) | HA-Entity des Beitragenden; Box löst per Suffix-Match gegen eigene Entities auf (Präfix = kundenseitiger Gerätename, nicht portabel) |
| `value` | `value_map` (slot → string) | Integrationsspezifische Strings (Select-Optionen, Hold-Modus) — auf jeder Installation identisch, Box übernimmt verbatim |
| `flag` | `value_map`, serialisiert als `"true"`, nur wenn gesetzt | Vorzeichen-Konventionen u.ä.; fehlend = Default (False) |

Pflicht-Slots (Vollständigkeits-Gate im Contribute-Flow; Backend soll
identisch validieren):

- **solar**: `entity_current_power_kw`, `entity_energy_total`
- **grid**: `entity_current_power_kw`, `entity_energy_total`
- **battery**: `entity_current_power_kw`, `entity_soc_percent`,
  `entity_battery_mode`, `value_battery_mode_active`,
  `value_battery_mode_passive`, `entity_battery_power_setpoint_w`
- **wallbox**: `entity_current_power_kw`, `entity_energy_total`,
  `entity_charge_mode`

kWh-Zähler bei battery/grid sind bewusst optional (nicht jede
Integration exposed getrennte Lade-/Entlade-Zähler); Capability wird
in Box-GUI/Picker angezeigt. Wer härter gaten will: `required=True`
in `preset_spec.py`, der Rest der Pipeline folgt.

## API-Vertrag

### `POST /api/v1/crowd-presets/contribute` (Bearer, User-JWT)

```json
{
  "device_type": "battery",
  "vendor": "KOSTAL",
  "model": "Plenticore plus 8.5",
  "entity_map": {"entity_current_power_kw": "sensor.x_battery_power", "...": "..."},
  "value_map": {"value_battery_mode_active": "External", "battery_setpoint_invert_sign": "true"},
  "integration_domain": "kostal_plenticore",
  "notes": "optional, ≤ 280 Zeichen"
}
```

- `value_map` ist NEU und optional (Solar-Presets tragen meist keins;
  Alt-Connector schickt das Feld nie). **Backend darf unbekannte
  Felder nicht mit 422 ablehnen** (Pydantic: extra=ignore genügt).
- Backend-Validierung: Slot-Keys gegen das Dictionary (Allowlist je
  device_type), Pflicht-Slots vollständig, Werte ≤ 255 Zeichen,
  `entity_map`-Werte müssen wie `<domain>.<object_id>` aussehen.
- Response: `{"status": "staged" | "approved", "contribution_count": n}`
  (wie bisher — der Flow zeigt beides im Erfolgs-Screen an).

### `GET /api/v1/crowd-presets/lookup?device_type=X[&vendor=Y]`

```json
{"presets": [{
  "device_type": "battery",
  "vendor": "KOSTAL",
  "model": "Plenticore plus 8.5",
  "integration_domain": "kostal_plenticore",
  "entity_map": {"...": "..."},
  "value_map": {"...": "..."},
  "status": "approved",
  "contribution_count": 4,
  "helper_yaml": null
}]}
```

- NEU: `value_map`, `status`. Beide optional — Connector/Box behandeln
  fehlendes `value_map` als leer und fehlenden `status` als
  `approved`.
- Sortierung: `approved` vor `staged`, innerhalb dessen
  `contribution_count` absteigend (Client verlässt sich nicht darauf,
  zeigt aber in Reihenfolge an).
- Lookup liefert in der Anfangsphase auch `staged` aus
  (Konfig-Schalter, siehe unten).

## Backend-Datenmodell (zu implementieren)

- `crowd_preset_contributions`: Roh-Submissions
  (id, user_id, device_type, vendor_norm, model_norm,
  integration_domain, entity_map JSONB, value_map JSONB, notes,
  created_at). Eine Zeile pro Submission, nie überschreiben
  (Audit/Abuse-Nachvollziehbarkeit).
- `crowd_presets`: Aggregat je Identitäts-Key
  **(device_type, integration_domain, lower(vendor), lower(model))**
  mit status (`staged` | `approved` | `rejected`), contribution_count,
  distinct_user_count, dem aktuell servierten entity_map/value_map
  (= das der JÜNGSTEN Submission eines Mehrheits-Fingerprints; v0:
  schlicht die jüngste) und timestamps.
- **Fingerprint für „gleiches Mapping":** je entity_map-Slot nur den
  Messnamens-Suffix vergleichen (object_id ab dem letzten
  Präfix-Segment ist nicht zuverlässig trennbar — pragmatisch:
  längster gemeinsamer `_`-Suffix ≥ 6 Zeichen, wie Box-`match_entity`).
  v0 darf vereinfachen: gleicher Identitäts-Key = gleiche Contribution
  (count++), Fingerprint-Vergleich kommt mit dem echten Approval.
- **Promotion:** `distinct_user_count ≥ CROWD_PRESET_PROMOTION_THRESHOLD`
  ⇒ `approved`. **Anfangsphase: Threshold=1** (oder Lookup mit
  `CROWD_PRESET_SERVE_STAGED=1`) — genau das meint „anfangs
  überspringen wir das und bieten es direkt auf der Box an".
  `rejected` (manuell, Abuse) verschwindet aus dem Lookup.
- Bestands-Migration: vorhandene Solar-Presets bekommen
  status=`approved` (sie sind live verifiziert).

## Admin / Kuration (Design-Vorschlag 2026-06-12 — mit dem Store umzusetzen)

Admin-Zugang gehört ins **Backend** — einziger Ort, an dem Store-Daten
und Auth bereits liegen. Box/iOS sind Kundenkanäle, der Connector ist
public Code; keiner davon taugt als Admin-Oberfläche.

- **AuthZ:** `users.is_admin`-Spalte (oder `ADMIN_USER_IDS`-Env) +
  `require_admin`-Dependency. Erreichbarkeit Stufe 1: Admin-Router nur
  auf localhost binden, Zugriff per SSH-Tunnel (bestehendes
  Staging-Muster) — kein öffentlicher Admin-Endpunkt, solange es genau
  einen Admin gibt.
- **Endpoints:**
  - `GET /admin/crowd-presets?status=staged|approved|rejected` (Queue,
    sortiert nach distinct_user_count/Alter)
  - `GET /admin/crowd-presets/{id}` — Aggregat + alle Roh-Contributions
    (Zeitstempel, User-ID pseudonymisiert) + Auto-Check-Ergebnisse +
    Konsens-Diff (Fingerprint-Vergleich bei mehreren Contributions,
    Abweichler markiert)
  - `POST /admin/crowd-presets/{id}/approve` / `/reject` — body:
    `reason`, optional korrigierte entity_map/value_map (dann
    `source=admin_edited`, Original bleibt in den Contributions)
  - `approved → rejected` ist erlaubt (Notbremse: falsche Steuer-Slots
    schalten reale Hardware). Verschwindet sofort aus dem Lookup;
    bereits angewendete Geräte behalten ihr Mapping (→ Re-Apply-Prompt,
    siehe Folgearbeit).
- **Auto-Checks** (Ampel pro Preset, vor dem Augen-Review): Pflicht-
  Slots vollständig (Spec-Mirror), Entity-Domain plausibel pro Slot
  (sensor auf Read-, select/number auf Steuer-Slots), **Suffix-
  Matchbarkeit** (object_id braucht einen `_`-Suffix ≥ 6 Zeichen —
  sonst kann keine Box die ID je auflösen), value_map-Werte nicht
  leer, notes-Länge/PII-Heuristik.
- **Audit:** jede Statusänderung append-only (wer/wann/was/Grund).
- **UI Stufe 1:** server-rendered Mini-Seite unter `/admin` (Jinja,
  kein Build-Step) — Tabellen + Diff sind visuell; CLI-Script als
  Alternative. **Stufe 2:** Benachrichtigung „N neue staged Presets",
  Telemetrie-Plausibilitäts-Loop nach Approve (liefern Boxen mit
  diesem Preset realistische kW/SoC-Werte?).
- **Empirische Validierung:** der Self-Hosted-Preset-Picker ist das
  Test-Vehikel — ein staged Preset gegen die eigene Hardware (KOSTAL)
  anwenden, bevor es approved wird.

## Offen / Folgearbeit

- **Rate-Limit auf `/crowd-presets/lookup`** (und moderat auf
  `/contribute`) + ToS-Klausel „Preset-Daten nicht für Drittprodukte":
  das public Connector-Repo enthält nur das SCHEMA, die Store-DATEN
  sind das Asset — ein einzelner (auch zahlender) Account darf den
  Store nicht komplett abziehen können (User-Frage 2026-06-11).
- **`updated_at` / Preset-Version im Lookup-Response**: Grundlage für
  den künftigen Box-Prompt „Verbessertes Profil verfügbar →
  übernehmen?" — die Box persistiert seit 2026-06-11 pro Gerät die
  Herkunft (vendor/model/integration_domain/source). Steuer-Slots nie
  still auto-re-applien.
- `helper_yaml`-Pfad (Steuer-Entities, die beim Beitragenden
  HA-HELPERS sind, z.B. input_select fürs Modbus-Schreiben): Feld
  existiert im Vertrag, wird aber von der Box noch nicht angewendet —
  Presets, deren Steuerung auf Helpers zeigt, funktionieren auf der
  Box erst mit Helper-Provisionierung. Native Integration-Entities
  (Select/Number) funktionieren heute schon.
- Echte Approval-UI / Admin-Override-Endpoint (Backend) — bis dahin
  Threshold-Env.
- Wallbox auf der Box: Slot-Spec + box_add_device können es bereits;
  es fehlt eine Wallbox-Integration in der kuratierten
  Box-Support-Tabelle (`crowdergy-box/box-manager/app/integrations.py`).
