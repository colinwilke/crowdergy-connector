"""Statische JSON-Assets müssen valides JSON sein.

Regression-Guard: eine kaputte `translations/de.json` (unescaptes Quote)
ließ HA beim Setup mit `orjson.JSONDecodeError` abbrechen — die Integration
kam gar nicht erst hoch. Hassfest deckt das nicht zuverlässig ab, also
laden wir hier jede ausgelieferte JSON-Datei und parsen sie hart.
"""
from __future__ import annotations

import json
import re
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


TRANSLATIONS_DIR = INTEGRATION_DIR / "translations"


def _flatten(obj: object, prefix: str = "") -> dict[str, object]:
    out: dict[str, object] = {}
    if isinstance(obj, dict):
        for key, val in obj.items():
            out.update(_flatten(val, f"{prefix}.{key}" if prefix else key))
    else:
        out[prefix] = obj
    return out


def test_english_translation_shipped() -> None:
    """`translations/en.json` MUSS existieren.

    HA liest Config-Flow-Übersetzungen zur Laufzeit NUR aus
    `translations/<lang>.json`; `strings.json` ist reine Entwickler-Quelle
    und wird bei einer Custom-Integration NICHT gelesen. Ohne `en.json`
    sieht jeder User mit englischer (= Default-)HA-Sprache ausschließlich
    leere Menü-/Feld-Einträge, weil es keinen `en`-Fallback gibt.
    """
    assert (TRANSLATIONS_DIR / "en.json").is_file(), (
        "translations/en.json fehlt — englische HA-Instanzen rendern den "
        "Config-Flow komplett leer (strings.json wird zur Laufzeit nicht "
        "gelesen)."
    )


@pytest.mark.parametrize("lang", ["en", "de"])
def test_translation_keys_cover_strings(lang: str) -> None:
    """Jede ausgelieferte Sprache MUSS die `strings.json`-Keys voll decken.

    Ein Menü-/Step-Zusatz in `strings.json` ohne passenden Eintrag in
    `en.json`/`de.json` rendert in der betroffenen Sprache leer.
    """
    source = _flatten(json.loads((INTEGRATION_DIR / "strings.json").read_text("utf-8")))
    target = _flatten(json.loads((TRANSLATIONS_DIR / f"{lang}.json").read_text("utf-8")))
    missing = sorted(set(source) - set(target))
    assert not missing, f"{lang}.json fehlen {len(missing)} Keys aus strings.json: {missing[:15]}"


_ANGLE_PLACEHOLDER = re.compile(r"<[A-Za-z][A-Za-z0-9_-]*>")

_UI_STRING_FILES = [INTEGRATION_DIR / "strings.json"] + sorted(
    TRANSLATIONS_DIR.glob("*.json")
)


@pytest.mark.parametrize(
    "path", _UI_STRING_FILES, ids=lambda p: str(p.relative_to(INTEGRATION_DIR))
)
def test_no_angle_bracket_placeholders(path: Path) -> None:
    """Kein UI-Text darf einen `<platzhalter>` enthalten.

    HA rendert Step-Beschreibungen und Feld-Hilfetexte als Markdown, also
    als HTML: der Browser liest `sensor.<wp>_target_temperature_water` als
    unbekanntes Tag `<wp>` und der Sanitizer wirft es weg — übrig bleibt
    `sensor._target_temperature_water`, ein Beispiel, das niemandem hilft.
    Das ist in v3.48.0 genau so ausgeliefert worden (#152).

    Platzhalter deshalb ausschreiben (`sensor.warmepumpe_…`) und dazusagen,
    dass vorn der eigene Gerätename steht — nie in spitzen Klammern.
    """
    offenders = [
        f"{key}: {value}"
        for key, value in _flatten(json.loads(path.read_text("utf-8"))).items()
        if isinstance(value, str) and _ANGLE_PLACEHOLDER.search(value)
    ]
    assert not offenders, (
        f"{path.relative_to(INTEGRATION_DIR)}: spitze Klammern werden beim "
        f"Markdown-Rendern verschluckt — {offenders}"
    )
