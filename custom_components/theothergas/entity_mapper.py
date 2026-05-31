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
- Werte-Strings (value_on, charge_mode_value_*, battery_value_*) sind
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
from typing import Literal

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
DeviceType = Literal[
    "solar", "battery", "wallbox", "grid",
    "heating", "warmwater", "generic", "haushalt",
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
    integration_types = INTEGRATION_HINTS.get(meta.platform, frozenset())
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
    priority = ["warmwater", "haushalt", "wallbox", "battery", "solar", "grid", "heating", "generic"]
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
        # Score jeden Cluster, wähle das beste. Tie-Break: mehr Slots
        # gewinnt vor mehr Confidence-Summe (vollständiger ist besser
        # als „nur ein Power-Sensor mit 95 %").
        def _score(slots: dict[str, MappingCandidate]) -> tuple[int, float]:
            return (len(slots), sum(c.confidence for c in slots.values()))

        best_dev_id, best_slots = max(clusters.items(), key=lambda kv: _score(kv[1]))

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
    # grid, heatpump-family, wallbox, generic — entspricht der iOS-
    # Tile-Sortierung.
    order = {
        "solar": 0, "battery": 1, "grid": 2,
        "heating": 3, "warmwater": 4, "wallbox": 5,
        "generic": 6, "haushalt": 7,
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
]
