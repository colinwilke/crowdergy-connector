"""Crowd-preset resolution helpers for the config flow.

Extracted from config_flow.py (#50 god-file split); imported back by
the flow classes in config_flow.py. Pure helpers — no dependency on the
ConfigFlow/OptionsFlow classes.
"""
from __future__ import annotations

from typing import Any


from .const import (
    CONF_ENTITY_BATTERY_MODE,
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
    return {
        **(getattr(flow, "_pending_preset_entity_map", None) or {}),
        **(getattr(flow, "_pending_preset_value_map", None) or {}),
    }


def _preset_suggests_battery_control(flow: Any) -> bool:
    """True wenn das gewählte Preset die Battery-Dispatch-Slots trägt.
    Der Battery-Werte-Step wurde bisher nur über ein gesetztes
    `entity_charge_mode` erreicht — ein Preset mit Mode-Select +
    Setpoint (Pflicht-Slots im Mapping-Dictionary) soll den Step auch
    ohne Lademodus-Select öffnen, damit die Steuerung nicht stumm
    unkonfiguriert bleibt."""
    return bool(_preset_step_defaults(flow).get(CONF_ENTITY_BATTERY_MODE))
