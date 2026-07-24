"""Region (Bundesland) dropdown — 2026-07-24.

The location + edit-base-settings steps offer the 16 official German
states as a fixed SelectSelector (so the Crowdwerk map groups "NRW" and
"Nordrhein-Westfalen" into one region). Stadt/Stadtteil stay free text.
"""
from __future__ import annotations

from homeassistant.helpers import selector

from custom_components.theothergas import config_flow
from custom_components.theothergas.const import CONF_REGION, GERMAN_STATES


def test_sixteen_states_unique():
    assert len(GERMAN_STATES) == 16
    assert len(set(GERMAN_STATES)) == 16
    assert "Nordrhein-Westfalen" in GERMAN_STATES
    assert "Bayern" in GERMAN_STATES
    assert "Thüringen" in GERMAN_STATES


def test_region_field_is_select_of_all_states():
    key, sel = config_flow._region_selector_field("")
    assert isinstance(sel, selector.SelectSelector)
    assert sel.config["options"] == list(GERMAN_STATES)
    assert sel.config["mode"] == selector.SelectSelectorMode.DROPDOWN
    assert key.schema == CONF_REGION  # the voluptuous marker key


def test_region_field_prefills_only_valid_state():
    # A canonical state (as Nominatim returns with accept-language=de) is
    # pre-selected via suggested_value.
    key, _ = config_flow._region_selector_field("Nordrhein-Westfalen")
    assert key.description == {"suggested_value": "Nordrhein-Westfalen"}

    key, _ = config_flow._region_selector_field("  Bayern ")  # trimmed
    assert key.description == {"suggested_value": "Bayern"}


def test_region_field_no_prefill_for_unknown_or_empty():
    # A legacy free-text value ("NRW") or empty is NOT pre-selected — the
    # user picks a canonical option; nothing invalid is forced into the
    # select.
    for raw in ("", "   ", "NRW", "Bavaria", "Tirol"):
        key, _ = config_flow._region_selector_field(raw)
        assert key.description is None
