"""Statische JSON-Assets müssen valides JSON sein.

Regression-Guard: eine kaputte `translations/de.json` (unescaptes Quote)
ließ HA beim Setup mit `orjson.JSONDecodeError` abbrechen — die Integration
kam gar nicht erst hoch. Hassfest deckt das nicht zuverlässig ab, also
laden wir hier jede ausgelieferte JSON-Datei und parsen sie hart.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

INTEGRATION_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "theothergas"

JSON_FILES = sorted(INTEGRATION_DIR.rglob("*.json"))


@pytest.mark.parametrize("path", JSON_FILES, ids=lambda p: str(p.relative_to(INTEGRATION_DIR)))
def test_shipped_json_is_valid(path: Path) -> None:
    """Jede ausgelieferte JSON-Datei muss parsebar sein."""
    text = path.read_text(encoding="utf-8")
    try:
        json.loads(text)
    except json.JSONDecodeError as err:  # pragma: no cover - Fehlermeldung
        pytest.fail(f"{path.relative_to(INTEGRATION_DIR)} ist kein valides JSON: {err}")


def test_at_least_translations_present() -> None:
    """Sanity: wir prüfen tatsächlich Dateien (kein leerer Glob)."""
    names = {p.name for p in JSON_FILES}
    assert "de.json" in names
    assert "manifest.json" in names
