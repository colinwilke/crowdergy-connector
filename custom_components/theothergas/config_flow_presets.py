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


def _resolve_integration_domain(
    hass: Any, entity_map: dict[str, str]
) -> str | None:
    """Aus den gemappten Entities die HA-Integration ableiten.

    Geht über die Entity-Registry → ConfigEntry-Lookup; entity_id-
    Präfixe sind unzuverlässig (user-renamable). Liefert den
    ConfigEntry.domain des ersten auflösbaren Eintrags (alle Entities
    eines Geräte-Setups gehören normalerweise demselben ConfigEntry).
    Returns None bei Template-Sensoren, nicht-registrierten Entities
    oder fehlendem ConfigEntry — Backend akzeptiert NULL, der box-
    manager filtert dann allerdings das Preset raus.
    """
    try:
        from homeassistant.helpers import entity_registry as er
    except Exception:  # pragma: no cover - HA-only path
        return None
    registry = er.async_get(hass)
    for entity_id in entity_map.values():
        entry = registry.async_get(entity_id)
        if entry is None or entry.config_entry_id is None:
            continue
        config_entry = hass.config_entries.async_get_entry(
            entry.config_entry_id
        )
        if config_entry is None:
            continue
        return config_entry.domain
    return None


def _picked_preset_maps(
    presets: list[dict[str, Any]], choice: str
) -> tuple[dict[str, str], dict[str, str]] | None:
    """Auflösung der Picker-Wahl `<vendor>::<model>` → (entity_map,
    value_map) des Presets, beide str→str-gefiltert. None wenn die
    Wahl nicht (mehr) im Lookup-Cache liegt. value_map ist neu im
    Mapping-Store-Vertrag — ältere Backends liefern das Feld nicht,
    dann bleibt die Map leer (Werte-Steps zeigen keine Vorschläge)."""
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

        return _str_map(p.get("entity_map")), _str_map(p.get("value_map"))
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
