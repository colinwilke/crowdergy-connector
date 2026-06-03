"""Single source of truth — Connector ↔ Backend Device-Feld-Roundtrip.

Beide Pfade (`_register_device` POST + `_update_device_backend` PUT)
bauen ihre Payload aus dieser Spec. **Damit ist Drift zwischen
Create + Update technisch unmöglich.**

Hintergrund 2026-06-03: Tester-Report von zillmann (Heizung + WW
gleichzeitig gestartet trotz korrekt gesetztem
`shares_hardware_with_device_id`). Root cause war ein Drift-Bug —
das Feld wurde im Create-Flow gesetzt aber im Edit-Flow nicht
re-übermittelt. Audit förderte zusätzliche Drift-Stellen zutage
(aircon `included_in_haushalt` fehlte im Edit, …). Statt jede
Stelle einzeln zu fixen, vereinheitlichen wir die Payload-Konstruktion.

## Adding a new backend-mapped field

1. Backend: addiere die Spalte + Pydantic-Schema-Feld.
2. Connector: addiere eine `DeviceField`-Zeile in `SPEC` unten.
   Fertig — beide Pfade picken's automatisch auf.

## Mode-Enum

Pro Feld zwei Regeln (`on_create` + `on_update`):

- `always`       — Wert immer schreiben (auch wenn leer/None →
                   clearen funktioniert)
- `skip_empty`   — nur schreiben wenn truthy (kein clearen möglich,
                   gut bei optional-on-create Feldern)
- `skip`         — nie schreiben (z.B. Location nur einmal beim
                   Create setzen, nie via Edit ändern)
- `if_present`   — nur schreiben wenn der Source-Key im
                   entity_input vorhanden ist (defensiv für
                   Edit-Flows die einen Step ggf. überspringen —
                   kein accidentally-clear)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .const import (
    CONF_CHARGE_MODE_VALUE_LOCK,
    CONF_CHARGE_MODE_VALUE_POWER,
    CONF_CHARGE_MODE_VALUE_SOLAR,
    CONF_CITY,
    CONF_DISTRICT,
    CONF_ENTITY_COOL_CONTROL,
    CONF_INCLUDED_IN_HAUSHALT,
    CONF_REGION,
    CONF_SHARES_HARDWARE_WITH,
    CONF_SUPPORTS_COOLING,
    CONF_VALUE_COOL_OFF,
    CONF_VALUE_COOL_ON,
    CONF_VALUE_OFF,
)


# ── DeviceField ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DeviceField:
    """Eine Backend-Spalte + ihre Connector-Source.

    Wert kommt entweder direkt aus `entity_input[conf_key]` ODER aus
    einer Compute-Funktion (für abgeleitete Werte wie
    `supports_cooling`, `value_cool_off`).
    """
    api_name: str
    """Backend-API-Feldname (matcht die Pydantic-Spalte)."""
    types: frozenset[str] | None = None
    """Auf welche Device-Types anwendbar. None = alle Typen."""
    on_create: str = "skip_empty"
    on_update: str = "if_present"
    conf_key: str | None = None
    """Direkter Lookup in entity_input."""
    compute: Callable[[dict[str, Any], str], Any] | None = None
    """Alternative zu conf_key: berechnet den Wert aus
    (entity_input, device_type). Nur eines von conf_key/compute pflegen."""
    transform: Callable[[Any], Any] | None = None
    """Optionaler Post-Process-Step auf den extrahierten Wert."""


# ── Compute-Helpers ──────────────────────────────────────────────────────────


def _compute_supports_cooling(entity_input: dict[str, Any], dtype: str) -> bool:
    """Aircon ist immer cooling-capable. Heating-Devices nur wenn
    der User `value_cool_on` gesetzt hat (oder den Legacy-Bool
    explizit angekreuzt hat)."""
    if dtype == "aircon":
        return True
    cool_on = entity_input.get(CONF_VALUE_COOL_ON, "") or ""
    return bool(cool_on or entity_input.get(CONF_SUPPORTS_COOLING, False))


def _compute_value_cool_off(entity_input: dict[str, Any], dtype: str) -> str:
    """Falls der User `value_cool_on` aber kein `value_cool_off`
    gepflegt hat: fall back auf den allgemeinen `value_off`
    (climate.set_hvac_mode('off') ist für Heat + Cool identisch)."""
    cool_on = entity_input.get(CONF_VALUE_COOL_ON, "") or ""
    cool_off = entity_input.get(CONF_VALUE_COOL_OFF, "") or ""
    if cool_on and not cool_off:
        cool_off = entity_input.get(CONF_VALUE_OFF, "") or ""
    return cool_off


# ── Type-Mengen ──────────────────────────────────────────────────────────────


_CONSUMER_HAUSHALT_TYPES = frozenset({
    "heating", "warmwater", "aircon", "wallbox", "generic",
})
_HEATING_FAMILY_COOLING = frozenset({"heating", "aircon"})
_WARMWATER_ONLY = frozenset({"warmwater"})
_WALLBOX_ONLY = frozenset({"wallbox"})


# ── SPEC ─────────────────────────────────────────────────────────────────────


SPEC: tuple[DeviceField, ...] = (
    # Standort-Strings — vom Create-Schritt aus dem Nominatim-Lookup
    # oder den Stadt-/Land-Defaults gefüllt. Edit-Flow toucht's nicht:
    # ein Umzug ist ein eigener Reset, nicht ein „Device-Edit".
    DeviceField(
        api_name="district", conf_key=CONF_DISTRICT,
        on_create="always", on_update="skip",
    ),
    DeviceField(
        api_name="city", conf_key=CONF_CITY,
        on_create="always", on_update="skip",
    ),
    DeviceField(
        api_name="region", conf_key=CONF_REGION,
        on_create="always", on_update="skip",
    ),

    # Haushalt-Flag — alle Verbraucher-Typen, beide Richtungen.
    # **Bugfix 2026-06-03**: aircon war im alten Update-Code-Set
    # nicht enthalten; ein Aircon-Edit ließ das Flag silent auf False
    # fallen. Mit Spec-driven Payload geht das nicht mehr.
    DeviceField(
        api_name="included_in_haushalt",
        conf_key=CONF_INCLUDED_IN_HAUSHALT,
        types=_CONSUMER_HAUSHALT_TYPES,
        on_create="always", on_update="always",
        transform=bool,
    ),

    # Warmwater shares-hardware — Pointer aufs Heizungs-Device das
    # denselben Kompressor teilt (Solver-Mutex: nie gleichzeitig).
    # **Bugfix 2026-06-03 (zillmann)**: Edit-Flow hat das vorher nie
    # ans Backend gesendet → stale state. Update auf `if_present`
    # damit ein Edit-Pass der den Step überspringt nichts clearen,
    # ein Edit der den Step durchläuft aber leer-wählt das Feld
    # korrekt auf None setzen kann.
    DeviceField(
        api_name="shares_hardware_with_device_id",
        conf_key=CONF_SHARES_HARDWARE_WITH,
        types=_WARMWATER_ONLY,
        on_create="skip_empty",
        on_update="if_present",
        transform=lambda v: v or None,
    ),

    # Wallbox-Lademodus-Mappings (drei Strings, eines pro Solver-Mode).
    # Both directions `always` — empty string = User hat den Mode-
    # Wert gecleared, Backend speichert das als „kein Override".
    DeviceField(
        api_name="charge_mode_value_lock",
        conf_key=CONF_CHARGE_MODE_VALUE_LOCK,
        types=_WALLBOX_ONLY,
        on_create="always", on_update="always",
    ),
    DeviceField(
        api_name="charge_mode_value_power",
        conf_key=CONF_CHARGE_MODE_VALUE_POWER,
        types=_WALLBOX_ONLY,
        on_create="always", on_update="always",
    ),
    DeviceField(
        api_name="charge_mode_value_solar",
        conf_key=CONF_CHARGE_MODE_VALUE_SOLAR,
        types=_WALLBOX_ONLY,
        on_create="always", on_update="always",
    ),

    # Cooling-Capability + cooling-side Entity/Values (Heating + Aircon).
    # supports_cooling ist abgeleitet (compute), die übrigen drei
    # direkt aus entity_input.
    DeviceField(
        api_name="supports_cooling",
        types=_HEATING_FAMILY_COOLING,
        on_create="always", on_update="always",
        compute=_compute_supports_cooling,
    ),
    DeviceField(
        api_name="entity_cool_control",
        conf_key=CONF_ENTITY_COOL_CONTROL,
        types=_HEATING_FAMILY_COOLING,
        on_create="always", on_update="always",
    ),
    DeviceField(
        api_name="value_cool_on",
        conf_key=CONF_VALUE_COOL_ON,
        types=_HEATING_FAMILY_COOLING,
        on_create="always", on_update="always",
    ),
    DeviceField(
        api_name="value_cool_off",
        types=_HEATING_FAMILY_COOLING,
        on_create="always", on_update="always",
        compute=_compute_value_cool_off,
    ),
)


# ── Payload-Builder ──────────────────────────────────────────────────────────


def build_payload(
    mode: str,                     # "create" | "update"
    dtype: str,
    name: str,
    entity_input: dict[str, Any],
    *,
    extra: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Baut die JSON-Payload für POST /devices (mode='create') oder
    PUT /devices/{id} (mode='update') aus der SPEC.

    `extra` mergt zusätzliche key→string-Defaults in entity_input vor
    der Spec-Evaluation — wird z.B. genutzt um die Location-Strings
    (district/city/region) im Create-Flow durchzureichen ohne sie
    im entity_input-Dict mitführen zu müssen.
    """
    if mode not in ("create", "update"):
        raise ValueError(f"unknown mode {mode!r}")

    # name + type sind immer da — egal welche Spec-Felder folgen.
    payload: dict[str, Any] = {"name": name, "type": dtype}

    # Spec konsumiert beides aus entity_input
    ctx: dict[str, Any] = dict(entity_input)
    if extra:
        for k, v in extra.items():
            ctx.setdefault(k, v)

    for field in SPEC:
        if field.types is not None and dtype not in field.types:
            continue
        rule = field.on_create if mode == "create" else field.on_update
        if rule == "skip":
            continue

        # Wert-Quelle: compute() oder direct lookup
        if field.compute is not None:
            value = field.compute(ctx, dtype)
        elif field.conf_key is not None:
            if rule == "if_present" and field.conf_key not in ctx:
                continue
            value = ctx.get(field.conf_key)
        else:
            # broken spec entry (weder compute noch conf_key)
            continue

        if field.transform is not None:
            value = field.transform(value)

        if rule == "skip_empty" and not value:
            continue

        payload[field.api_name] = value

    return payload
