"""CN-13 (2026-06-11): aircon in allen Typ-Registries + SSOT für die
controllable Typen (const.CONTROLLABLE_TYPES statt dreier Kopien)."""
from __future__ import annotations

from custom_components.theothergas import config_flow, switch
from custom_components.theothergas.const import CONTROLLABLE_TYPES, DEVICE_TYPES
from custom_components.theothergas.device_registry import DEVICE_TYPE_MODELS
from custom_components.theothergas.entity_mapper import NAME_HINTS


def test_controllable_types_is_single_source_of_truth():
    assert switch._CROWDERGIZE_TYPES is CONTROLLABLE_TYPES
    assert config_flow._CONTROLLABLE_TYPES is CONTROLLABLE_TYPES


def test_aircon_present_everywhere():
    assert "aircon" in DEVICE_TYPES
    assert "aircon" in CONTROLLABLE_TYPES
    # Crowdergize-Switch für Klimaanlagen (fehlte in switch.py).
    assert "aircon" in switch._CROWDERGIZE_TYPES
    # HA-Geräteliste: kein "Generic Energy Device" mehr für ACs.
    assert "aircon" in DEVICE_TYPE_MODELS
    # Auto-Mapping-Heuristik kennt den Typ.
    assert "aircon" in NAME_HINTS


def test_controllable_types_excludes_read_only():
    assert {"solar", "grid", "haushalt"} & CONTROLLABLE_TYPES == set()
