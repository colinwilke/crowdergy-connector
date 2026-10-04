"""Crowd-preset resolution helpers for the config flow.

Extracted from config_flow.py (#50 god-file split); imported back by
the flow classes in config_flow.py. Pure helpers — no dependency on the
ConfigFlow/OptionsFlow classes.
"""
from __future__ import annotations

from typing import Any


from .const import (
    CONF_ENTITY_BATTERY_MODE,
    CONF_ENTITY_CLIMATE,
    CONF_DEVICE_CONFIG_MODE,
    CONF_ENTITY_CONTROL,
    CONF_ENTITY_WATER_HEATER,
    CONFIG_MODE_CLIMATE,
    CONFIG_MODE_MANUAL,
)
from .preset_spec import PRESET_SLOT_SPEC

# Typen mit KonfigMode-Step (Manuell vs. Climate-/Water-Heater-Entity).
# Bei einer Profil-Wahl leitet sich der Modus aus der Steuer-Entity des
# Profils ab, der Step entfällt (#69-Folge: der Picker lag vorher HINTER
# dem Modus-Step und wurde für diese Typen nie erreicht).
CONFIG_MODE_TYPES = frozenset({"heating", "warmwater", "aircon"})

# Flag-Slots wandern als String "true" im value_map (preset_spec-Doku);
# die Schemas erwarten bool — `bool("false")` wäre True.
_FLAG_SLOTS = frozenset(
    slot.key
    for slots in PRESET_SLOT_SPEC.values()
    for slot in slots
    if slot.kind == "flag"
)


# NB (#97, 2026-07-02): das frühere `_resolve_integration_domain`
# (first-resolvable-Entity) ist entfernt — der Contribute-Flow nutzt
# ausschließlich `entity_mapper.dominant_integration_domain` (häufigste
# Domain; identisches None-Verhalten, da beide dieselbe Registry
# traversieren). Zwei Berechnungen desselben Werts, von denen die
# zweite die erste sofort überschrieb, waren ein Drift-/
# Fehlklassifikations-Risiko.


def _picked_preset_maps(
    presets: list[dict[str, Any]], choice: str
) -> tuple[dict[str, str], dict[str, str], dict[str, dict]] | None:
    """Auflösung der Picker-Wahl `<vendor>::<model>` → (entity_map,
    value_map, entity_identity_map) des Presets, defensiv gefiltert.
    None wenn die Wahl nicht (mehr) im Lookup-Cache liegt. value_map +
    entity_identity_map sind jüngere Vertragsfelder — ältere Backends
    liefern sie nicht, dann bleiben die Maps leer (Werte-Steps zeigen
    keine Vorschläge; die Entity-Auflösung fällt auf den
    Suffix-Match zurück)."""
    for p in presets:
        if f"{p['vendor']}::{p['model']}" != choice:
            continue

        def _str_map(raw: Any) -> dict[str, str]:
            if not isinstance(raw, dict):
                return {}
            return {
                k: v for k, v in raw.items()
                if isinstance(k, str) and isinstance(v, str)
            }

        def _identity_map(raw: Any) -> dict[str, dict]:
            if not isinstance(raw, dict):
                return {}
            return {
                k: v for k, v in raw.items()
                if isinstance(k, str) and isinstance(v, dict)
            }

        return (
            _str_map(p.get("entity_map")),
            _str_map(p.get("value_map")),
            _identity_map(p.get("entity_identity_map")),
        )
    return None


def _preset_step_defaults(flow: Any) -> dict[str, Any]:
    """Gemergte Preset-Defaults (entity_map + value_map) für die
    Werte-Steps nach dem Entity-Step. Beide Flow-Klassen (Initial +
    Options-Add) tragen die gleichen `_pending_preset_*`-Attribute.
    Leeres Dict = kein Preset gewählt → Steps rendern wie bisher."""
    merged: dict[str, Any] = {
        **(getattr(flow, "_pending_preset_entity_map", None) or {}),
        **(getattr(flow, "_pending_preset_value_map", None) or {}),
    }
    for key in _FLAG_SLOTS & merged.keys():
        raw = merged[key]
        merged[key] = (
            raw.strip().lower() == "true" if isinstance(raw, str) else bool(raw)
        )
    return merged


def _preset_entities_defaults(flow: Any) -> dict[str, Any] | None:
    """Defaults für den Entity-Step nach einer Profil-Wahl: aufgelöste
    Entities + Flags (Vorzeichen) aus dem value_map, dazu der KonfigMode
    explizit — sonst kippt `_entities_schema`s Legacy-Migration ein
    Profil mit climate-/water_heater-Steuerung in den Climate-Modus,
    auch wenn dessen Primärfeld die Domain nicht nimmt. None = kein
    Profil gewählt."""
    if getattr(flow, "_pending_preset_entity_map", None) is None:
        return None
    return {
        **_preset_step_defaults(flow),
        CONF_DEVICE_CONFIG_MODE: getattr(
            flow, "_pending_config_mode", None
        ) or CONFIG_MODE_MANUAL,
    }


def _preset_config_mode(device_type: str, raw_entity_map: dict[str, str]) -> str:
    """KonfigMode aus der Steuer-Entity des Profils (ROH-Map, nicht die
    aufgelöste — ein unauflösbarer Steuer-Slot ändert nichts am
    Steuer-Muster des Geräts). Climate nur, wenn die Domain zum
    Primärfeld des Climate-Modus passt (warmwater → water_heater,
    heating/aircon → climate); sonst Manuell, dort nimmt `entity_control`
    jede steuerbare Domain."""
    control = raw_entity_map.get(CONF_ENTITY_CONTROL, "")
    domain = control.split(".", 1)[0] if "." in control else ""
    primary = "water_heater" if device_type == "warmwater" else "climate"
    return CONFIG_MODE_CLIMATE if domain == primary else CONFIG_MODE_MANUAL


def _apply_preset_config_mode(flow: Any, raw_entity_map: dict[str, str]) -> None:
    """Setzt `_pending_config_mode` aus dem gewählten Profil und spiegelt
    im Climate-Modus die aufgelöste Steuer-Entity auf das Primärfeld
    (`entity_climate`/`entity_water_heater`) — der Climate-Entity-Step
    rendert `entity_control` nicht. `_apply_climate_first` kopiert sie
    beim Submit zurück."""
    device_type = getattr(flow, "_pending_type", None) or ""
    mode = _preset_config_mode(device_type, raw_entity_map)
    flow._pending_config_mode = mode
    resolved = flow._pending_preset_entity_map
    if mode == CONFIG_MODE_CLIMATE and resolved and resolved.get(CONF_ENTITY_CONTROL):
        key = (
            CONF_ENTITY_WATER_HEATER if device_type == "warmwater"
            else CONF_ENTITY_CLIMATE
        )
        resolved.setdefault(key, resolved[CONF_ENTITY_CONTROL])


def _preset_suggests_battery_control(flow: Any) -> bool:
    """True wenn das gewählte Preset die Battery-Dispatch-Slots trägt.
    Der Battery-Werte-Step wurde bisher nur über ein gesetztes
    `entity_charge_mode` erreicht — ein Preset mit Mode-Select +
    Setpoint (Pflicht-Slots im Mapping-Dictionary) soll den Step auch
    ohne Lademodus-Select öffnen, damit die Steuerung nicht stumm
    unkonfiguriert bleibt. (#300) Gefragt wird das ROH-Preset: der
    Prefill lässt einen unauflösbaren Steuer-Slot leer, der Vorschlag
    bleibt trotzdem bestehen."""
    if CONF_ENTITY_BATTERY_MODE in (
        getattr(flow, "_pending_preset_slots", None) or ()
    ):
        return True
    return bool(_preset_step_defaults(flow).get(CONF_ENTITY_BATTERY_MODE))
