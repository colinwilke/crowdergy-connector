"""Shared pytest-Fixtures für die Connector-Test-Suite (Cluster E
Connector 2026-06-09).

Verwendet `pytest-homeassistant-custom-component` damit wir ohne
echte HA-Instance gegen einen sauberen `hass`-State testen können.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Lädt das Connector-Custom-Component automatisch in jedem
    Test (pytest-homeassistant-custom-component-Pflicht-Hook).
    """
    yield
