"""Auto-Mapping Heuristik (Phase 1) für den Crowdergy-Connector.

Scant die laufende HA-Instanz, klassifiziert jede Power/Energy/SoC/
Steuer-Entity nach Crowdergy-Gerätetyp + Attribut-Slot und gruppiert
Vorschläge pro HA-„Device" (Geräte-Registry-Eintrag). Reines Read-only
+ deterministisch — kein Backend-Call, kein LLM. Stufe 2 (LLM-Fallback)
folgt in einem späteren Patch und greift auf dieselben Datenstrukturen
zurück.

Output ist eine Liste `DeviceGroup`s, die der Config-Flow in den
Auto-Confirm-Step rendert. Pro Slot eine `MappingCandidate` mit
Confidence + Begründung, sodass die UI Marker („sicher" vs „bitte
prüfen") setzen kann.

Wichtige Designentscheidungen:
- Werte-Strings (value_on, charge_mode_value_*) sind
  KEINE Entities, sondern Optionen einer Select/Number-Entity. Die
  Heuristik findet nur die *Entity*; die spätere Option-Zuordnung
  bleibt im manuellen Werte-Step.
- Wir nutzen das HA-DeviceRegistry als Gruppierungsanker. Eine
  Sonnen-Battery z.B. taucht in HA als ein DeviceEntry auf, das alle
  zugehörigen Sensoren / Switches besitzt — perfekter Schnitt für
  ein Crowdergy-`Device`.
- Entities ohne `device_id` (z.B. Template-Sensoren) werden in einer
  Sammel-Gruppe „Sonstige" geführt, damit sie nicht verloren gehen.
"""
from __future__ import annotations

from dataclasses import dataclass, field
# P3 (2026-06-11): `Any` wurde in Signaturen genutzt, war aber nie
# importiert — dank `from __future__ import annotations` kein
# Laufzeitfehler, aber jede Introspektion (get_type_hints) bräche.
from typing import Any, Literal

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import (
    CONF_ENTITY_CHARGE_MODE,
    CONF_ENTITY_CLIMATE,
    CONF_ENTITY_CONTROL,
    CONF_ENTITY_COOL_CONTROL,
    CONF_ENTITY_CURRENT_TEMP,
    CONF_ENTITY_ENERGY_DISCHARGED_TOTAL,
    CONF_ENTITY_ENERGY_TOTAL,
    CONF_ENTITY_POWER,
    CONF_ENTITY_POWER_2,
    CONF_ENTITY_SOC,
    CONF_ENTITY_VEHICLE_STATUS,
    CONF_ENTITY_WATER_HEATER,
    HEURISTIC_ACCEPT,
    HEURISTIC_REJECT,
)

# Aliase für besseres Lesen unten — match den Konvention in
# config_flow.py / coordinator.py.
# CN-13 (2026-06-11): aircon ergänzt — fehlte hier sowie in
# NAME_HINTS und der Sortier-Order.
DeviceType = Literal[
    "solar", "battery", "wallbox", "grid",
    "heating", "warmwater", "aircon", "generic", "haushalt",
]

# Welche Crowdergy-Typen eine HA-Integration üblicherweise bedient.
# Auszug, bewusst additiv — neue Hersteller landen hier ohne Code-
# Änderung am Klassifizierer.
INTEGRATION_HINTS: dict[str, frozenset[str]] = {
    # Solar / Wechselrichter
    "fronius":              frozenset({"solar"}),
    "sma":                  frozenset({"solar", "battery"}),
    "solaredge":            frozenset({"solar", "battery"}),
    "huawei_solar":         frozenset({"solar", "battery"}),
    "enphase_envoy":        frozenset({"solar", "battery"}),
    "goodwe":               frozenset({"solar", "battery"}),
    "kostal":               frozenset({"solar"}),
    "kostal_plenticore":    frozenset({"solar", "battery"}),
    "growatt_server":       frozenset({"solar", "battery"}),
    # Batterie / Heimspeicher
    "sonnen":               frozenset({"battery"}),
    "sonnenbatterie":       frozenset({"battery"}),
    "victron":              frozenset({"battery", "solar"}),
    "byd":                  frozenset({"battery"}),
    "powerwall":            frozenset({"battery"}),
    # Wallbox
    "keba":                 frozenset({"wallbox"}),
    "easee":                frozenset({"wallbox"}),
    "go_e":                 frozenset({"wallbox"}),
    "goecharger":           frozenset({"wallbox"}),
    "wallbox":              frozenset({"wallbox"}),
    "openevse":             frozenset({"wallbox"}),
    "tesla_wall_connector": frozenset({"wallbox"}),
    "evcc":                 frozenset({"wallbox"}),
    # Wärmepumpe / Klima — bedienen meist sowohl heating als auch
    # warmwater. Welcher Slot welche Entity ist, entscheidet die
    # Slot-Heuristik unten (Domain + Name-Tokens).
    "daikin":               frozenset({"heating", "warmwater"}),
    "nibe":                 frozenset({"heating", "warmwater"}),
    "viessmann":            frozenset({"heating", "warmwater"}),
    "stiebel_eltron":       frozenset({"heating", "warmwater"}),
    "mitsubishi_heavy_aircon": frozenset({"heating"}),
    "lg_thinq":             frozenset({"heating"}),
    "vaillant":             frozenset({"heating", "warmwater"}),
    "buderus":              frozenset({"heating", "warmwater"}),
    # Panasonic / Aquarea (HACS-Community, am meisten verbreitet)
    "aquarea_smart_cloud":  frozenset({"heating", "warmwater"}),
    "aquarea_smart_cloud2": frozenset({"heating", "warmwater"}),
    "aquarea":              frozenset({"heating", "warmwater"}),
    "panasonic_smart_app":  frozenset({"heating", "warmwater"}),
    # Netz / Smart Meter
    "dsmr":                 frozenset({"grid"}),
    "p1_monitor":           frozenset({"grid"}),
    "tibber":               frozenset({"grid"}),
    "shelly":               frozenset({"grid", "generic"}),
    "shelly_em":            frozenset({"grid"}),
    "shellyem":             frozenset({"grid"}),
    # Multipurpose / Generic
    "modbus":               frozenset({"generic", "solar", "battery", "grid"}),
    "mqtt":                 frozenset({"generic", "solar", "battery", "grid", "wallbox"}),
}

# Schwache Name-Token-Hints — nur als Tie-Breaker, nie Primärsignal.
# Lowercase Match auf irgendeinem Bestandteil von entity_id oder
# friendly_name nach Tokenisierung an `[._\s\-/]`.
NAME_HINTS: dict[str, frozenset[str]] = {
    "solar":     frozenset({"solar", "pv", "photovoltaic", "inverter", "wechselrichter"}),
    "battery":   frozenset({"battery", "batterie", "speicher", "akku"}),
    "wallbox":   frozenset({"wallbox", "wb", "evse", "charger", "ladestation"}),
    "grid":      frozenset({"grid", "netz", "meter", "zähler", "zaehler", "bezug", "einspeisung"}),
    "heating":   frozenset({"heating", "heat", "heizung", "hp", "wp", "waermepumpe", "warmpump"}),
    "warmwater": frozenset({"warmwater", "ww", "dhw", "warmwasser", "hot_water", "boiler"}),
    "aircon":    frozenset({"aircon", "ac", "klima", "klimaanlage", "klimaanlagen", "airco", "split"}),
    "haushalt":  frozenset({"haushalt", "household", "load", "hausverbrauch"}),
}


@dataclass(frozen=True)
class EntityMeta:
    """Read-only Snapshot einer HA-Entity, plus Geräte-Anker.

    Wird einmal pro Auto-Setup gesammelt; die Heuristik arbeitet nur
    auf diesen Strukturen, nicht direkt gegen `hass.states` — das hält
    die Klassifizierung deterministisch + testbar.
    """
    entity_id: str
    platform: str                # = Integration, z.B. "fronius"
    domain: str                  # sensor, switch, climate, ...
    device_class: str | None
    unit: str | None             # unit_of_measurement
    state_class: str | None      # measurement, total, total_increasing
    friendly_name: str | None
    device_id: str | None        # HA device_registry id (Gruppierungsanker)
    area_id: str | None
    name_tokens: tuple[str, ...] = field(default_factory=tuple)


@dataclass
class MappingCandidate:
    """Heuristischer (Phase 1) oder LLM-getragener (Phase 2) Vorschlag
    für eine einzelne Entity → (DeviceType, Slot, Confidence).

    `slot` ist einer der CONF_ENTITY_*-Keys aus const.py. None bei
    Entities die wir nicht zuordnen können (Heuristik unsicher / kein
    Match) — die UI rendert das als „bitte manuell".
    """
    entity_id: str
    device_type: DeviceType | None
    slot: str | None
    confidence: float
    source: Literal["heuristic", "llm", "manual"] = "heuristic"
    reason: str = ""


@dataclass
class DeviceGroup:
    """Eine HA-Device-Registry-Gruppe zusammengefasst zu einem
    Crowdergy-Device-Vorschlag. Der Confirm-Step rendert pro Gruppe
    eine Card mit den Slot-Vorschlägen.
    """
    suggested_type: DeviceType
    suggested_name: str
    ha_device_id: str | None
    candidates: list[MappingCandidate] = field(default_factory=list)

    @property
    def avg_confidence(self) -> float:
        if not self.candidates:
            return 0.0
        return sum(c.confidence for c in self.candidates) / len(self.candidates)

    def slot_map(self) -> dict[str, str]:
        """Letztes Wort pro Slot — falls mehrere Kandidaten den
        gleichen Slot beanspruchen, gewinnt der mit höchster Confidence.
        Verwendet von _flow_ zur Vor-Auswahl.
        """
        best: dict[str, MappingCandidate] = {}
        for c in self.candidates:
            if c.slot is None or c.entity_id == "":
                continue
            existing = best.get(c.slot)
            if existing is None or c.confidence > existing.confidence:
                best[c.slot] = c
        return {slot: c.entity_id for slot, c in best.items()}


# ─────────────────────────────────────────────────────────────────────
# Sammlung der Metadaten
# ─────────────────────────────────────────────────────────────────────


def collect_entity_metadata(hass: HomeAssistant) -> list[EntityMeta]:
    """Walk durch das Entity-Registry, augmentiert um state-Attribute
    (device_class, unit, state_class). Nur energie-/strom-/SoC-/temp-
    relevante Domains landen in der Liste — alles andere ist eh nicht
    mapping-fähig und nur Rauschen für den Klassifizierer.
    """
    ent_reg = er.async_get(hass)
    relevant_domains = {
        "sensor", "binary_sensor", "switch", "input_boolean",
        "number", "input_number", "select", "input_select",
        "climate", "water_heater", "light", "fan",
    }
    out: list[EntityMeta] = []
    for entry in ent_reg.entities.values():
        if entry.disabled or entry.hidden_by is not None:
            continue
        if entry.domain not in relevant_domains:
            continue
        state = hass.states.get(entry.entity_id)
        attrs = state.attributes if state is not None else {}
        unit = attrs.get("unit_of_measurement") if state else None
        state_class = attrs.get("state_class") if state else None
        device_class = entry.device_class or (
            attrs.get("device_class") if state else None
        )
        friendly = (state.name if state else None) or entry.name or entry.original_name
        tokens = _tokenise(entry.entity_id, friendly)
        out.append(
            EntityMeta(
                entity_id=entry.entity_id,
                platform=entry.platform or "",
                domain=entry.domain,
                device_class=device_class,
                unit=unit,
                state_class=state_class,
                friendly_name=friendly,
                device_id=entry.device_id,
                area_id=entry.area_id,
                name_tokens=tokens,
            )
        )
    return out


def _tokenise(entity_id: str, friendly: str | None) -> tuple[str, ...]:
    """Splittet entity_id + friendly_name in Lowercase-Tokens, ohne
    Domain-Präfix. Wird für die NAME_HINTS-Matches benutzt — bewusst
    rohe Splits, keine NLP.
    """
    import re

    pieces: list[str] = []
    if "." in entity_id:
        _, after = entity_id.split(".", 1)
    else:
        after = entity_id
    pieces.extend(re.split(r"[._\-\s/]+", after.lower()))
    if friendly:
        pieces.extend(re.split(r"[._\-\s/]+", friendly.lower()))
    return tuple(p for p in pieces if p)


# ─────────────────────────────────────────────────────────────────────
# Klassifizierung pro Entity
# ─────────────────────────────────────────────────────────────────────


def classify_entity(meta: EntityMeta) -> list[MappingCandidate]:
    """Erzeuge zero-oder-mehr Kandidaten aus einer Entity. Eine
    Power-Sensor-Entity einer Wallbox-Integration ergibt z.B. einen
    Vorschlag (`wallbox`, `entity_current_power_kw`, 0.92). Energie-
    Counter ergeben einen zweiten Vorschlag (`entity_energy_total`).

    Liefert mehrere Vorschläge wenn die Entity mehrdeutig auf
    verschiedene Slots passt (selten — meistens ein klarer Hit).
    """
    # platform kann theoretisch None sein (Template-Sensoren, dynamic
    # Entities). INTEGRATION_HINTS.get fängt das ab; ohne Match landen
    # wir auf dem Name-Token-only-Pfad weiter unten.
    integration_types = INTEGRATION_HINTS.get(meta.platform or "", frozenset())
    slot_guess = _guess_slot(meta)
    if slot_guess is None:
        return []
    slot, slot_conf = slot_guess

    # Typ-Kandidaten: Integration-Hints zuerst, fällt sonst auf Name-
    # Tokens zurück.
    type_candidates = list(integration_types)
    type_conf = 0.65  # bekannte Integration → solider Default
    if not type_candidates:
        type_candidates = _types_from_name(meta)
        type_conf = 0.35   # nur Name-Match → schwächer
    if not type_candidates:
        return []

    # Falls mehrere Typen plausibel sind (z.B. Daikin → heating/
    # warmwater), nutze Name-Tokens als Tie-Breaker. „dhw"/„ww"/
    # „warmwater"/„hot_water"-Tokens kippen Richtung warmwater.
    if len(type_candidates) > 1:
        type_candidates = _disambiguate_by_name(meta, type_candidates)

    reasons: list[str] = []
    if meta.platform in INTEGRATION_HINTS:
        reasons.append(f"integration={meta.platform}")
    if meta.device_class:
        reasons.append(f"device_class={meta.device_class}")
    if meta.unit:
        reasons.append(f"unit={meta.unit}")

    out: list[MappingCandidate] = []
    for dt in type_candidates:
        combined = round(0.5 * type_conf + 0.5 * slot_conf, 3)
        out.append(
            MappingCandidate(
                entity_id=meta.entity_id,
                device_type=dt,        # type: ignore[arg-type]
                slot=slot,
                confidence=combined,
                source="heuristic",
                reason=", ".join(reasons) or "name-hint only",
            )
        )
    return out


def _guess_slot(meta: EntityMeta) -> tuple[str, float] | None:
    """Welches Crowdergy-Entity-Slot passt zu dieser HA-Entity?
    Returnt (slot, confidence) oder None wenn kein klarer Treffer.
    """
    domain = meta.domain
    dc = (meta.device_class or "").lower()
    unit = (meta.unit or "").lower()
    sc = (meta.state_class or "").lower()

    # Climate / water_heater Domain → moderne Heizungs-Steuerung
    if domain == "climate":
        return (CONF_ENTITY_CLIMATE, 0.95)
    if domain == "water_heater":
        return (CONF_ENTITY_WATER_HEATER, 0.95)

    # Sensor-Pfade
    if domain == "sensor":
        if dc == "power" and unit in {"w", "kw"}:
            return (CONF_ENTITY_POWER, 0.92)
        if dc == "energy" and sc in {"total", "total_increasing"} and unit in {"wh", "kwh", "mwh"}:
            # Discharged-Counter detect anhand Name-Tokens. Beide
            # Verbformen abdecken (German/English) — discharge/
            # discharged/entladen/exportiert.
            if _has_token(meta, {
                "discharge", "discharged", "entladen", "abgegeben",
                "export", "exported", "einspeisung",
            }):
                return (CONF_ENTITY_ENERGY_DISCHARGED_TOTAL, 0.85)
            return (CONF_ENTITY_ENERGY_TOTAL, 0.90)
        if dc == "battery" and unit == "%":
            return (CONF_ENTITY_SOC, 0.95)
        if dc == "temperature" and unit in {"°c", "c"}:
            return (CONF_ENTITY_CURRENT_TEMP, 0.85)
        # Vehicle-Status: state-Strings ohne unit, name-token-Hint
        if dc is None or dc == "":
            if _has_token(meta, {"status", "vehicle", "car", "auto", "fahrzeug", "state"}):
                if _has_token(meta, {"wallbox", "wb", "charger", "evse", "ladestation"}):
                    return (CONF_ENTITY_VEHICLE_STATUS, 0.70)
        return None

    # Switch / input_boolean / light / fan = entity_control
    if domain in {"switch", "input_boolean", "light", "fan"}:
        return (CONF_ENTITY_CONTROL, 0.70)

    # Number / input_number / select / input_select = entity_control
    # oder entity_charge_mode je nach Name. Heuristik: Modi-Namen
    # (`mode`, `lademodus`, `charge`) kippen Richtung charge_mode.
    if domain in {"number", "input_number", "select", "input_select"}:
        if _has_token(meta, {"mode", "lademodus", "modus", "charge_mode", "battery_mode"}):
            return (CONF_ENTITY_CHARGE_MODE, 0.75)
        if _has_token(meta, {"cool", "kuehl", "kühl", "cooling"}):
            return (CONF_ENTITY_COOL_CONTROL, 0.65)
        return (CONF_ENTITY_CONTROL, 0.55)

    return None


def _types_from_name(meta: EntityMeta) -> list[str]:
    """Fallback wenn die Integration nicht in INTEGRATION_HINTS steht
    — versuche Crowdergy-Typ aus Name-Tokens zu raten.
    """
    hits: list[str] = []
    for crowdergy_type, hints in NAME_HINTS.items():
        if hints & set(meta.name_tokens):
            hits.append(crowdergy_type)
    return hits


def _disambiguate_by_name(meta: EntityMeta, types: list[str]) -> list[str]:
    """Wenn die Integration mehrere Typen liefert, dünne via Name-
    Tokens aus. Wir gehen die Typen in absteigender Spezifität durch
    (warmwater vor heating, weil „water_heating" warmwater ist) und
    nehmen den ersten der einen Name-Hint-Match hat. Wenn keiner
    matched, bleiben alle Kandidaten — der Confirm-Step rendert sie
    dann als Dropdown.
    """
    tokens = set(meta.name_tokens)
    # Reihenfolge ist relevant: spezifischere Typen zuerst.
    priority = ["warmwater", "haushalt", "wallbox", "battery", "solar", "grid", "aircon", "heating", "generic"]
    for crowdergy_type in priority:
        if crowdergy_type in types and tokens & NAME_HINTS.get(crowdergy_type, frozenset()):
            return [crowdergy_type]
    return types  # mehrdeutig — UI zeigt's als Dropdown


def _has_token(meta: EntityMeta, candidates: set[str]) -> bool:
    return bool(candidates & set(meta.name_tokens))


# ─────────────────────────────────────────────────────────────────────
# Gruppierung pro HA-DeviceRegistry-Eintrag
# ─────────────────────────────────────────────────────────────────────


def group_candidates_by_device(
    hass: HomeAssistant,
    entities: list[EntityMeta],
) -> list[DeviceGroup]:
    """Reduziert alle Kandidaten auf **maximal eine Gruppe pro Crowdergy-
    Typ** — der „best guess" pro Typ.

    Vorgehen (v3.1.1, nach User-Feedback v3.1.0 ergab 35 Cards bei einem
    realen Setup, was unbenutzbar war):
    1. Alle Kandidaten pro Typ sammeln.
    2. Pro Typ: Kandidaten nach HA-device_id clustern und das Cluster
       mit dem höchsten kombinierten Score (Σ confidence über alle
       distinkten Slots) wählen — der beste „Container" für diesen Typ.
    3. Aus dem Sieger-Cluster pro Slot den höchst-konfidenten
       Kandidaten ziehen.
    4. Eine DeviceGroup pro Typ in der Output-Liste.

    Wer mehrere Geräte desselben Typs hat (zwei Wallboxen, mehrere
    Generics) ergänzt sie nach dem Auto-Setup via OptionsFlow / „Gerät
    hinzufügen". P1-Hypothese: 90 % der Setups haben pro Typ höchstens
    ein Crowdergy-relevantes Gerät.
    """
    dev_reg = dr.async_get(hass)

    # Pro Crowdergy-Typ alle Kandidaten sammeln, gruppiert nach HA-
    # device_id. Cluster-Score = Σ confidence über distinkte Slots
    # (mehr verschiedene Slots = vollständigeres Gerät = besser).
    by_type: dict[str, dict[str | None, dict[str, MappingCandidate]]] = {}
    for meta in entities:
        for cand in classify_entity(meta):
            if cand.confidence < HEURISTIC_REJECT or cand.slot is None:
                continue
            if cand.device_type is None:
                continue
            cluster = by_type.setdefault(cand.device_type, {}).setdefault(
                meta.device_id, {}
            )
            existing = cluster.get(cand.slot)
            if existing is None or cand.confidence > existing.confidence:
                cluster[cand.slot] = cand

    groups: list[DeviceGroup] = []
    for dtype, clusters in by_type.items():
        if not clusters:
            continue
        # Score jeden Cluster, wähle das beste. Primär mehr Slots,
        # dann Σ confidence, dann (deterministischer Tie-Break) die
        # HA-device_id als String — sonst hängt das Resultat bei
        # Tie von Dict-Insertion-Order ab und ist nicht reproduzierbar
        # über Setup-Runs hinweg.
        def _score(
            kv: tuple[str | None, dict[str, MappingCandidate]],
        ) -> tuple[int, float, str]:
            dev_id, slots = kv
            return (
                len(slots),
                sum(c.confidence for c in slots.values()),
                str(dev_id or ""),
            )

        best_dev_id, best_slots = max(clusters.items(), key=_score)

        # Suggested-Name vom HA-DeviceRegistry, Fallback auf Typ-Default.
        device_entry = dev_reg.async_get(best_dev_id) if best_dev_id else None
        if device_entry is not None and device_entry.name_by_user:
            name = device_entry.name_by_user
        elif device_entry is not None and device_entry.name:
            name = device_entry.name
        else:
            name = _default_name_for_type(dtype)

        groups.append(
            DeviceGroup(
                suggested_type=dtype,        # type: ignore[arg-type]
                suggested_name=name,
                ha_device_id=best_dev_id,
                candidates=list(best_slots.values()),
            )
        )

    # Stabile, gut lesbare Reihenfolge: solar zuerst, dann battery,
    # grid, heatpump-family (inkl. aircon), wallbox, generic —
    # entspricht der iOS-Tile-Sortierung.
    order = {
        "solar": 0, "battery": 1, "grid": 2,
        "heating": 3, "warmwater": 4, "aircon": 5,
        "wallbox": 6, "generic": 7, "haushalt": 8,
    }
    groups.sort(key=lambda g: order.get(g.suggested_type, 99))
    return groups


def _default_name_for_type(dtype: str) -> str:
    return {
        "solar": "PV-Anlage",
        "battery": "Hausbatterie",
        "wallbox": "Wallbox",
        "grid": "Netz",
        "heating": "Heizung",
        "warmwater": "Warmwasser",
        "aircon": "Klimaanlage",
        "generic": "Verbraucher",
        "haushalt": "Hausverbrauch",
    }.get(dtype, "Gerät")


# ─────────────────────────────────────────────────────────────────────
# High-Level Entry-Point für den Config-Flow
# ─────────────────────────────────────────────────────────────────────


async def discover_devices(hass: HomeAssistant) -> list[DeviceGroup]:
    """Sammelt Metadaten, klassifiziert, gruppiert. Der Config-Flow
    ruft diese eine Funktion und packt das Resultat in seinen Context.
    """
    metas = collect_entity_metadata(hass)
    return group_candidates_by_device(hass, metas)


# ─────────────────────────────────────────────────────────────────────
# Phase 2: LLM-Fallback via Backend-Endpoint
# ─────────────────────────────────────────────────────────────────────


def _build_llm_payload(
    metas: list[EntityMeta],
    classified_entity_ids: set[str],
    known_types: set[str],
) -> tuple[dict[str, Any], dict[str, str]]:
    """Erzeuge anonymisiertes LLM-Request-Payload + lokales `ref →
    entity_id` Mapping. Wichtigste Datenschutz-Garantie: das Backend
    bekommt KEIN raw entity_id und KEIN friendly_name — nur die
    domain/device_class/unit/state_class/integration + tokenisierte
    Namensbestandteile + eine UUID als ref-Token.
    """
    import uuid

    unmapped_payload: list[dict[str, Any]] = []
    ref_to_entity: dict[str, str] = {}
    for meta in metas:
        # Bereits sicher klassifiziert (Heuristik ≥ ACCEPT) → skippen
        # damit der LLM nur die wirklich unklaren Reste sieht.
        if meta.entity_id in classified_entity_ids:
            continue
        # Entities ohne mapping-fähige Domain müssen wir gar nicht
        # erst dem Modell zumuten.
        if meta.domain not in {
            "sensor", "switch", "input_boolean", "number",
            "input_number", "select", "input_select",
            "climate", "water_heater", "light", "fan",
        }:
            continue
        ref = uuid.uuid4().hex[:12]
        ref_to_entity[ref] = meta.entity_id
        unmapped_payload.append(
            {
                "ref": ref,
                "domain": meta.domain,
                "device_class": meta.device_class,
                "unit": meta.unit,
                "state_class": meta.state_class,
                "integration": meta.platform,
                "name_tokens": list(meta.name_tokens),
                "device_area": None,  # Area-Versand ist Opt-in (P3)
            }
        )

    payload = {
        "schema_version": 1,
        "unmapped_entities": unmapped_payload,
        "known_devices": sorted(known_types),
    }
    return payload, ref_to_entity


def merge_llm_suggestions(
    groups: list[DeviceGroup],
    suggestions: list[dict[str, Any]],
    ref_to_entity: dict[str, str],
    hass: HomeAssistant,
) -> list[DeviceGroup]:
    """LLM-Vorschläge in die bestehenden Heuristik-Gruppen einbauen:
    pro LLM-Suggestion suche eine bestehende Gruppe vom selben Typ
    und fülle den fehlenden Slot dort. Wenn noch keine Gruppe vom Typ
    existiert, lege eine neue an.

    Slot-Konflikt mit Heuristik wird IMMER zugunsten der Heuristik
    aufgelöst (Heuristik > LLM bei gleichem Slot) — wir vertrauen
    deterministischen Regeln mehr als Modell-Output, vor allem bei
    bekannten Integrationen.
    """
    dev_reg = dr.async_get(hass)
    by_type: dict[str, DeviceGroup] = {g.suggested_type: g for g in groups}
    occupied_slots: dict[str, set[str]] = {
        dtype: {c.slot for c in g.candidates if c.slot}
        for dtype, g in by_type.items()
    }

    for sug in suggestions:
        ref = sug.get("ref")
        if not isinstance(ref, str):
            continue
        entity_id = ref_to_entity.get(ref)
        if not entity_id:
            continue
        device_type = sug.get("device_type")
        slot = sug.get("attribute")
        if not device_type or not slot:
            continue
        try:
            confidence = float(sug.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence <= 0.0:
            continue

        # Slot-Konflikt mit Heuristik → Heuristik gewinnt.
        if slot in occupied_slots.get(device_type, set()):
            continue

        group = by_type.get(device_type)
        if group is None:
            # Neue Typ-Gruppe vom LLM erfunden — wir nehmen den HA-
            # Device-Namen wenn vorhanden, sonst Typ-Default.
            ent_reg = er.async_get(hass)
            entry = ent_reg.async_get(entity_id) if ent_reg else None
            ha_device_id = entry.device_id if entry else None
            device_entry = dev_reg.async_get(ha_device_id) if ha_device_id else None
            name = (
                (device_entry.name_by_user or device_entry.name)
                if device_entry and (device_entry.name_by_user or device_entry.name)
                else _default_name_for_type(device_type)
            )
            group = DeviceGroup(
                suggested_type=device_type,        # type: ignore[arg-type]
                suggested_name=name,
                ha_device_id=ha_device_id,
                candidates=[],
            )
            by_type[device_type] = group
            occupied_slots.setdefault(device_type, set())

        group.candidates.append(
            MappingCandidate(
                entity_id=entity_id,
                device_type=device_type,        # type: ignore[arg-type]
                slot=slot,
                confidence=confidence,
                source="llm",
                reason=str(sug.get("rationale", ""))[:200],
            )
        )
        occupied_slots[device_type].add(slot)

    # Stabile Sortier-Order beibehalten (siehe group_candidates_by_device).
    order = {
        "solar": 0, "battery": 1, "grid": 2,
        "heating": 3, "warmwater": 4, "aircon": 5,
        "wallbox": 6, "generic": 7, "haushalt": 8,
    }
    out = list(by_type.values())
    out.sort(key=lambda g: order.get(g.suggested_type, 99))
    return out


async def discover_devices_with_llm(
    hass: HomeAssistant,
    api_url: str,
    access_token: str,
    user_agent: str,
) -> list[DeviceGroup]:
    """Heuristik + (optional) Backend-LLM-Fallback in einem Schritt.
    Wird vom Config-Flow aufgerufen wenn `MAPPING_LLM_ENABLED=True`.
    Bei jedem Backend-/LLM-Fehler fallen wir auf die reine Heuristik
    zurück — Auto-Setup darf an einem Modell-Hiccup nie scheitern.
    """
    import httpx

    from .const import LLM_MIN_CONFIDENCE

    metas = collect_entity_metadata(hass)
    heuristic_groups = group_candidates_by_device(hass, metas)

    classified = {
        c.entity_id
        for g in heuristic_groups
        for c in g.candidates
        if c.confidence >= HEURISTIC_ACCEPT
    }
    known_types = {g.suggested_type for g in heuristic_groups}

    payload, ref_to_entity = _build_llm_payload(
        metas, classified, known_types
    )
    if not payload["unmapped_entities"]:
        return heuristic_groups

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{api_url}/api/v1/mapping/suggest",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "User-Agent": user_agent,
                    "content-type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.TimeoutException:
        import logging
        logging.getLogger(__name__).warning(
            "Auto-Setup LLM-Call: Backend antwortet nicht in 10 s, "
            "fallback auf reine Heuristik."
        )
        return heuristic_groups
    except httpx.HTTPStatusError as err:
        import logging
        logging.getLogger(__name__).warning(
            "Auto-Setup LLM-Call: Backend HTTP %s — fallback auf "
            "reine Heuristik.", err.response.status_code,
        )
        return heuristic_groups
    except (httpx.HTTPError, ValueError) as err:
        import logging
        logging.getLogger(__name__).warning(
            "Auto-Setup LLM-Call gescheitert (%s) — fallback auf "
            "reine Heuristik.", err,
        )
        return heuristic_groups

    suggestions = data.get("suggestions", [])
    # Defensive: filter sub-min-confidence vorab.
    suggestions = [
        s for s in suggestions
        if float(s.get("confidence", 0.0) or 0.0) >= LLM_MIN_CONFIDENCE
    ]
    return merge_llm_suggestions(
        heuristic_groups, suggestions, ref_to_entity, hass
    )


__all__ = [
    "DeviceGroup",
    "EntityMeta",
    "HEURISTIC_ACCEPT",
    "HEURISTIC_REJECT",
    "MappingCandidate",
    "classify_entity",
    "collect_entity_metadata",
    "discover_devices",
    "group_candidates_by_device",
    "INTEGRATION_DISPLAY_NAMES",
    "integration_display_name",
    "required_integration_domains",
]


# Klarnamen für die verbreitetsten HA-Integrationen, damit die
# Connector-Anzeige „dafür brauchst du folgende Integration(en)" beim
# Profil-Pick nicht den rohen Domain-Slug zeigt. Unbekannte Domains
# fallen über `integration_display_name` auf einen aufgehübschten Slug
# zurück (Underscores → Leerzeichen, Title-Case). Bewusst additiv —
# neue Integrationen hier ergänzen, ohne dass etwas bricht.
INTEGRATION_DISPLAY_NAMES: dict[str, str] = {
    # Solar / Wechselrichter
    "fronius": "Fronius",
    "sma": "SMA",
    "solaredge": "SolarEdge",
    "huawei_solar": "Huawei Solar",
    "enphase_envoy": "Enphase Envoy",
    "goodwe": "GoodWe",
    "kostal": "Kostal",
    "kostal_plenticore": "Kostal Plenticore",
    "growatt_server": "Growatt",
    # Batterie / Heimspeicher
    "sonnen": "sonnen",
    "sonnenbatterie": "sonnenBatterie",
    "victron": "Victron",
    "byd": "BYD",
    "powerwall": "Tesla Powerwall",
    # Wallbox
    "keba": "KEBA",
    "easee": "Easee",
    "go_e": "go-e",
    "goecharger": "go-e Charger",
    "wallbox": "Wallbox",
    "openevse": "OpenEVSE",
    "tesla_wall_connector": "Tesla Wall Connector",
    "evcc": "evcc",
    # Wärmepumpe / Klima
    "daikin": "Daikin",
    "nibe": "NIBE",
    "viessmann": "Viessmann",
    "stiebel_eltron": "Stiebel Eltron",
    "mitsubishi_heavy_aircon": "Mitsubishi Heavy",
    "lg_thinq": "LG ThinQ",
    "vaillant": "Vaillant",
    "buderus": "Buderus",
    "aquarea_smart_cloud": "Panasonic Aquarea",
    "aquarea_smart_cloud2": "Panasonic Aquarea",
    "aquarea": "Panasonic Aquarea",
    "panasonic_smart_app": "Panasonic Comfort Cloud",
    # Netz / Smart Meter
    "dsmr": "DSMR Smart Meter",
    "p1_monitor": "P1 Monitor",
    "tibber": "Tibber",
    "shelly": "Shelly",
    "shelly_em": "Shelly EM",
    "shellyem": "Shelly EM",
    # Multipurpose
    "modbus": "Modbus",
    "mqtt": "MQTT",
}


def integration_display_name(domain: str) -> str:
    """Klarname für eine HA-Integration-Domain, Slug-Fallback.

    Bekannte Domains kommen aus `INTEGRATION_DISPLAY_NAMES`; unbekannte
    werden aufgehübscht (Underscores → Leerzeichen, Title-Case), sodass
    z.B. eine noch nicht kuratierte `my_inverter`-Domain als „My Inverter"
    erscheint statt roh."""
    slug = (domain or "").strip()
    if not slug:
        return ""
    if slug in INTEGRATION_DISPLAY_NAMES:
        return INTEGRATION_DISPLAY_NAMES[slug]
    return slug.replace("_", " ").title()


def required_integration_domains(
    hass: HomeAssistant, entity_ids: list[str]
) -> list[str]:
    """Alle DISTINKTEN HA-Integration-Domains der Entities (Registry →
    Config-Entry), sortiert.

    Superset zu `dominant_integration_domain` (das nur die häufigste
    Domain zurückgibt): ein Preset, dessen entity_map Entities aus
    mehreren Integrationen zieht, braucht sie ALLE, um vollständig
    auflösbar zu sein. Template-/Helper-Entities ohne Config-Entry zählen
    nicht. Leere Liste, wenn keine Entity eine Integration auflöst."""
    ent_reg = er.async_get(hass)
    domains: set[str] = set()
    for entity_id in entity_ids:
        reg_entry = ent_reg.async_get(entity_id)
        if reg_entry is None or not reg_entry.config_entry_id:
            continue
        config_entry = hass.config_entries.async_get_entry(
            reg_entry.config_entry_id
        )
        if config_entry is not None:
            domains.add(config_entry.domain)
    return sorted(domains)


def dominant_integration_domain(
    hass: HomeAssistant, entity_ids: list[str]
) -> str | None:
    """Häufigste Integration-Domain der Entities (Registry → Config-Entry).

    Box-Mapping-Umbau (2026-06-10): beim Crowd-Contribute wird
    mitgespeichert, WELCHE Integration die gemappten Entities liefert —
    nur damit kann die Crowdergy Box ein approved Preset später headless
    nachbauen (Integration provisionieren + Mapping anwenden). Template-/
    Helper-Entities haben keinen Config-Entry und zählen nicht; bei
    Mischungen gewinnt die häufigste Domain.
    """
    from collections import Counter

    ent_reg = er.async_get(hass)
    domains: list[str] = []
    for entity_id in entity_ids:
        reg_entry = ent_reg.async_get(entity_id)
        if reg_entry is None or not reg_entry.config_entry_id:
            continue
        config_entry = hass.config_entries.async_get_entry(
            reg_entry.config_entry_id
        )
        if config_entry is not None:
            domains.append(config_entry.domain)
    if not domains:
        return None
    return Counter(domains).most_common(1)[0][0]
