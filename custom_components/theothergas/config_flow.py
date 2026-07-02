"""Config flow for Crowdergy Connector integration."""
from __future__ import annotations

import logging
from typing import Any

import httpx
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_API_URL,
    CONF_CITY,
    CONF_DEVICE_ID,
    CONF_DEVICE_CONFIG_MODE,
    CONF_DEVICE_NAME,
    CONF_DEVICE_TYPE,
    CONF_DEVICES,
    CONFIG_MODE_CLIMATE,
    CONFIG_MODE_MANUAL,
    CONF_DISTRICT,
    CONF_ENTITY_OUTDOOR_TEMP,
    CONF_EMAIL,
    CONF_ENTITY_CHARGE_MODE,
    CONF_ENTITY_CLIMATE,
    CONF_ENTITY_WATER_HEATER,
    CONF_ENTITY_CONTROL,
    CONF_ENTITY_POWER,
    CONF_ENTITY_POWER_2,
    CONF_ENTITY_SOC,
    CONF_ENTITY_VEHICLE_STATUS,
    CONF_ENTITY_CURRENT_TEMP,
    CONF_ENTITY_ENERGY_TOTAL,
    CONF_ENTITY_ENERGY_DISCHARGED_TOTAL,
    CONF_PAIRING_CODE,
    CONF_REFRESH_TOKEN,
    CONF_REGION,
    CONF_USER_ID,
    CONF_ENTITY_CONTROL_HOLD,
    CONF_VALUE_OFF,
    CONF_VALUE_ON,
    CONF_VEHICLE_STATUS_VALUE_PLUGGED,
    CONF_VEHICLE_STATUS_VALUE_UNPLUGGED,
    CONF_VEHICLE_STATUS_VALUE_ERROR,
    CONF_CHARGE_MODE_VALUE_LOCK,
    CONF_CHARGE_MODE_VALUE_POWER,
    CONF_CHARGE_MODE_VALUE_SOLAR,
    CONF_ENTITY_BATTERY_MODE,
    CONF_VALUE_BATTERY_MODE_ACTIVE,
    CONF_VALUE_BATTERY_MODE_PASSIVE,
    CONF_ENTITY_BATTERY_POWER_SETPOINT,
    CONF_BATTERY_SETPOINT_INVERT_SIGN,
    CONF_SHARES_HARDWARE_WITH,
    CONF_ENTITY_COOL_CONTROL,
    CONF_VALUE_COOL_ON,
    CONF_VALUE_COOL_OFF,
    CONF_SETUP_MODE,
    SETUP_MODE_AUTO,
    SETUP_MODE_MANUAL,
    HEURISTIC_ACCEPT,
    MAPPING_LLM_ENABLED,
    ENTITY_CONTROL_HOLD_AUTO,
    CONTROLLABLE_TYPES,
    DEFAULT_API_URL,
    DEVICE_TYPES,
    DOMAIN,
    USER_AGENT,
)
from .device_field_spec import build_payload
from .entity_mapper import DeviceGroup, discover_devices, discover_devices_with_llm
from .preset_spec import (
    PRESET_CAPABLE_TYPES,
    extract_preset_maps,
    missing_required_labels,
)

_LOGGER = logging.getLogger(__name__)


# ── Helpers extracted to sibling modules (#50 god-file split) ──────────────
from .config_flow_schemas import (
    DEVICE_TYPE_LABELS_DE,
    _battery_values_schema,
    _charge_mode_values_schema,
    _config_mode_schema,
    _contribute_form_schema,
    _entities_schema,
    _shares_hardware_schema,
    _type_name_schema,
    _values_schema,
    _vehicle_status_schema,
    _vendor_preset_pick_schema,
)
# Re-exported (defined in config_flow_schemas) so `config_flow._ENTITY_SELECTORS`
# stays the documented SSOT access point (#46) and the selector-contract tests
# keep importing it from here.
from .config_flow_schemas import (  # noqa: F401
    _ENTITY_SELECTORS,
    _READ_FIELDS,
)
from .config_flow_presets import (
    _picked_preset_maps,
    _preset_step_defaults,
    _preset_suggests_battery_control,
)
from .config_flow_mapping import (
    _apply_climate_first,
    _auto_fill_binary_vehicle_status,
    _build_device_record,
    _flatten_sections,
    _is_binary_entity,
    _remove_ha_device,
)

# Device types that the Crowdergy app can switch on/off through the
# user-mapped entity_control. CN-13 (2026-06-11): SSOT liegt jetzt in
# const.py (CONTROLLABLE_TYPES); Alias bleibt für die ~10 Call-Sites.
_CONTROLLABLE_TYPES = CONTROLLABLE_TYPES


async def _fetch_vendor_presets(
    hass,
    device_type: str,
    *,
    entry=None,
    api_url: str = "",
    token: str = "",
) -> list[dict[str, Any]]:
    """GET /api/v1/crowd-presets/lookup für einen Device-Type.

    Returns the list of presets (sorted desc by contribution_count
    backend-seitig). Bei Netzwerk-/Backend-Fehler: leere Liste, der
    aufrufende Step skipt den Picker und geht direkt zu device_entities.

    CN-12 (2026-06-11): läuft über `_authenticated_config_request`
    (401-Refresh + Client-Bau im Executor statt im Event-Loop). Im
    Options-Flow `entry` übergeben; im Initial-Flow (vor Entry-
    Existenz) explizite `api_url`/`token`-Credentials.
    """
    try:
        response = await _authenticated_config_request(
            hass, entry, "GET", "/api/v1/crowd-presets/lookup",
            api_url=api_url, access_token=token,
            params={"device_type": device_type},
        )
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as err:
        _LOGGER.debug("vendor-preset lookup failed: %s", err)
        return []
    presets = (data.get("presets") or []) if isinstance(data, dict) else []
    if not isinstance(presets, list):
        return []
    # CN-14 (2026-06-11): Pflichtkeys defensiv prüfen — ein Preset
    # ohne vendor/model crasht sonst den Picker
    # (`_vendor_preset_pick_schema` indiziert beide hart).
    out: list[dict[str, Any]] = []
    for p in presets:
        if not isinstance(p, dict):
            continue
        if not {"vendor", "model"} <= p.keys():
            _LOGGER.debug(
                "vendor-preset without vendor/model dropped (keys: %s)",
                sorted(p.keys()),
            )
            continue
        out.append(p)
    return out


# ── Persistence helpers ─────────────────────────────────────────────────────


async def _fetch_account_email(api_url: str, access_token: str) -> str | None:
    """Best-effort `GET /users/me` → email, nur für den Entry-Titel.

    Der Pairing-Claim liefert keine Email; schlägt der Lookup fehl
    (offline / alter Token), fällt der Titel auf die User-ID zurück.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{api_url}/api/v1/users/me",
                headers={"Authorization": f"Bearer {access_token}"},
            )
        if resp.status_code == 200:
            return resp.json().get("email") or None
    except (httpx.RequestError, ValueError):
        pass
    return None


async def _register_device(
    hass,
    entry,
    device_type: str,
    device_name: str,
    entity_input: dict[str, Any],
    location: dict[str, str],
    *,
    api_url: str = "",
    token: str = "",
) -> dict[str, Any]:
    """Register a device on the backend and return the full device dict.

    Payload-Konstruktion läuft seit 2026-06-03 über das zentrale
    `device_field_spec.SPEC` — siehe `device_field_spec.py`. Beide
    Pfade (POST hier, PUT in `_update_device_backend`) konsumieren
    dieselbe Spec, damit Field-Drift ausgeschlossen ist.

    CN-12 (2026-06-11): POST läuft über `_authenticated_config_request`
    (401-Refresh + Client-Bau im Executor). `entry=None` + explizite
    Credentials nur für den Initial-Flow vor Entry-Existenz.
    """
    device_config = build_payload(
        mode="create",
        dtype=device_type,
        name=device_name,
        entity_input=entity_input,
        extra={
            CONF_DISTRICT: location.get(CONF_DISTRICT, ""),
            CONF_CITY: location.get(CONF_CITY, ""),
            CONF_REGION: location.get(CONF_REGION, ""),
        },
    )
    response = await _authenticated_config_request(
        hass, entry, "POST", "/api/v1/devices",
        api_url=api_url, access_token=token,
        json=device_config,
    )
    response.raise_for_status()
    result = response.json()

    return _build_device_record(result["id"], device_type, device_name, entity_input)


async def _update_device_backend(
    hass,
    entry,
    device_id: str,
    device_type: str,
    device_name: str,
    entity_input: dict[str, Any] | None = None,
) -> None:
    """PUT eines Devices ans Backend. Payload kommt seit 2026-06-03
    aus dem zentralen `device_field_spec.SPEC` — vorher hatte die
    Update-Funktion ihre eigene Conditional-Liste, die mit der
    Create-Liste auseinandergedriftet ist (siehe Bug-Audit-Bericht
    2026-06-03: zillmann's shares_hardware ging im Edit-Flow nie
    ans Backend; aircon `included_in_haushalt` fiel beim Edit
    silent auf False).

    CN-12 (2026-06-11): PUT über `_authenticated_config_request`.
    """
    payload = build_payload(
        mode="update",
        dtype=device_type,
        name=device_name,
        entity_input=entity_input or {},
    )
    response = await _authenticated_config_request(
        hass, entry, "PUT", f"/api/v1/devices/{device_id}",
        json=payload,
    )
    response.raise_for_status()


async def _resolve_location_defaults(hass) -> dict[str, str]:
    """Best-effort defaults for the district/city/region form fields.

    Strategy:
      1. Read `hass.config.latitude` / `longitude`. If both are usable,
         reverse-geocode via Nominatim and map the returned address to
         our Stadtteil/Stadt/Region buckets.
      2. On any failure (network, rate-limit, missing coords) fall back
         to `hass.config.location_name` as the city default. District
         and region stay empty.
    """
    fallback_city = (getattr(hass.config, "location_name", "") or "").strip()
    fallback = {CONF_DISTRICT: "", CONF_CITY: fallback_city, CONF_REGION: ""}

    lat = getattr(hass.config, "latitude", 0.0) or 0.0
    lon = getattr(hass.config, "longitude", 0.0) or 0.0
    if abs(lat) < 0.01 and abs(lon) < 0.01:
        return fallback

    url = "https://nominatim.openstreetmap.org/reverse"
    params = {
        "format": "json",
        "lat": f"{lat}",
        "lon": f"{lon}",
        "addressdetails": "1",
        "accept-language": "de",
    }
    headers = {
        # Nominatim's usage policy requires a descriptive UA on every
        # request. USER_AGENT is built from manifest.json so the version
        # tag and the User-Agent never drift apart.
        "User-Agent": USER_AGENT,
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as err:
        _LOGGER.debug("Reverse-geocode failed (%s); using location_name fallback", err)
        return fallback

    address = data.get("address", {}) if isinstance(data, dict) else {}

    # Take the first non-empty value across the candidate keys for each bucket.
    def _pick(keys: list[str]) -> str:
        for key in keys:
            value = address.get(key, "")
            if value:
                return str(value)
        return ""

    district = _pick(["suburb", "city_district", "borough", "quarter", "neighbourhood", "residential"])
    city = _pick(["city", "town", "village", "municipality"])
    region = _pick(["state", "region"])

    return {
        CONF_DISTRICT: district,
        CONF_CITY: city or fallback_city,
        CONF_REGION: region,
    }


async def _refresh_token(
    api_url: str, refresh_token: str
) -> tuple[str, str] | None:
    """One-shot token refresh for config-flow HTTP calls. The
    coordinator has its own refresh path on `_authenticated_request`;
    config-flow paths (register / update / delete) are too rare to
    justify the same machinery, so this small helper covers them.
    Returns (access_token, refresh_token) on success, None otherwise."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{api_url}/api/v1/auth/refresh",
                json={"refresh_token": refresh_token},
            )
        if response.status_code == 200:
            tokens = response.json()
            return tokens["access_token"], tokens["refresh_token"]
    except (httpx.HTTPStatusError, httpx.RequestError) as err:
        _LOGGER.error("Token refresh failed in config flow: %s", err)
    return None


async def _authenticated_config_request(
    hass,
    entry,
    method: str,
    path: str,
    *,
    api_url: str | None = None,
    access_token: str | None = None,
    **kwargs,
) -> httpx.Response:
    """Run an authenticated HTTP call against the backend from the
    config / options flow. Retries once with a fresh access token
    when the first attempt comes back 401 — same behaviour as the
    coordinator's `_authenticated_request`, just without the
    persistent `httpx.AsyncClient` (config-flow calls are rare and
    short-lived). Persists rotated tokens back into the config
    entry so the next call starts from the new pair.

    CN-12 (2026-06-11): `entry=None` + explizite `api_url`/
    `access_token` für Flows VOR Entry-Existenz (Initial-Flow).
    Dort kein 401-Retry — der Token ist Sekunden alt und ein
    Refresh würde das Token-Paar rotieren, ohne dass es irgendwo
    persistiert werden könnte (Backend invalidiert per Use).
    """
    if entry is None:
        if not api_url or not access_token:
            raise ValueError(
                "authenticated config request without entry needs "
                "explicit api_url + access_token"
            )
        access = access_token
        refresh = None
    else:
        api_url = entry.data[CONF_API_URL]
        access = entry.data[CONF_ACCESS_TOKEN]
        refresh = entry.data[CONF_REFRESH_TOKEN]

    async def _do(token: str) -> httpx.Response:
        # Client-Konstruktion ins Executor: httpx lädt beim Erzeugen
        # synchron die CA-Zertifikate (load_verify_locations) — im
        # Event-Loop wirft HA dafür eine Blocking-Call-Warnung (live
        # gesehen im Box-Smoke-Test 2026-06-10, analog Coordinator-Fix
        # v3.5.1).
        client = await hass.async_add_executor_job(
            lambda: httpx.AsyncClient(timeout=15.0)
        )
        try:
            return await client.request(
                method,
                f"{api_url}{path}",
                headers={"Authorization": f"Bearer {token}"},
                **kwargs,
            )
        finally:
            await client.aclose()

    response = await _do(access)
    if response.status_code == 401 and refresh is not None:
        rotated = await _refresh_token(api_url, refresh)
        if rotated is not None:
            new_access, new_refresh = rotated
            new_data = {
                **entry.data,
                CONF_ACCESS_TOKEN: new_access,
                CONF_REFRESH_TOKEN: new_refresh,
            }
            hass.config_entries.async_update_entry(entry, data=new_data)
            response = await _do(new_access)
    return response


# ── Initial Config Flow ─────────────────────────────────────────────────────


class CrowdergyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Crowdergy Connector."""

    VERSION = 2

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._devices: list[dict[str, Any]] = []
        # Carry the device-type/name picked in step 1 into step 2.
        self._pending_type: str | None = None
        self._pending_name: str | None = None
        # v3.0 KonfigMode-Wahl aus Step 1b — entscheidet ob entity_*
        # einzeln (manual) oder via climate.* gebündelt (climate) sind.
        self._pending_config_mode: str = CONFIG_MODE_MANUAL
        # Entity-mapping kept between step 2 and step 3 (values).
        self._pending_entity_input: dict[str, Any] | None = None
        # FEAT-1 v0.1 (2026-06-09): wenn der User im
        # `vendor_preset_pick`-Step ein Crowd-Beitrag-Preset
        # übernimmt, landen die Entity-IDs hier und füllen den
        # `device_entities`-Step als Suggested-Defaults vor. None =
        # User skipped Preset oder es gab keine.
        self._pending_preset_entity_map: dict[str, str] | None = None
        # Mapping-Store (2026-06-11): value_map des gewählten Presets —
        # integrationsspezifische Werte (Select-Optionen, Flags) als
        # Defaults für die nachgelagerten Werte-Steps.
        self._pending_preset_value_map: dict[str, str] | None = None
        # Backend-Response-Cache zwischen Picker-Render und Submit,
        # damit der Submit nicht erneut zum Backend gehen muss.
        self._pending_lookup_cache: list[dict[str, Any]] = []
        # v3.1 Auto-Setup state. Erkannte Geräte-Gruppen aus dem
        # entity_mapper-Scan + die FIFO der vom User confirm'd-Devices
        # die noch durch den klassischen Value-Step-Pfad müssen.
        self._auto_groups: list[DeviceGroup] = []
        self._auto_queue: list[dict[str, Any]] = []

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> CrowdergyOptionsFlow:
        return CrowdergyOptionsFlow(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            api_url = DEFAULT_API_URL
            code = user_input[CONF_PAIRING_CODE].strip()
            tokens: dict[str, Any] | None = None

            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    response = await client.post(
                        f"{api_url}/api/v1/connector/claim",
                        json={"code": code},
                    )
                    if response.status_code == 404:
                        # Generisches 404 vom Backend: unbekannt /
                        # abgelaufen / bereits verbraucht.
                        errors[CONF_PAIRING_CODE] = "invalid_pairing_code"
                    elif response.status_code == 429:
                        errors["base"] = "rate_limited"
                    elif response.status_code >= 400:
                        errors["base"] = "cannot_connect"
                    else:
                        tokens = response.json()
            except httpx.RequestError:
                errors["base"] = "cannot_connect"

            if not errors and tokens is not None:
                user_id = str(tokens.get("user_id", "") or "")
                # P3 (2026-06-11): unique_id = user_id → derselbe Account
                # kann kein Duplikat-Entry werden (Token-Tausch = Reauth).
                if user_id:
                    await self.async_set_unique_id(user_id)
                    self._abort_if_unique_id_configured()
                # Der Claim-Response trägt keine Email — Best-Effort-Lookup
                # nur für einen schönen Entry-Titel (Fallback: User-ID).
                email = await _fetch_account_email(
                    api_url, tokens["access_token"]
                )
                self._data[CONF_API_URL] = api_url
                self._data[CONF_EMAIL] = email or ""
                self._data[CONF_ACCESS_TOKEN] = tokens["access_token"]
                self._data[CONF_REFRESH_TOKEN] = tokens["refresh_token"]
                self._data[CONF_USER_ID] = user_id
                # FEAT-1 v0.3: Entry direkt mit leerer Device-Liste; Ort +
                # Außentemp + Geräte legt der User im Options-Flow an.
                self._data[CONF_DEVICES] = []
                self._data[CONF_DISTRICT] = ""
                self._data[CONF_CITY] = ""
                self._data[CONF_REGION] = ""
                self._data[CONF_ENTITY_OUTDOOR_TEMP] = ""
                if email:
                    title = f"Crowdergy ({email})"
                elif user_id:
                    title = f"Crowdergy ({user_id[:8]})"
                else:
                    title = "Crowdergy"
                return self.async_create_entry(title=title, data=self._data)

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PAIRING_CODE): str,
                }
            ),
            errors=errors,
        )

    async def async_step_import(
        self, import_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Headless provisioning durch die Crowdergy Box (Phase 2).

        Der box-manager der Box hat per Pairing-Code bereits ein
        JWT-Paar geclaimt (`POST /api/v1/box/claim`) und ruft den
        Service `theothergas.provision_box` — der landet hier. Kein
        Login, keine Forms; Entry-Shape wie nach dem interaktiven
        Login (leere Geräteliste, Rest kommt aus Options-Flow bzw.
        box-manager-Provisionierung).

        unique_id = Backend-User-ID: ein Re-Pairing derselben Box/
        desselben Accounts ersetzt die Tokens im bestehenden Entry
        statt einen Duplikat-Entry anzulegen.
        """
        from .provisioning import (
            entry_title,
            extract_consent_options,
            validate_provision_data,
        )

        try:
            data = validate_provision_data(import_data)
        except ValueError:
            return self.async_abort(reason="invalid_provision_data")

        await self.async_set_unique_id(data[CONF_USER_ID])
        # CN-2 (2026-06-11): Re-Pairing-Pfad (Entry existiert schon).
        # Vorher liefen nur die Token-/URL-Updates über
        # `_abort_if_unique_id_configured(updates=...)` — die frisch
        # erfassten Consent-Flags aus dem Box-Wizard gingen verloren
        # (Invariante 5: Consent VOR Pairing, atomar). Jetzt werden
        # data UND options gemeinsam aktualisiert und der Entry neu
        # geladen, damit das Consent-Gate sofort gilt.
        for entry in self._async_current_entries(include_ignore=False):
            if entry.unique_id != self.unique_id:
                continue
            self.hass.config_entries.async_update_entry(
                entry,
                data={
                    **entry.data,
                    CONF_ACCESS_TOKEN: data[CONF_ACCESS_TOKEN],
                    CONF_REFRESH_TOKEN: data[CONF_REFRESH_TOKEN],
                    CONF_API_URL: data[CONF_API_URL],
                },
                options={
                    **entry.options,
                    **extract_consent_options(import_data),
                },
            )
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
            return self.async_abort(reason="already_configured")
        # Consent-Options atomar mit dem Entry anlegen — kein Fenster,
        # in dem der Coordinator mit Default-True pushen könnte.
        return self.async_create_entry(
            title=entry_title(data),
            data=data,
            options=extract_consent_options(import_data),
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """CN-11 (2026-06-11): Reauth-Einstieg. Getriggert vom SSE-
        Client via `entry.async_start_reauth(hass)` wenn das Token-
        Paar nach SSE_AUTH_FAILURE_LIMIT 401-Zyklen endgültig tot ist."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Reauth per Pairing-Code (#39, 2026-06-16): der User erzeugt in
        der App einen frischen Code (funktioniert für Sign-in-with-Apple +
        widerrufene Sessions). Ein Code für ein ANDERES Konto wird
        abgelehnt (`reauth_account_mismatch`) ohne fremde Tokens zu
        persistieren — Reauth tauscht nur das Token-Paar, nie das Konto
        des Entries."""
        errors: dict[str, str] = {}
        entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        if entry is None:
            return self.async_abort(reason="reauth_entry_missing")

        if user_input is not None:
            api_url = entry.data.get(CONF_API_URL, DEFAULT_API_URL)
            code = user_input[CONF_PAIRING_CODE].strip()
            try:
                # Client-Bau im Executor (synchroner CA-Load), analog
                # `_authenticated_config_request`.
                client = await self.hass.async_add_executor_job(
                    lambda: httpx.AsyncClient(timeout=10.0)
                )
                try:
                    response = await client.post(
                        f"{api_url}/api/v1/connector/claim",
                        json={"code": code},
                    )
                finally:
                    await client.aclose()
                if response.status_code == 404:
                    errors[CONF_PAIRING_CODE] = "invalid_pairing_code"
                elif response.status_code == 429:
                    errors["base"] = "rate_limited"
                elif response.status_code >= 400:
                    errors["base"] = "cannot_connect"
                else:
                    tokens = response.json()
                    stored_user_id = entry.data.get(CONF_USER_ID, "")
                    new_user_id = str(tokens.get("user_id", "") or "")
                    if (
                        stored_user_id
                        and new_user_id
                        and new_user_id != stored_user_id
                    ):
                        # Fremd-Konto-Tokens NICHT persistieren — der User
                        # soll einen Code fürs ursprüngliche Konto erzeugen.
                        errors[CONF_PAIRING_CODE] = "reauth_account_mismatch"
                    else:
                        new_data = {
                            **entry.data,
                            CONF_ACCESS_TOKEN: tokens["access_token"],
                            CONF_REFRESH_TOKEN: tokens["refresh_token"],
                        }
                        if new_user_id:
                            new_data[CONF_USER_ID] = new_user_id
                        return self.async_update_reload_and_abort(
                            entry, data=new_data,
                        )
            except (httpx.RequestError, ValueError):
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {vol.Required(CONF_PAIRING_CODE): str}
            ),
            description_placeholders={
                "email": entry.data.get(CONF_EMAIL) or "—"
            },
            errors=errors,
        )

    async def async_step_location(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data[CONF_DISTRICT] = user_input.get(CONF_DISTRICT, "")
            self._data[CONF_CITY] = user_input.get(CONF_CITY, "")
            self._data[CONF_REGION] = user_input.get(CONF_REGION, "")
            self._data[CONF_ENTITY_OUTDOOR_TEMP] = user_input.get(
                CONF_ENTITY_OUTDOOR_TEMP, ""
            )
            return await self.async_step_setup_mode()

        # Pre-fill from HA's configured coordinates so the user usually
        # just hits Submit. Resolved once per fresh location step; if the
        # user goes back and edits we keep whatever they typed.
        defaults = await _resolve_location_defaults(self.hass)
        return self.async_show_form(
            step_id="location",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_DISTRICT, default=defaults[CONF_DISTRICT]): str,
                    vol.Optional(CONF_CITY, default=defaults[CONF_CITY]): str,
                    vol.Optional(CONF_REGION, default=defaults[CONF_REGION]): str,
                    vol.Optional(CONF_ENTITY_OUTDOOR_TEMP): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="sensor")
                    ),
                }
            ),
        )

    # ── v3.1 Auto-Setup-Pfad ───────────────────────────────────────

    async def async_step_setup_mode(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Erster Schritt nach Login + Standort: Manuell vs Auto.

        Per ConfigEntry fix gewählt — wer den Modus später wechseln
        will, entfernt die Integration und legt sie neu an. Default
        ist Auto, weil das die deutlich angenehmere UX ist; Manuell
        bleibt als Escape-Hatch für komplexe Setups (Modbus-Custom-
        Sensoren etc.) wo die Heuristik nichts erkennt.
        """
        if user_input is not None:
            mode = user_input.get(CONF_SETUP_MODE, SETUP_MODE_AUTO)
            self._data[CONF_SETUP_MODE] = mode
            if mode == SETUP_MODE_AUTO:
                return await self.async_step_auto_discover()
            return await self.async_step_device_type()

        return self.async_show_form(
            step_id="setup_mode",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SETUP_MODE, default=SETUP_MODE_AUTO): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                {"value": SETUP_MODE_AUTO, "label": "Auto-Setup — Geräte automatisch erkennen"},
                                {"value": SETUP_MODE_MANUAL, "label": "Manuell — Geräte selbst anlegen"},
                            ],
                            mode=selector.SelectSelectorMode.LIST,
                            translation_key=CONF_SETUP_MODE,
                        )
                    ),
                }
            ),
        )

    async def async_step_auto_discover(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Scant die HA-Instanz, klassifiziert + gruppiert Entities zu
        Crowdergy-Geräte-Vorschlägen. Findet die Heuristik nichts,
        fallen wir auf den manuellen Pfad zurück — sonst weiter zur
        Confirm-Page.
        """
        try:
            if MAPPING_LLM_ENABLED:
                groups = await discover_devices_with_llm(
                    self.hass,
                    self._data[CONF_API_URL],
                    self._data[CONF_ACCESS_TOKEN],
                    USER_AGENT,
                )
            else:
                groups = await discover_devices(self.hass)
        except Exception:   # noqa: BLE001 — Heuristik darf den Flow nie blockieren
            _LOGGER.exception("Auto-Discover failed, falling back to manual flow")
            groups = []

        # Sicherheits-Filter: nur Gruppen mit min. einem Slot anzeigen,
        # alles andere ist nur Rauschen.
        groups = [g for g in groups if g.slot_map()]
        self._auto_groups = groups

        if not groups:
            return await self.async_step_device_type()
        return await self.async_step_auto_confirm()

    # Slot-Set das die Auto-Confirm-Form rendert + parsed. Eine Quelle
    # — sonst stiftet ein Schema-Loop ohne match im Default-Loop einen
    # „extra keys not allowed"-Voluptuous-Fehler. Power-2 + Discharged
    # sind drin damit Hersteller-spezifische Zweit-Sensoren (Sonnen-
    # Charge/Discharge, Grid-Einspeisung) im UI sichtbar sind.
    _AUTO_SLOTS = (
        CONF_ENTITY_POWER,
        CONF_ENTITY_POWER_2,
        CONF_ENTITY_ENERGY_TOTAL,
        CONF_ENTITY_ENERGY_DISCHARGED_TOTAL,
        CONF_ENTITY_SOC,
        CONF_ENTITY_CURRENT_TEMP,
        CONF_ENTITY_VEHICLE_STATUS,
        CONF_ENTITY_CONTROL,
        CONF_ENTITY_CHARGE_MODE,
        CONF_ENTITY_CLIMATE,
        CONF_ENTITY_WATER_HEATER,
    )

    async def async_step_auto_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Listet alle erkannten Gruppen mit pre-filled Entity-Slots.
        User editiert / bestätigt; auf Submit wandern alle aktivierten
        Gruppen in `_auto_queue`. Anschließend läuft pro Gruppe der
        klassische `_dispatch_post_entities`-Pfad — so kommen die Mode-
        Values-Steps (charge_mode, battery, vehicle_status, value_on)
        gewohnt durch, nur die Entity-Auswahl ist eben vorbelegt.
        """
        # Stable Section-Keys: nach v3.1.1 max. 1 Gruppe pro Crowdergy-
        # Typ — die Sections heißen einfach nach dem Typ. Das matched
        # die statisch in strings.json hinterlegten Section-Labels.
        groups_by_type: dict[str, DeviceGroup] = {
            g.suggested_type: g for g in self._auto_groups
        }

        if user_input is not None:
            for dtype, group in groups_by_type.items():
                section_data = user_input.get(dtype, {})
                if not section_data:
                    continue
                if not section_data.get(f"{dtype}_include", True):
                    continue
                entity_input: dict[str, Any] = {}
                for slot in self._AUTO_SLOTS:
                    val = section_data.get(f"{dtype}_{slot}", "")
                    if val:
                        entity_input[slot] = val
                config_mode = (
                    CONFIG_MODE_CLIMATE
                    if entity_input.get(CONF_ENTITY_CLIMATE)
                       or entity_input.get(CONF_ENTITY_WATER_HEATER)
                    else CONFIG_MODE_MANUAL
                )
                self._auto_queue.append(
                    {
                        "device_type": section_data.get(
                            f"{dtype}_type", group.suggested_type
                        ),
                        "device_name": section_data.get(
                            f"{dtype}_name", group.suggested_name
                        ),
                        "config_mode": config_mode,
                        "entity_input": entity_input,
                    }
                )
            return await self._auto_process_next()

        # Form-Schema bauen: eine vol.section pro Crowdergy-Typ. Die
        # Section-Header kommen aus strings.json (statisch pro Typ);
        # die Confidence + der HA-Device-Name landen oben im Form-
        # Description-Block via `{summary}`-Placeholder. So sieht der
        # User auf einen Blick welche Gruppe mit welcher Confidence
        # erkannt wurde, bevor er ins Detail klappt.
        schema_dict: dict[Any, Any] = {}
        summary_lines: list[str] = []
        # Pro-Section-Description-Placeholder. `solar_conf` / `battery_
        # _conf` etc. werden in strings.json sections.<type>.description
        # referenziert, sodass die Confidence direkt unter der Section-
        # Überschrift steht (sichtbarer als nur oben in der Summary).
        section_placeholders: dict[str, str] = {
            f"{dtype}_conf": "—" for dtype in DEVICE_TYPES
        }

        for dtype, group in groups_by_type.items():
            slot_map = group.slot_map()
            section_schema: dict[Any, Any] = {
                vol.Optional(f"{dtype}_include", default=True): bool,
                vol.Required(
                    f"{dtype}_name", default=group.suggested_name
                ): str,
                vol.Required(
                    f"{dtype}_type", default=group.suggested_type
                ): vol.In(DEVICE_TYPES),
            }
            section_defaults: dict[str, Any] = {
                f"{dtype}_include": True,
                f"{dtype}_name": group.suggested_name,
                f"{dtype}_type": group.suggested_type,
            }
            for slot in self._AUTO_SLOTS:
                slot_key = f"{dtype}_{slot}"
                heuristic_pick = slot_map.get(slot)
                # vol.Optional ohne default — voluptuous-EntitySelector
                # akzeptiert leere Strings nicht als „nicht gewählt".
                # Wenn die Heuristik einen Pick hat, geht der ins
                # section_defaults; sonst bleibt der Slot leer und
                # taucht erst gar nicht im default-Dict auf.
                section_schema[vol.Optional(slot_key)] = selector.EntitySelector(
                    selector.EntitySelectorConfig(multiple=False)
                )
                if heuristic_pick:
                    section_defaults[slot_key] = heuristic_pick
            schema_dict[
                vol.Optional(dtype, default=section_defaults)
            ] = section(vol.Schema(section_schema), {"collapsed": False})

            confidence_pct = int(round(group.avg_confidence * 100))
            marker = "✓" if group.avg_confidence >= HEURISTIC_ACCEPT else "?"
            type_label = DEVICE_TYPE_LABELS_DE.get(dtype, dtype)
            summary_lines.append(
                f"{marker} **{type_label}** — {group.suggested_name} "
                f"({confidence_pct}%)"
            )
            section_placeholders[f"{dtype}_conf"] = (
                f"{marker} {group.suggested_name} · {confidence_pct}%"
            )

        return self.async_show_form(
            step_id="auto_confirm",
            data_schema=vol.Schema(schema_dict),
            description_placeholders={
                "device_count": str(len(self._auto_groups)),
                "summary": "\n\n".join(summary_lines),
                **section_placeholders,
            },
        )

    async def _auto_process_next(self) -> ConfigFlowResult:
        """Pop des nächsten Auto-Devices aus der Queue und weiter durch
        den klassischen Per-Device-Value-Step-Pfad. Queue leer →
        finish.
        """
        if not self._auto_queue:
            return await self.async_step_finish()
        pending = self._auto_queue.pop(0)
        self._pending_type = pending["device_type"]
        self._pending_name = pending["device_name"]
        self._pending_config_mode = pending.get("config_mode", CONFIG_MODE_MANUAL)
        entity_input = pending.get("entity_input", {})
        # Dispatch geht durch dieselben Value-Steps wie der Manuell-
        # Flow — Mode-Werte (Lock/Solar/Power, charge/idle/discharge,
        # plugged/unplugged, value_on/value_off) werden gewohnt
        # abgefragt; nur die Entity-Auswahl ist aus der Heuristik
        # vorbelegt. Nach _register_with_entities landet der Flow in
        # `async_step_add_more` — den hooken wir unten so dass Auto-
        # Mode dort direkt das nächste Queue-Element nachzieht.
        return await self._dispatch_post_entities(entity_input)

    async def async_step_device_type(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: pick the device type + name."""
        if user_input is not None:
            self._pending_type = user_input[CONF_DEVICE_TYPE]
            self._pending_name = user_input[CONF_DEVICE_NAME]
            # Reset Preset-State zwischen Devices (z.B. wenn der User
            # mehrere Geräte hintereinander anlegt).
            self._pending_preset_entity_map = None
            self._pending_preset_value_map = None
            # v3.0: WP-Typen (heating, warmwater) bekommen einen
            # KonfigMode-Step danach. Andere Typen skippen direkt zu
            # device_entities mit implizitem config_mode = manual.
            if self._pending_type in {"heating", "warmwater", "aircon"}:
                return await self.async_step_device_config_mode()
            self._pending_config_mode = CONFIG_MODE_MANUAL
            # FEAT-1 (2026-06-09, erweitert 2026-06-11): vor dem
            # manuellen Entity-Step prüfen ob Hersteller-Presets
            # verfügbar sind — für alle preset-fähigen Typen aus dem
            # Mapping-Dictionary (vorher solar-only).
            if self._pending_type in PRESET_CAPABLE_TYPES:
                return await self.async_step_vendor_preset_pick()
            return await self.async_step_device_entities()

        return self.async_show_form(
            step_id="device_type",
            data_schema=_type_name_schema(),
            description_placeholders={"device_number": str(len(self._devices) + 1)},
        )

    async def async_step_vendor_preset_pick(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """FEAT-1 v0.1 (2026-06-09): zeigt verfügbare Hersteller-Presets
        für den aktuellen Device-Type. User wählt entweder ein Preset
        (Entity-IDs werden im nächsten Step vorausgefüllt) oder
        „Manuell konfigurieren" → klassischer Flow."""
        if user_input is not None:
            choice = user_input.get("preset_choice", "__manual__")
            if choice != "__manual__" and self._pending_lookup_cache:
                maps = _picked_preset_maps(self._pending_lookup_cache, choice)
                if maps is not None:
                    self._pending_preset_entity_map = maps[0]
                    self._pending_preset_value_map = maps[1]
            return await self.async_step_device_entities()

        api_url = self._data.get(CONF_API_URL, "")
        token = self._data.get(CONF_ACCESS_TOKEN, "")
        presets: list[dict[str, Any]] = []
        if api_url and token and self._pending_type:
            # Initial-Flow: noch kein Entry → explizite Credentials
            # (frisch aus dem Login), kein 401-Refresh nötig.
            presets = await _fetch_vendor_presets(
                self.hass, self._pending_type,
                api_url=api_url, token=token,
            )
        # Wenn 0 Presets → skip diesen Step komplett, kein User-Hick-Up
        # mit leerem Picker.
        if not presets:
            return await self.async_step_device_entities()
        self._pending_lookup_cache = presets
        return self.async_show_form(
            step_id="vendor_preset_pick",
            data_schema=_vendor_preset_pick_schema(presets),
            description_placeholders={
                "device_type": DEVICE_TYPE_LABELS_DE.get(
                    self._pending_type or "", self._pending_type or "",
                ),
                "count": str(len(presets)),
            },
        )

    async def async_step_device_config_mode(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1b (heating/warmwater only): KonfigMode-Picker.
        Manuell = klassisch alle Entities einzeln. Climate-Entity =
        moderne climate.* Integration, Steuerung + Modi + Ist-Temp
        kommen automatisch. Edit-Flow skippt diesen Step — wer den
        Modus wechseln will, entfernt das Gerät und legt's neu an.
        """
        if user_input is not None:
            self._pending_config_mode = user_input.get(
                CONF_DEVICE_CONFIG_MODE, CONFIG_MODE_MANUAL
            )
            return await self.async_step_device_entities()

        return self.async_show_form(
            step_id="device_config_mode",
            data_schema=_config_mode_schema(),
            description_placeholders={
                "device_type": DEVICE_TYPE_LABELS_DE.get(
                    self._pending_type or "", self._pending_type or ""
                ),
                "device_name": self._pending_name or "",
            },
        )

    async def async_step_device_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: type-specific entity mapping."""
        device_type = self._pending_type or "generic"
        device_name = self._pending_name or ""

        if user_input is not None:
            entity_input = _apply_climate_first(
                _flatten_sections(user_input),
                device_type=device_type,
            )
            return await self._dispatch_post_entities(entity_input)

        # FEAT-1 v0.1 (2026-06-09): wenn der User im vorigen Step ein
        # Hersteller-Preset ausgewählt hat, sind die Entity-IDs in
        # `_pending_preset_entity_map` und werden als Suggested-Defaults
        # ans Schema übergeben. _entities_schema rendert sie als
        # `suggested_value` damit der User sie sehen und ändern kann
        # bevor er bestätigt.
        defaults = self._pending_preset_entity_map or None
        return self.async_show_form(
            step_id="device_entities",
            data_schema=_entities_schema(
                device_type,
                defaults=defaults,
                config_mode=self._pending_config_mode,
            ),
            description_placeholders={
                "device_number": str(len(self._devices) + 1),
                "device_type": DEVICE_TYPE_LABELS_DE.get(device_type, device_type),
                "device_name": device_name,
            },
        )

    async def _dispatch_post_entities(
        self, entity_input: dict[str, Any]
    ) -> ConfigFlowResult:
        """Decide which follow-up step (if any) to render next.

        Order:
          1. Charge-mode-values ternary mapping (Aus / An / Solaroptimiert)
                                            — wallbox + entity_charge_mode set
          2. Vehicle-status ternary mapping  — wallbox + entity_vehicle_status set
          3. value_on / value_off           — controllable + non-binary control
          4. Shares-hardware picker         — warmwater
          5. Otherwise: register the device

        value_on / value_off is entity-level config (defines what the
        connector writes to `entity_control`) and is a precondition
        for ANY on/off automation. Shares-hardware is a household
        coupling that only affects the joint MILP. Asking values
        first keeps setup-flow semantics top-down (entity → coupling
        → register) and surfaces the values step for warmwater
        devices, which used to be skipped over by the shares step's
        early return on its second visit.

        Each branch stashes the (in-progress) entity_input on
        `self._pending_entity_input` so the next step can pick up
        where this one left off.
        """
        device_type = self._pending_type or "generic"

        if (
            device_type == "wallbox"
            and entity_input.get(CONF_ENTITY_CHARGE_MODE)
            and CONF_CHARGE_MODE_VALUE_LOCK not in entity_input
        ):
            self._pending_entity_input = entity_input
            return await self.async_step_device_charge_mode_values()

        if (
            device_type == "battery"
            and (
                entity_input.get(CONF_ENTITY_CHARGE_MODE)
                or _preset_suggests_battery_control(self)
            )
            and CONF_ENTITY_BATTERY_MODE not in entity_input
        ):
            self._pending_entity_input = entity_input
            return await self.async_step_device_battery_values()

        if (
            device_type == "wallbox"
            and entity_input.get(CONF_ENTITY_VEHICLE_STATUS)
            and CONF_VEHICLE_STATUS_VALUE_PLUGGED not in entity_input
        ):
            # Binary-Sensor-Pfad: on/off ist die einzige sinnvolle
            # Belegung — auto-mappen und Step skippen.
            _auto_fill_binary_vehicle_status(entity_input)
            if CONF_VEHICLE_STATUS_VALUE_PLUGGED not in entity_input:
                self._pending_entity_input = entity_input
                return await self.async_step_device_vehicle_status()

        entity_control = entity_input.get(CONF_ENTITY_CONTROL, "")
        if (
            device_type in _CONTROLLABLE_TYPES
            and entity_control
            and not _is_binary_entity(entity_control)
            and CONF_VALUE_ON not in entity_input
        ):
            self._pending_entity_input = entity_input
            return await self.async_step_device_values()

        if (
            device_type == "warmwater"
            and CONF_SHARES_HARDWARE_WITH not in entity_input
        ):
            self._pending_entity_input = entity_input
            return await self.async_step_device_shares_hardware()

        # v3.0: legacy cooling step wird nicht mehr dispatched — das
        # Feld wird inline im device_values Step erfasst; fehlt der
        # Schlüssel, heißt das "kein cooling". (Das Haushalt-Flag ist
        # seit v3.26 komplett raus — ersetzt durch den parent_device_id-
        # Baum, konfiguriert in der Crowdergy-App.)
        return await self._register_with_entities(entity_input)

    async def async_step_device_charge_mode_values(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """v2.2 step: map the wallbox's HA charge-mode select-options
        to the three Crowdergy modes (Aus / An / Solaroptimiert).
        Each field is optional — modes left blank simply don't get a
        button in the iOS tile.
        """
        device_type = self._pending_type or "generic"
        device_name = self._pending_name or ""
        entity_input = dict(self._pending_entity_input or {})
        entity_charge_mode = entity_input.get(CONF_ENTITY_CHARGE_MODE, "")

        if user_input is not None:
            entity_input[CONF_CHARGE_MODE_VALUE_LOCK] = user_input.get(
                CONF_CHARGE_MODE_VALUE_LOCK, ""
            )
            entity_input[CONF_CHARGE_MODE_VALUE_POWER] = user_input.get(
                CONF_CHARGE_MODE_VALUE_POWER, ""
            )
            entity_input[CONF_CHARGE_MODE_VALUE_SOLAR] = user_input.get(
                CONF_CHARGE_MODE_VALUE_SOLAR, ""
            )
            entity_input[CONF_ENTITY_CONTROL_HOLD] = user_input.get(
                CONF_ENTITY_CONTROL_HOLD, ENTITY_CONTROL_HOLD_AUTO
            )
            return await self._dispatch_post_entities(entity_input)

        return self.async_show_form(
            step_id="device_charge_mode_values",
            data_schema=_charge_mode_values_schema(
                self.hass, entity_charge_mode, _preset_step_defaults(self)
            ),
            description_placeholders={
                "device_type": DEVICE_TYPE_LABELS_DE.get(device_type, device_type),
                "device_name": device_name,
                "entity_charge_mode": entity_charge_mode,
            },
        )

    async def async_step_device_battery_values(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """v3.8.0 (Phase 3 Option D, 2026-06-02): Battery-Dispatch via
        Lademodus-Select-Entity + Power-Setpoint-Number. Backend
        liefert pro Tick continuous setpoint_kw + Mode-Tag, Connector
        schreibt direkt auf die mapped Entities (kein mehr-4-strings-
        Mapping wie pre-v3.8).
        """
        device_type = self._pending_type or "generic"
        device_name = self._pending_name or ""
        entity_input = dict(self._pending_entity_input or {})

        if user_input is not None:
            for key in (
                CONF_ENTITY_BATTERY_MODE,
                CONF_VALUE_BATTERY_MODE_ACTIVE,
                CONF_VALUE_BATTERY_MODE_PASSIVE,
                CONF_ENTITY_BATTERY_POWER_SETPOINT,
            ):
                entity_input[key] = user_input.get(key, "")
            entity_input[CONF_BATTERY_SETPOINT_INVERT_SIGN] = bool(
                user_input.get(CONF_BATTERY_SETPOINT_INVERT_SIGN, False)
            )
            entity_input[CONF_ENTITY_CONTROL_HOLD] = user_input.get(
                CONF_ENTITY_CONTROL_HOLD, ENTITY_CONTROL_HOLD_AUTO
            )
            return await self._dispatch_post_entities(entity_input)

        return self.async_show_form(
            step_id="device_battery_values",
            data_schema=_battery_values_schema(
                self.hass, _preset_step_defaults(self)
            ),
            description_placeholders={
                "device_type": DEVICE_TYPE_LABELS_DE.get(device_type, device_type),
                "device_name": device_name,
            },
        )

    async def async_step_device_vehicle_status(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """v2.0 step: map the wallbox's vehicle-status sensor states
        to the normalised plugged / unplugged / error trio."""
        device_type = self._pending_type or "generic"
        device_name = self._pending_name or ""
        entity_input = dict(self._pending_entity_input or {})
        entity_vehicle_status = entity_input.get(CONF_ENTITY_VEHICLE_STATUS, "")

        if user_input is not None:
            entity_input[CONF_VEHICLE_STATUS_VALUE_PLUGGED] = user_input.get(
                CONF_VEHICLE_STATUS_VALUE_PLUGGED, ""
            )
            entity_input[CONF_VEHICLE_STATUS_VALUE_UNPLUGGED] = user_input.get(
                CONF_VEHICLE_STATUS_VALUE_UNPLUGGED, ""
            )
            entity_input[CONF_VEHICLE_STATUS_VALUE_ERROR] = user_input.get(
                CONF_VEHICLE_STATUS_VALUE_ERROR, ""
            )
            return await self._dispatch_post_entities(entity_input)

        return self.async_show_form(
            step_id="device_vehicle_status",
            data_schema=_vehicle_status_schema(
                self.hass, entity_vehicle_status, _preset_step_defaults(self)
            ),
            description_placeholders={
                "device_type": DEVICE_TYPE_LABELS_DE.get(device_type, device_type),
                "device_name": device_name,
                "entity_vehicle_status": entity_vehicle_status,
            },
        )

    async def async_step_device_shares_hardware(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """v2.0 step: link a warmwater device to its sibling heating
        device on the same compressor (optional)."""
        device_type = self._pending_type or "generic"
        device_name = self._pending_name or ""
        entity_input = dict(self._pending_entity_input or {})

        heating_devices = [
            {"id": d.get(CONF_DEVICE_ID, ""), "name": d.get(CONF_DEVICE_NAME, "")}
            for d in self._devices
            if d.get(CONF_DEVICE_TYPE) == "heating"
        ]

        if user_input is not None:
            entity_input[CONF_SHARES_HARDWARE_WITH] = user_input.get(
                CONF_SHARES_HARDWARE_WITH, ""
            )
            return await self._dispatch_post_entities(entity_input)

        return self.async_show_form(
            step_id="device_shares_hardware",
            data_schema=_shares_hardware_schema(heating_devices, {}),
            description_placeholders={
                "device_type": DEVICE_TYPE_LABELS_DE.get(device_type, device_type),
                "device_name": device_name,
            },
        )

    async def async_step_device_values(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 3: typ-bewusste value_on / value_off für entity_control.
        v3.0 heating + climate-Mode: value_cool_on/off direkt inline
        statt eigenem Cooling-Step. Leer = kein Cooling.
        """
        device_type = self._pending_type or "generic"
        device_name = self._pending_name or ""
        entity_input = dict(self._pending_entity_input or {})
        entity_control = entity_input.get(CONF_ENTITY_CONTROL, "")
        # aircon: cooling immer aktiv. heating: nur im Climate-Mode.
        include_cooling = device_type == "aircon" or (
            device_type == "heating"
            and self._pending_config_mode == CONFIG_MODE_CLIMATE
        )

        if user_input is not None:
            entity_input[CONF_VALUE_ON] = user_input.get(CONF_VALUE_ON, "")
            entity_input[CONF_VALUE_OFF] = user_input.get(CONF_VALUE_OFF, "")
            if include_cooling:
                entity_input[CONF_VALUE_COOL_ON] = user_input.get(
                    CONF_VALUE_COOL_ON, ""
                )
            entity_input[CONF_ENTITY_CONTROL_HOLD] = user_input.get(
                CONF_ENTITY_CONTROL_HOLD, ENTITY_CONTROL_HOLD_AUTO
            )
            return await self._dispatch_post_entities(entity_input)

        return self.async_show_form(
            step_id="device_values",
            data_schema=_values_schema(
                self.hass, entity_control, {},
                include_cooling=include_cooling,
                cooling_first=device_type == "aircon",
            ),
            description_placeholders={
                "device_type": DEVICE_TYPE_LABELS_DE.get(device_type, device_type),
                "device_name": device_name,
                "entity_control": entity_control,
            },
        )

    async def _register_with_entities(
        self, entity_input: dict[str, Any]
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        device_type = self._pending_type or "generic"
        device_name = self._pending_name or ""
        # v3.0: ConfigMode aus dem Pending-State ins entity_input
        # einbringen, sodass _build_device_record es persistiert.
        entity_input.setdefault(
            CONF_DEVICE_CONFIG_MODE, self._pending_config_mode
        )
        try:
            # Initial-Flow: noch kein Entry → explizite Credentials.
            dev = await _register_device(
                self.hass,
                None,
                device_type,
                device_name,
                entity_input,
                self._data,
                api_url=self._data[CONF_API_URL],
                token=self._data[CONF_ACCESS_TOKEN],
            )
            self._devices.append(dev)
            self._pending_type = None
            self._pending_name = None
            self._pending_config_mode = CONFIG_MODE_MANUAL
            self._pending_entity_input = None
            return await self.async_step_add_more()
        except (httpx.HTTPStatusError, httpx.RequestError) as err:
            _LOGGER.error("Failed to register device: %s", err)
            errors["base"] = "cannot_connect"
            return self.async_show_form(
                step_id="device_entities",
                data_schema=_entities_schema(device_type, config_mode=self._pending_config_mode),
                description_placeholders={
                    "device_number": str(len(self._devices) + 1),
                    "device_type": DEVICE_TYPE_LABELS_DE.get(device_type, device_type),
                    "device_name": device_name,
                },
                errors=errors,
            )

    async def async_step_add_more(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        # Auto-Mode walking durch die Queue — solange noch was offen
        # ist, sofort weiter mit dem nächsten Device statt den Menu-
        # Dialog zu zeigen. Erst wenn die Queue leer ist gibt's das
        # gewohnte „weiter hinzufügen / fertig" Menü.
        if self._auto_queue:
            return await self._auto_process_next()
        return self.async_show_menu(
            step_id="add_more",
            menu_options=["device_type", "finish"],
        )

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._data[CONF_DEVICES] = self._devices
        title = f"Crowdergy ({len(self._devices)} Geräte)"
        return self.async_create_entry(title=title, data=self._data)


# ── Options Flow (add / edit / remove devices after setup) ──────────────────


class CrowdergyOptionsFlow(OptionsFlow):
    """Handle options for Crowdergy Connector."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry
        self._devices: list[dict[str, Any]] = list(
            config_entry.data.get(CONF_DEVICES, [])
        )
        # Add-flow scratch state.
        self._pending_type: str | None = None
        self._pending_name: str | None = None
        self._pending_config_mode: str = CONFIG_MODE_MANUAL
        self._pending_entity_input: dict[str, Any] | None = None
        # Edit-flow scratch state.
        self._edit_target_id: str | None = None
        self._edit_pending_type: str | None = None
        self._edit_pending_name: str | None = None
        self._edit_pending_entity_input: dict[str, Any] | None = None
        # FEAT-1 Sprint C v0.1 (2026-06-09): scratch für Crowd-
        # Contribution-Step. Hält die device_id zwischen Device-Picker-
        # Step und dem Vendor/Model-Form-Step.
        self._contribute_target_id: str | None = None
        # FEAT-1 v0.2 (2026-06-09): Vendor-Preset-Pickup auch im
        # Options-Flow-Add. Spiegel der entsprechenden Attrs im
        # CrowdergyConfigFlow — Picker zeigt Presets aus dem Backend,
        # gewählte Entity-IDs landen als suggested_values im
        # add_device_entities-Step.
        self._pending_preset_entity_map: dict[str, str] | None = None
        # value_map des Presets als Defaults der Werte-Steps (Mapping-
        # Store 2026-06-11) — Spiegel des Initial-Flow-Attributs.
        self._pending_preset_value_map: dict[str, str] | None = None
        self._pending_lookup_cache: list[dict[str, Any]] = []

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        # FEAT-1 v0.3 (2026-06-09): „Grundeinstellungen" als Top-
        # Eintrag (Ort + Außentemp gebündelt, ersetzt
        # edit_outdoor_temp). Reihenfolge: erst Grundkonfig, dann
        # Devices, dann Crowd-Beitrag.
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "edit_base_settings",
                "add_device",
                "edit_device",
                "remove_device",
                "contribute_preset",
                "done",
            ],
        )

    # ── Crowd-Contribution (FEAT-1 Sprint C, 2026-06-09) ───────────────────
    #
    # User submittet ein bereits konfiguriertes Device als Vendor-Preset.
    # v0.2 (Mapping-Store 2026-06-11): alle preset-fähigen Typen aus dem
    # Mapping-Dictionary (preset_spec.PRESET_SLOT_SPEC) statt solar-only.
    # Die Anonymisierung liegt jetzt in der Spec selbst: NUR die dort
    # spezifizierten Slots verlassen die Installation (Allowlist statt
    # „alle entity_*-Keys"), plus Vollständigkeits-Gate auf die
    # Pflicht-Slots, damit nur box-taugliche Beiträge im Store landen.

    async def async_step_contribute_preset(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick which configured device to contribute as a vendor preset."""
        candidates = [
            d for d in self._devices
            if d.get(CONF_DEVICE_TYPE) in PRESET_CAPABLE_TYPES
        ]
        if not candidates:
            return self.async_abort(reason="contribute_no_devices")

        if user_input is not None:
            device_id = user_input[CONF_DEVICE_ID]
            dev = next(
                (d for d in candidates if d.get(CONF_DEVICE_ID) == device_id),
                None,
            )
            if dev is None:
                return self.async_abort(reason="contribute_device_missing")
            # Vollständigkeits-Gate VOR dem Vendor/Model-Formular: ein
            # unvollständiges Gerät erst gar nicht beschreiben lassen,
            # sondern die fehlenden Pflicht-Slots benennen.
            missing = missing_required_labels(dev)
            if missing:
                return self.async_abort(
                    reason="contribute_incomplete",
                    description_placeholders={
                        "device_name": dev.get(CONF_DEVICE_NAME, device_id),
                        "missing": ", ".join(missing),
                    },
                )
            self._contribute_target_id = device_id
            return await self.async_step_contribute_preset_form()

        options = {
            d[CONF_DEVICE_ID]: (
                f"{d.get(CONF_DEVICE_NAME, d[CONF_DEVICE_ID])} "
                f"({DEVICE_TYPE_LABELS_DE.get(d.get(CONF_DEVICE_TYPE, ''), d.get(CONF_DEVICE_TYPE, ''))})"
            )
            for d in candidates
        }
        return self.async_show_form(
            step_id="contribute_preset",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DEVICE_ID): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                {"value": k, "label": v}
                                for k, v in options.items()
                            ],
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    ),
                }
            ),
        )

    async def async_step_contribute_preset_form(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Form für Vendor + Model + optional Notes. Submit → POST an
        Backend `/crowd-presets/contribute`. Zeigt Erfolg/Fehler als
        async_abort-Screen."""
        if user_input is not None:
            vendor = (user_input.get("vendor") or "").strip()
            model = (user_input.get("model") or "").strip()
            notes = (user_input.get("notes") or "").strip() or None
            if not vendor or not model:
                return self.async_show_form(
                    step_id="contribute_preset_form",
                    data_schema=_contribute_form_schema(vendor, model, notes),
                    errors={"base": "vendor_model_required"},
                )
            dev = next(
                (d for d in self._devices
                 if d.get(CONF_DEVICE_ID) == self._contribute_target_id),
                None,
            )
            if dev is None:
                return self.async_abort(reason="contribute_device_missing")

            # Mapping-Store (2026-06-11): Slot-Extraktion strikt über
            # die Spec (preset_spec.PRESET_SLOT_SPEC, Vertrag:
            # docs/crowd-preset-store.md) — entity_map (Entity-Slots) +
            # value_map (integrationsspezifische Werte/Flags). Nichts
            # außerhalb der Allowlist verlässt die Installation.
            entity_map, value_map = extract_preset_maps(dev)
            if not entity_map:
                return self.async_abort(reason="contribute_no_entities")

            # integration_domain mitschicken, sonst filtert der
            # box-manager das Preset raus (SUPPORTED_INTEGRATIONS
            # check). Quelle: HA-Entity-Registry → ConfigEntry.domain
            # (Entity-IDs sind frei vom User umbenennbar, die Domain
            # nicht); bei gemischten Setups gewinnt die HÄUFIGSTE
            # Domain (`dominant_integration_domain` — #97: vorher wurde
            # hier zusätzlich ein zweiter first-resolvable-Wert
            # berechnet und sofort überschrieben). None = Template-/
            # Helper-only-Mapping; das Backend akzeptiert NULL.
            from .entity_mapper import dominant_integration_domain

            payload = {
                "device_type": dev[CONF_DEVICE_TYPE],
                "vendor": vendor,
                "model": model,
                "entity_map": entity_map,
                "notes": notes,
                "integration_domain": dominant_integration_domain(
                    self.hass, list(entity_map.values())
                ),
            }
            if value_map:
                payload["value_map"] = value_map
            # CN-12 (2026-06-11): über `_authenticated_config_request`
            # (401-Refresh + Client-Bau im Executor). CN-14: auch das
            # JSON-Parsing defensiv — ValueError landet im selben
            # User-Fehlerpfad statt den Flow zu crashen.
            try:
                response = await _authenticated_config_request(
                    self.hass, self._entry,
                    "POST", "/api/v1/crowd-presets/contribute",
                    json=payload,
                )
                response.raise_for_status()
                result = response.json()
            except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as err:
                _LOGGER.warning("Crowd-Contribution POST failed: %s", err)
                return self.async_abort(
                    reason="contribute_backend_error",
                    description_placeholders={"err": str(err)[:160]},
                )

            return self.async_abort(
                reason="contribute_success",
                description_placeholders={
                    "vendor": vendor,
                    "model": model,
                    "status": result.get("status", "?"),
                    "count": str(result.get("contribution_count", "?")),
                },
            )

        return self.async_show_form(
            step_id="contribute_preset_form",
            data_schema=_contribute_form_schema(None, None, None),
        )

    async def async_step_edit_base_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """FEAT-1 v0.3 (2026-06-09): „Grundeinstellungen" — kombiniert
        Ort (district / city / region) und Außentemperatur-Sensor in
        einem Step. Ersetzt das vorige `edit_outdoor_temp` und macht
        den Ort nachträglich editierbar (der frühere Onboarding-Flow
        hat ihn nur einmal abgefragt).

        Persistiert direkt auf `entry.data` und springt zurück ins
        Hauptmenü. Leeres outdoor-temp-Feld setzt das Mapping zurück
        → Backend nutzt Open-Meteo-Fallback für deinen Stadtteil.
        """
        if user_input is not None:
            new_data = {
                **self._entry.data,
                CONF_DISTRICT: user_input.get(CONF_DISTRICT, "").strip(),
                CONF_CITY: user_input.get(CONF_CITY, "").strip(),
                CONF_REGION: user_input.get(CONF_REGION, "").strip(),
                CONF_ENTITY_OUTDOOR_TEMP: user_input.get(
                    CONF_ENTITY_OUTDOOR_TEMP, "",
                ),
            }
            self.hass.config_entries.async_update_entry(
                self._entry, data=new_data,
            )
            return await self.async_step_init()

        current_district = self._entry.data.get(CONF_DISTRICT, "")
        current_city = self._entry.data.get(CONF_CITY, "")
        current_region = self._entry.data.get(CONF_REGION, "")
        current_outdoor = self._entry.data.get(CONF_ENTITY_OUTDOOR_TEMP, "")

        # Erstes Öffnen ohne irgendwelche gespeicherten Ort-Werte: HA-
        # Defaults aus latitude/longitude pre-fillen damit der User
        # normalerweise nur „Speichern" tippen muss.
        if not (current_district or current_city or current_region):
            location_defaults = await _resolve_location_defaults(self.hass)
            current_district = location_defaults.get(CONF_DISTRICT, "")
            current_city = location_defaults.get(CONF_CITY, "")
            current_region = location_defaults.get(CONF_REGION, "")

        def _str_field(key: str, current: str) -> Any:
            if current:
                return vol.Optional(
                    key, description={"suggested_value": current},
                )
            return vol.Optional(key)

        outdoor_field: Any = (
            vol.Optional(
                CONF_ENTITY_OUTDOOR_TEMP,
                description={"suggested_value": current_outdoor},
            )
            if current_outdoor
            else vol.Optional(CONF_ENTITY_OUTDOOR_TEMP)
        )
        return self.async_show_form(
            step_id="edit_base_settings",
            data_schema=vol.Schema(
                {
                    _str_field(CONF_DISTRICT, current_district): str,
                    _str_field(CONF_CITY, current_city): str,
                    _str_field(CONF_REGION, current_region): str,
                    outdoor_field: selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="sensor")
                    ),
                }
            ),
        )

    # ── Add-Device (two-step) ───────────────────────────────────────────────

    async def async_step_add_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1 (add): pick type + name."""
        if user_input is not None:
            self._pending_type = user_input[CONF_DEVICE_TYPE]
            self._pending_name = user_input[CONF_DEVICE_NAME]
            # Reset Preset-State zwischen Devices (User legt mehrere
            # nacheinander an).
            self._pending_preset_entity_map = None
            self._pending_preset_value_map = None
            if self._pending_type in {"heating", "warmwater", "aircon"}:
                return await self.async_step_add_device_config_mode()
            self._pending_config_mode = CONFIG_MODE_MANUAL
            # FEAT-1 v0.2 (2026-06-09, erweitert 2026-06-11): Vendor-
            # Preset-Picker für alle preset-fähigen Typen.
            if self._pending_type in PRESET_CAPABLE_TYPES:
                return await self.async_step_add_vendor_preset_pick()
            return await self.async_step_add_device_entities()

        return self.async_show_form(
            step_id="add_device",
            data_schema=_type_name_schema(),
        )

    async def async_step_add_vendor_preset_pick(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Options-Flow-Add Variante des Vendor-Preset-Pickers.
        Identische Semantik zur Initial-Config-Flow-Version: 0 Treffer
        → skip, sonst Picker mit __manual__ als Skip-Option."""
        if user_input is not None:
            choice = user_input.get("preset_choice", "__manual__")
            if choice != "__manual__" and self._pending_lookup_cache:
                maps = _picked_preset_maps(self._pending_lookup_cache, choice)
                if maps is not None:
                    self._pending_preset_entity_map = maps[0]
                    self._pending_preset_value_map = maps[1]
            return await self.async_step_add_device_entities()

        presets: list[dict[str, Any]] = []
        if self._pending_type:
            presets = await _fetch_vendor_presets(
                self.hass, self._pending_type, entry=self._entry,
            )
        if not presets:
            return await self.async_step_add_device_entities()
        self._pending_lookup_cache = presets
        return self.async_show_form(
            step_id="add_vendor_preset_pick",
            data_schema=_vendor_preset_pick_schema(presets),
            description_placeholders={
                "device_type": DEVICE_TYPE_LABELS_DE.get(
                    self._pending_type or "", self._pending_type or "",
                ),
                "count": str(len(presets)),
            },
        )

    async def async_step_add_device_config_mode(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """v3.0 add-flow Variante des KonfigMode-Pickers."""
        if user_input is not None:
            self._pending_config_mode = user_input.get(
                CONF_DEVICE_CONFIG_MODE, CONFIG_MODE_MANUAL
            )
            return await self.async_step_add_device_entities()

        return self.async_show_form(
            step_id="add_device_config_mode",
            data_schema=_config_mode_schema(),
            description_placeholders={
                "device_type": DEVICE_TYPE_LABELS_DE.get(
                    self._pending_type or "", self._pending_type or ""
                ),
                "device_name": self._pending_name or "",
            },
        )

    async def async_step_add_device_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2 (add): type-specific entity mapping."""
        device_type = self._pending_type or "generic"
        device_name = self._pending_name or ""

        if user_input is not None:
            entity_input = _apply_climate_first(
                _flatten_sections(user_input),
                device_type=device_type,
            )
            return await self._dispatch_add_post_entities(entity_input)

        defaults = self._pending_preset_entity_map or None
        return self.async_show_form(
            step_id="add_device_entities",
            data_schema=_entities_schema(
                device_type,
                defaults=defaults,
                config_mode=self._pending_config_mode,
            ),
            description_placeholders={
                "device_type": DEVICE_TYPE_LABELS_DE.get(device_type, device_type),
                "device_name": device_name,
            },
        )

    async def _dispatch_add_post_entities(
        self, entity_input: dict[str, Any]
    ) -> ConfigFlowResult:
        """Options-flow add path: same routing logic as the main
        config flow's `_dispatch_post_entities` (charge-mode-values →
        vehicle-status → values → shares-hardware → register).
        Values runs before shares-hardware because it's entity-level
        config, while shares-hardware is a household-level coupling."""
        device_type = self._pending_type or "generic"

        if (
            device_type == "wallbox"
            and entity_input.get(CONF_ENTITY_CHARGE_MODE)
            and CONF_CHARGE_MODE_VALUE_LOCK not in entity_input
        ):
            self._pending_entity_input = entity_input
            return await self.async_step_add_device_charge_mode_values()

        if (
            device_type == "battery"
            and (
                entity_input.get(CONF_ENTITY_CHARGE_MODE)
                or _preset_suggests_battery_control(self)
            )
            and CONF_ENTITY_BATTERY_MODE not in entity_input
        ):
            self._pending_entity_input = entity_input
            return await self.async_step_add_device_battery_values()

        if (
            device_type == "wallbox"
            and entity_input.get(CONF_ENTITY_VEHICLE_STATUS)
            and CONF_VEHICLE_STATUS_VALUE_PLUGGED not in entity_input
        ):
            _auto_fill_binary_vehicle_status(entity_input)
            if CONF_VEHICLE_STATUS_VALUE_PLUGGED not in entity_input:
                self._pending_entity_input = entity_input
                return await self.async_step_add_device_vehicle_status()

        entity_control = entity_input.get(CONF_ENTITY_CONTROL, "")
        if (
            device_type in _CONTROLLABLE_TYPES
            and entity_control
            and not _is_binary_entity(entity_control)
            and CONF_VALUE_ON not in entity_input
        ):
            self._pending_entity_input = entity_input
            return await self.async_step_add_device_values()

        if (
            device_type == "warmwater"
            and CONF_SHARES_HARDWARE_WITH not in entity_input
        ):
            self._pending_entity_input = entity_input
            return await self.async_step_add_device_shares_hardware()

        # v3.0: legacy cooling step entfernt — inline erfasst (siehe
        # initial-Setup-Flow); Haushalt-Flag seit v3.26 komplett raus
        # (parent_device_id-Baum, App-konfiguriert).
        return await self._options_register(entity_input)

    async def async_step_add_device_charge_mode_values(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Options-flow variant of the wallbox Lademodus-Werte mapping
        (Aus / An / Solaroptimiert → wallbox HA select-options)."""
        device_type = self._pending_type or "generic"
        device_name = self._pending_name or ""
        entity_input = dict(self._pending_entity_input or {})
        entity_charge_mode = entity_input.get(CONF_ENTITY_CHARGE_MODE, "")

        if user_input is not None:
            entity_input[CONF_CHARGE_MODE_VALUE_LOCK] = user_input.get(
                CONF_CHARGE_MODE_VALUE_LOCK, ""
            )
            entity_input[CONF_CHARGE_MODE_VALUE_POWER] = user_input.get(
                CONF_CHARGE_MODE_VALUE_POWER, ""
            )
            entity_input[CONF_CHARGE_MODE_VALUE_SOLAR] = user_input.get(
                CONF_CHARGE_MODE_VALUE_SOLAR, ""
            )
            entity_input[CONF_ENTITY_CONTROL_HOLD] = user_input.get(
                CONF_ENTITY_CONTROL_HOLD, ENTITY_CONTROL_HOLD_AUTO
            )
            return await self._dispatch_add_post_entities(entity_input)

        return self.async_show_form(
            step_id="add_device_charge_mode_values",
            data_schema=_charge_mode_values_schema(
                self.hass, entity_charge_mode, _preset_step_defaults(self)
            ),
            description_placeholders={
                "device_type": DEVICE_TYPE_LABELS_DE.get(device_type, device_type),
                "device_name": device_name,
                "entity_charge_mode": entity_charge_mode,
            },
        )

    async def async_step_add_device_battery_values(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """v3.8.0 Options-flow variant: Battery-Setpoint-Dispatch."""
        device_type = self._pending_type or "generic"
        device_name = self._pending_name or ""
        entity_input = dict(self._pending_entity_input or {})

        if user_input is not None:
            for key in (
                CONF_ENTITY_BATTERY_MODE,
                CONF_VALUE_BATTERY_MODE_ACTIVE,
                CONF_VALUE_BATTERY_MODE_PASSIVE,
                CONF_ENTITY_BATTERY_POWER_SETPOINT,
            ):
                entity_input[key] = user_input.get(key, "")
            entity_input[CONF_BATTERY_SETPOINT_INVERT_SIGN] = bool(
                user_input.get(CONF_BATTERY_SETPOINT_INVERT_SIGN, False)
            )
            entity_input[CONF_ENTITY_CONTROL_HOLD] = user_input.get(
                CONF_ENTITY_CONTROL_HOLD, ENTITY_CONTROL_HOLD_AUTO
            )
            return await self._dispatch_add_post_entities(entity_input)

        return self.async_show_form(
            step_id="add_device_battery_values",
            data_schema=_battery_values_schema(
                self.hass, _preset_step_defaults(self)
            ),
            description_placeholders={
                "device_type": DEVICE_TYPE_LABELS_DE.get(device_type, device_type),
                "device_name": device_name,
            },
        )

    async def async_step_add_device_vehicle_status(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Options-flow variant of the wallbox vehicle-status mapping."""
        device_type = self._pending_type or "generic"
        device_name = self._pending_name or ""
        entity_input = dict(self._pending_entity_input or {})
        entity_vehicle_status = entity_input.get(CONF_ENTITY_VEHICLE_STATUS, "")

        if user_input is not None:
            entity_input[CONF_VEHICLE_STATUS_VALUE_PLUGGED] = user_input.get(
                CONF_VEHICLE_STATUS_VALUE_PLUGGED, ""
            )
            entity_input[CONF_VEHICLE_STATUS_VALUE_UNPLUGGED] = user_input.get(
                CONF_VEHICLE_STATUS_VALUE_UNPLUGGED, ""
            )
            entity_input[CONF_VEHICLE_STATUS_VALUE_ERROR] = user_input.get(
                CONF_VEHICLE_STATUS_VALUE_ERROR, ""
            )
            return await self._dispatch_add_post_entities(entity_input)

        return self.async_show_form(
            step_id="add_device_vehicle_status",
            data_schema=_vehicle_status_schema(
                self.hass, entity_vehicle_status, _preset_step_defaults(self)
            ),
            description_placeholders={
                "device_type": DEVICE_TYPE_LABELS_DE.get(device_type, device_type),
                "device_name": device_name,
                "entity_vehicle_status": entity_vehicle_status,
            },
        )

    async def async_step_add_device_shares_hardware(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Options-flow variant of the warmwater shares-hardware picker."""
        device_type = self._pending_type or "generic"
        device_name = self._pending_name or ""
        entity_input = dict(self._pending_entity_input or {})

        heating_devices = [
            {"id": d.get(CONF_DEVICE_ID, ""), "name": d.get(CONF_DEVICE_NAME, "")}
            for d in self._devices
            if d.get(CONF_DEVICE_TYPE) == "heating"
        ]

        if user_input is not None:
            entity_input[CONF_SHARES_HARDWARE_WITH] = user_input.get(
                CONF_SHARES_HARDWARE_WITH, ""
            )
            return await self._dispatch_add_post_entities(entity_input)

        return self.async_show_form(
            step_id="add_device_shares_hardware",
            data_schema=_shares_hardware_schema(heating_devices, {}),
            description_placeholders={
                "device_type": DEVICE_TYPE_LABELS_DE.get(device_type, device_type),
                "device_name": device_name,
            },
        )

    async def async_step_add_device_values(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 3 (add): typ-bewusste value_on / value_off. v3.0 inline
        Cooling für heating+climate (siehe device_values)."""
        device_type = self._pending_type or "generic"
        device_name = self._pending_name or ""
        entity_input = dict(self._pending_entity_input or {})
        entity_control = entity_input.get(CONF_ENTITY_CONTROL, "")
        # aircon: cooling immer aktiv. heating: nur im Climate-Mode.
        include_cooling = device_type == "aircon" or (
            device_type == "heating"
            and self._pending_config_mode == CONFIG_MODE_CLIMATE
        )

        if user_input is not None:
            entity_input[CONF_VALUE_ON] = user_input.get(CONF_VALUE_ON, "")
            entity_input[CONF_VALUE_OFF] = user_input.get(CONF_VALUE_OFF, "")
            if include_cooling:
                entity_input[CONF_VALUE_COOL_ON] = user_input.get(
                    CONF_VALUE_COOL_ON, ""
                )
            entity_input[CONF_ENTITY_CONTROL_HOLD] = user_input.get(
                CONF_ENTITY_CONTROL_HOLD, ENTITY_CONTROL_HOLD_AUTO
            )
            return await self._dispatch_add_post_entities(entity_input)

        return self.async_show_form(
            step_id="add_device_values",
            data_schema=_values_schema(
                self.hass, entity_control, {},
                include_cooling=include_cooling,
                cooling_first=device_type == "aircon",
            ),
            description_placeholders={
                "device_type": DEVICE_TYPE_LABELS_DE.get(device_type, device_type),
                "device_name": device_name,
                "entity_control": entity_control,
            },
        )

    async def _persist_devices(self, devices: list[dict[str, Any]]) -> None:
        """CN-7 (2026-06-11): Geräteliste SOFORT nach jeder Add/Edit/
        Remove-Operation in den Entry schreiben + Reload (Muster:
        `box_add_device`). Vorher persistierte erst der „Fertig"-Step
        einen Flow-Start-Snapshot — Dialog-Abbruch nach einer Operation
        ließ Backend und Entry auseinanderlaufen, und der Snapshot-
        Writeback überschrieb zwischenzeitliche Änderungen anderer
        Pfade (box_add_device, Geräteseiten-Löschung) — Lost-Update.

        `devices` ist die NEUE Liste, berechnet gegen den frischen
        `entry.data`-Stand (nicht gegen den Flow-Start-Snapshot).
        """
        self._devices = list(devices)
        new_data = {**self._entry.data, CONF_DEVICES: list(devices)}
        self.hass.config_entries.async_update_entry(self._entry, data=new_data)
        await self.hass.config_entries.async_reload(self._entry.entry_id)

    async def _options_register(
        self, entity_input: dict[str, Any]
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        device_type = self._pending_type or "generic"
        device_name = self._pending_name or ""
        entity_input.setdefault(
            CONF_DEVICE_CONFIG_MODE, self._pending_config_mode
        )
        try:
            dev = await _register_device(
                self.hass,
                self._entry,
                device_type,
                device_name,
                entity_input,
                self._entry.data,
            )
            await self._persist_devices(
                [*self._entry.data.get(CONF_DEVICES, []), dev]
            )
            self._pending_type = None
            self._pending_name = None
            self._pending_config_mode = CONFIG_MODE_MANUAL
            self._pending_entity_input = None
            return await self.async_step_init()
        except (httpx.HTTPStatusError, httpx.RequestError) as err:
            _LOGGER.error("Failed to register device: %s", err)
            errors["base"] = "cannot_connect"
            return self.async_show_form(
                step_id="add_device_entities",
                data_schema=_entities_schema(device_type, config_mode=self._pending_config_mode),
                description_placeholders={
                    "device_type": DEVICE_TYPE_LABELS_DE.get(device_type, device_type),
                    "device_name": device_name,
                },
                errors=errors,
            )

    # ── Edit-Device (two-step, defaults pre-filled) ─────────────────────────

    async def async_step_edit_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick which device to edit."""
        if not self._devices:
            return await self.async_step_init()

        if user_input is not None:
            self._edit_target_id = user_input["device_to_edit"]
            # v3.0: kein type-name Step mehr auf Edit. Typ ändert sich
            # eh nicht; Name ist auf der Entities-Seite editierbar.
            target = next(
                (d for d in self._devices
                 if d.get(CONF_DEVICE_ID) == self._edit_target_id),
                None,
            )
            if target is not None:
                self._edit_pending_type = target.get(CONF_DEVICE_TYPE)
                self._edit_pending_name = target.get(CONF_DEVICE_NAME)
            return await self.async_step_edit_device_entities()

        options = [
            selector.SelectOptionDict(
                value=d[CONF_DEVICE_ID],
                label=f"{d[CONF_DEVICE_NAME]} ({DEVICE_TYPE_LABELS_DE.get(d[CONF_DEVICE_TYPE], d[CONF_DEVICE_TYPE])})",
            )
            for d in self._devices
        ]
        return self.async_show_form(
            step_id="edit_device",
            data_schema=vol.Schema(
                {
                    vol.Required("device_to_edit"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=options,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
        )

    async def async_step_edit_device_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit-Step 2: type-specific entity mapping, pre-filled.

        Entity-Felder, die der neue Typ nicht mehr exponiert, fallen
        beim Speichern automatisch weg (siehe `_build_device_record`),
        damit kein stale-Mapping liegen bleibt.
        """
        target = next(
            (d for d in self._devices if d.get(CONF_DEVICE_ID) == self._edit_target_id),
            None,
        )
        if target is None:
            return await self.async_step_init()

        device_type = self._edit_pending_type or target[CONF_DEVICE_TYPE]
        device_name = self._edit_pending_name or target[CONF_DEVICE_NAME]

        if user_input is not None:
            # Device-name comes via this step in v3.0 — type-step is
            # skipped on edit (type-changes are rare and lossy), but
            # rename is common and belongs right next to the entity
            # mapping anyway.
            new_name = (
                user_input.pop(CONF_DEVICE_NAME, "")
                or device_name
            )
            self._edit_pending_name = new_name
            entity_input = _apply_climate_first(
                _flatten_sections(user_input),
                device_type=device_type,
            )
            # Hold-mode is invisible in the user-facing flow (every
            # device gets `always` since v1.20.0). Carry it forward
            # so dispatch never reasons about it.
            entity_input.setdefault(
                CONF_ENTITY_CONTROL_HOLD,
                target.get(CONF_ENTITY_CONTROL_HOLD, ENTITY_CONTROL_HOLD_AUTO),
            )
            # v3.4.8 — Edit-Flow überschreibt sonst CONF_DEVICE_CONFIG_MODE
            # auf MANUAL (Default in _build_device_record) weil der Mode
            # in der Edit-Flow nicht erneut abgefragt wird (config_mode-
            # Step ist bewusst geskippt). Vor v3.4.8 hat das jedes Save
            # einen CLIMATE-Mode auf MANUAL umgepatcht → nächster Edit
            # sah include_cooling=False → Cool-Wert war weg.
            entity_input.setdefault(
                CONF_DEVICE_CONFIG_MODE,
                target.get(CONF_DEVICE_CONFIG_MODE) or CONFIG_MODE_MANUAL,
            )
            # value_on / value_off are intentionally NOT carried
            # forward. Pre-fix the carry caused the dispatcher's
            # "step already filled → skip" logic to bypass the
            # values step on every edit, even when the user wanted
            # to adjust the mapping. The values step's
            # `_values_schema(defaults=target)` pre-fills the input
            # fields from the stored device record, so leaving the
            # keys absent shows the step with the existing values
            # already typed in — best of both worlds.
            return await self._dispatch_edit_post_entities(target, entity_input)

        return self.async_show_form(
            step_id="edit_device_entities",
            data_schema=_entities_schema(
                device_type,
                defaults={**target, CONF_DEVICE_NAME: device_name},
                config_mode=target.get(CONF_DEVICE_CONFIG_MODE) or CONFIG_MODE_MANUAL,
                include_name=True,
            ),
            description_placeholders={
                "device_type": DEVICE_TYPE_LABELS_DE.get(device_type, device_type),
                "device_name": device_name,
            },
        )

    async def _dispatch_edit_post_entities(
        self, target: dict[str, Any], entity_input: dict[str, Any]
    ) -> ConfigFlowResult:
        """Edit-flow dispatcher mirrors `_dispatch_post_entities` /
        `_dispatch_add_post_entities`: charge-mode-values →
        vehicle-status → values → shares-hardware → save. Values
        runs before shares-hardware because it's entity-level
        config. Same skip logic — when the relevant key is already
        populated the step is silently skipped, so users only see
        the step when they actually need to fill it in."""
        device_type = self._edit_pending_type or target[CONF_DEVICE_TYPE]

        if (
            device_type == "wallbox"
            and entity_input.get(CONF_ENTITY_CHARGE_MODE)
            and CONF_CHARGE_MODE_VALUE_LOCK not in entity_input
        ):
            self._edit_pending_entity_input = entity_input
            return await self.async_step_edit_device_charge_mode_values()

        if (
            device_type == "battery"
            and entity_input.get(CONF_ENTITY_CHARGE_MODE)
            and CONF_ENTITY_BATTERY_MODE not in entity_input
        ):
            self._edit_pending_entity_input = entity_input
            return await self.async_step_edit_device_battery_values()

        if (
            device_type == "wallbox"
            and entity_input.get(CONF_ENTITY_VEHICLE_STATUS)
            and CONF_VEHICLE_STATUS_VALUE_PLUGGED not in entity_input
        ):
            _auto_fill_binary_vehicle_status(entity_input)
            if CONF_VEHICLE_STATUS_VALUE_PLUGGED not in entity_input:
                self._edit_pending_entity_input = entity_input
                return await self.async_step_edit_device_vehicle_status()

        entity_control = entity_input.get(CONF_ENTITY_CONTROL, "")
        needs_values = (
            device_type in _CONTROLLABLE_TYPES
            and entity_control
            and not _is_binary_entity(entity_control)
            # Re-entry guard: after the values step submits, it adds
            # CONF_VALUE_ON to entity_input, so subsequent dispatches
            # skip the step. The values step's defaults pre-fill from
            # `target` on first show.
            and CONF_VALUE_ON not in entity_input
        )
        if needs_values:
            self._edit_pending_entity_input = entity_input
            return await self.async_step_edit_device_values()

        if (
            device_type == "warmwater"
            and CONF_SHARES_HARDWARE_WITH not in entity_input
        ):
            self._edit_pending_entity_input = entity_input
            return await self.async_step_edit_device_shares_hardware()

        # v3.0: legacy cooling step entfernt — inline erfasst;
        # Haushalt-Flag seit v3.26 komplett raus (parent_device_id-
        # Baum, App-konfiguriert).
        return await self._edit_save(target, entity_input)

    async def async_step_edit_device_charge_mode_values(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit-flow variant of the wallbox Lademodus-Werte mapping
        (Aus / An / Solaroptimiert → wallbox HA select-options)."""
        target = next(
            (d for d in self._devices if d.get(CONF_DEVICE_ID) == self._edit_target_id),
            None,
        )
        if target is None:
            return await self.async_step_init()
        device_type = self._edit_pending_type or target[CONF_DEVICE_TYPE]
        device_name = self._edit_pending_name or target[CONF_DEVICE_NAME]
        entity_input = dict(self._edit_pending_entity_input or {})
        entity_charge_mode = entity_input.get(CONF_ENTITY_CHARGE_MODE, "")

        if user_input is not None:
            entity_input[CONF_CHARGE_MODE_VALUE_LOCK] = user_input.get(
                CONF_CHARGE_MODE_VALUE_LOCK, ""
            )
            entity_input[CONF_CHARGE_MODE_VALUE_POWER] = user_input.get(
                CONF_CHARGE_MODE_VALUE_POWER, ""
            )
            entity_input[CONF_CHARGE_MODE_VALUE_SOLAR] = user_input.get(
                CONF_CHARGE_MODE_VALUE_SOLAR, ""
            )
            entity_input[CONF_ENTITY_CONTROL_HOLD] = user_input.get(
                CONF_ENTITY_CONTROL_HOLD, ENTITY_CONTROL_HOLD_AUTO
            )
            return await self._dispatch_edit_post_entities(target, entity_input)

        return self.async_show_form(
            step_id="edit_device_charge_mode_values",
            data_schema=_charge_mode_values_schema(
                self.hass, entity_charge_mode, target
            ),
            description_placeholders={
                "device_type": DEVICE_TYPE_LABELS_DE.get(device_type, device_type),
                "device_name": device_name,
                "entity_charge_mode": entity_charge_mode,
            },
        )

    async def async_step_edit_device_battery_values(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """v3.8.0 Edit-flow variant: Battery-Setpoint-Dispatch."""
        target = next(
            (d for d in self._devices if d.get(CONF_DEVICE_ID) == self._edit_target_id),
            None,
        )
        if target is None:
            return await self.async_step_init()
        device_type = self._edit_pending_type or target[CONF_DEVICE_TYPE]
        device_name = self._edit_pending_name or target[CONF_DEVICE_NAME]
        entity_input = dict(self._edit_pending_entity_input or {})

        if user_input is not None:
            for key in (
                CONF_ENTITY_BATTERY_MODE,
                CONF_VALUE_BATTERY_MODE_ACTIVE,
                CONF_VALUE_BATTERY_MODE_PASSIVE,
                CONF_ENTITY_BATTERY_POWER_SETPOINT,
            ):
                entity_input[key] = user_input.get(key, "")
            entity_input[CONF_BATTERY_SETPOINT_INVERT_SIGN] = bool(
                user_input.get(CONF_BATTERY_SETPOINT_INVERT_SIGN, False)
            )
            entity_input[CONF_ENTITY_CONTROL_HOLD] = user_input.get(
                CONF_ENTITY_CONTROL_HOLD, ENTITY_CONTROL_HOLD_AUTO
            )
            return await self._dispatch_edit_post_entities(target, entity_input)

        return self.async_show_form(
            step_id="edit_device_battery_values",
            data_schema=_battery_values_schema(self.hass, target),
            description_placeholders={
                "device_type": DEVICE_TYPE_LABELS_DE.get(device_type, device_type),
                "device_name": device_name,
            },
        )

    async def async_step_edit_device_vehicle_status(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit-flow variant of the wallbox vehicle-status mapping."""
        target = next(
            (d for d in self._devices if d.get(CONF_DEVICE_ID) == self._edit_target_id),
            None,
        )
        if target is None:
            return await self.async_step_init()
        device_type = self._edit_pending_type or target[CONF_DEVICE_TYPE]
        device_name = self._edit_pending_name or target[CONF_DEVICE_NAME]
        entity_input = dict(self._edit_pending_entity_input or {})
        entity_vehicle_status = entity_input.get(CONF_ENTITY_VEHICLE_STATUS, "")

        if user_input is not None:
            entity_input[CONF_VEHICLE_STATUS_VALUE_PLUGGED] = user_input.get(
                CONF_VEHICLE_STATUS_VALUE_PLUGGED, ""
            )
            entity_input[CONF_VEHICLE_STATUS_VALUE_UNPLUGGED] = user_input.get(
                CONF_VEHICLE_STATUS_VALUE_UNPLUGGED, ""
            )
            entity_input[CONF_VEHICLE_STATUS_VALUE_ERROR] = user_input.get(
                CONF_VEHICLE_STATUS_VALUE_ERROR, ""
            )
            return await self._dispatch_edit_post_entities(target, entity_input)

        return self.async_show_form(
            step_id="edit_device_vehicle_status",
            data_schema=_vehicle_status_schema(
                self.hass, entity_vehicle_status, target
            ),
            description_placeholders={
                "device_type": DEVICE_TYPE_LABELS_DE.get(device_type, device_type),
                "device_name": device_name,
                "entity_vehicle_status": entity_vehicle_status,
            },
        )

    async def async_step_edit_device_shares_hardware(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit-flow variant of the warmwater shares-hardware picker.
        Only the user's OTHER heating devices appear as candidates —
        a device can't share hardware with itself."""
        target = next(
            (d for d in self._devices if d.get(CONF_DEVICE_ID) == self._edit_target_id),
            None,
        )
        if target is None:
            return await self.async_step_init()
        device_type = self._edit_pending_type or target[CONF_DEVICE_TYPE]
        device_name = self._edit_pending_name or target[CONF_DEVICE_NAME]
        entity_input = dict(self._edit_pending_entity_input or {})

        heating_devices = [
            {"id": d.get(CONF_DEVICE_ID, ""), "name": d.get(CONF_DEVICE_NAME, "")}
            for d in self._devices
            if d.get(CONF_DEVICE_TYPE) == "heating"
                and d.get(CONF_DEVICE_ID) != self._edit_target_id
        ]

        if user_input is not None:
            entity_input[CONF_SHARES_HARDWARE_WITH] = user_input.get(
                CONF_SHARES_HARDWARE_WITH, ""
            )
            return await self._dispatch_edit_post_entities(target, entity_input)

        return self.async_show_form(
            step_id="edit_device_shares_hardware",
            data_schema=_shares_hardware_schema(heating_devices, target),
            description_placeholders={
                "device_type": DEVICE_TYPE_LABELS_DE.get(device_type, device_type),
                "device_name": device_name,
            },
        )

    async def async_step_edit_device_values(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit-Step 3: typ-bewusste value_on / value_off."""
        target = next(
            (d for d in self._devices if d.get(CONF_DEVICE_ID) == self._edit_target_id),
            None,
        )
        if target is None:
            return await self.async_step_init()

        device_type = self._edit_pending_type or target[CONF_DEVICE_TYPE]
        device_name = self._edit_pending_name or target[CONF_DEVICE_NAME]
        entity_input = dict(self._edit_pending_entity_input or {})
        entity_control = entity_input.get(CONF_ENTITY_CONTROL, "")
        config_mode = (
            target.get(CONF_DEVICE_CONFIG_MODE) or CONFIG_MODE_MANUAL
        )
        # v3.4.7 + v3.4.8 Legacy-/Reparatur-Auto-Migration: zwei Fälle
        # sind hier dasselbe Symptom:
        #   1) pre-v3.0-Entries hatten kein CONF_DEVICE_CONFIG_MODE
        #      gespeichert → falsy
        #   2) post-v3.0 Bug bis v3.4.7: Edit-Save überschrieb MODE auf
        #      "manual" weil entity_input das Feld nicht trug → truthy
        #      aber semantisch falsch
        # In beiden Fällen: wenn entity_control auf climate.* zeigt, ist
        # das Device tatsächlich climate-mode → cool-Feld muss in den
        # Values-Step rein. _entities_schema macht die identische
        # Migration für den Entity-Step (line 442-461).
        if config_mode != CONFIG_MODE_CLIMATE:
            legacy_ctrl = entity_control or target.get(CONF_ENTITY_CONTROL, "")
            if isinstance(legacy_ctrl, str) and legacy_ctrl.startswith(
                "climate."
            ):
                config_mode = CONFIG_MODE_CLIMATE
        # aircon: cooling immer aktiv. heating: nur im Climate-Mode.
        include_cooling = device_type == "aircon" or (
            device_type == "heating" and config_mode == CONFIG_MODE_CLIMATE
        )

        if user_input is not None:
            entity_input[CONF_VALUE_ON] = (
                user_input.get(CONF_VALUE_ON)
                or target.get(CONF_VALUE_ON, "")
            )
            entity_input[CONF_VALUE_OFF] = (
                user_input.get(CONF_VALUE_OFF)
                or target.get(CONF_VALUE_OFF, "")
            )
            if include_cooling:
                entity_input[CONF_VALUE_COOL_ON] = (
                    user_input.get(CONF_VALUE_COOL_ON)
                    or target.get(CONF_VALUE_COOL_ON, "")
                )
            # v3.4.9: stille Resets auf der Cool-Familie verhindern.
            # `value_cool_off` + `entity_cool_control` werden vom Form
            # nie abgefragt (climate.set_hvac_mode reicht für die meisten
            # Setups), aber für SG-Ready / Legacy-v2.x mit explizitem
            # Cool-Pfad muss der gespeicherte Wert beim Edit erhalten
            # bleiben. Ohne carry-forward würde `_build_device_record`
            # die Felder auf `""` zurücksetzen → falscher Hold-Loop-Wert.
            entity_input.setdefault(
                CONF_VALUE_COOL_OFF,
                target.get(CONF_VALUE_COOL_OFF, ""),
            )
            entity_input.setdefault(
                CONF_ENTITY_COOL_CONTROL,
                target.get(CONF_ENTITY_COOL_CONTROL, ""),
            )
            entity_input[CONF_ENTITY_CONTROL_HOLD] = user_input.get(
                CONF_ENTITY_CONTROL_HOLD,
                target.get(CONF_ENTITY_CONTROL_HOLD, ENTITY_CONTROL_HOLD_AUTO),
            )
            return await self._dispatch_edit_post_entities(target, entity_input)

        # Pre-fill from `target` (the stored device record) so the
        # user sees their existing mapping when they open the edit
        # flow. Earlier code passed `entity_input` here, but that
        # dict deliberately doesn't carry value_on/value_off (the
        # v2.2.2 fix in async_step_edit_device_entities strips them
        # so the dispatcher's "skip-when-filled" guard always re-runs
        # this step). Result: form showed empty, user re-typed or hit
        # Next, values got cleared.
        return self.async_show_form(
            step_id="edit_device_values",
            data_schema=_values_schema(
                self.hass, entity_control, target,
                include_cooling=include_cooling,
                cooling_first=device_type == "aircon",
            ),
            description_placeholders={
                "device_type": DEVICE_TYPE_LABELS_DE.get(device_type, device_type),
                "device_name": device_name,
                "entity_control": entity_control,
            },
        )

    async def _edit_save(
        self, target: dict[str, Any], entity_input: dict[str, Any]
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        device_type = self._edit_pending_type or target[CONF_DEVICE_TYPE]
        device_name = self._edit_pending_name or target[CONF_DEVICE_NAME]
        try:
            await _update_device_backend(
                self.hass,
                self._entry,
                target[CONF_DEVICE_ID],
                device_type,
                device_name,
                entity_input,
            )
            updated = _build_device_record(
                target[CONF_DEVICE_ID], device_type, device_name, entity_input
            )
            await self._persist_devices([
                updated if d[CONF_DEVICE_ID] == target[CONF_DEVICE_ID] else d
                for d in self._entry.data.get(CONF_DEVICES, [])
            ])
            self._edit_target_id = None
            self._edit_pending_type = None
            self._edit_pending_name = None
            self._edit_pending_entity_input = None
            return await self.async_step_init()
        except (httpx.HTTPStatusError, httpx.RequestError) as err:
            _LOGGER.error("Failed to update device: %s", err)
            errors["base"] = "cannot_connect"
            return self.async_show_form(
                step_id="edit_device_entities",
                data_schema=_entities_schema(device_type, defaults=target, config_mode=target.get(CONF_DEVICE_CONFIG_MODE) or CONFIG_MODE_MANUAL),
                description_placeholders={
                    "device_type": DEVICE_TYPE_LABELS_DE.get(device_type, device_type),
                    "device_name": device_name,
                },
                errors=errors,
            )

    # ── Remove ──────────────────────────────────────────────────────────────

    async def async_step_remove_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            device_id = user_input["device_to_remove"]
            try:
                response = await _authenticated_config_request(
                    self.hass,
                    self._entry,
                    "DELETE",
                    f"/api/v1/devices/{device_id}",
                )
                # 404 is a benign "already gone" — treat as success
                # so the user can clean up an orphaned HA-side device
                # even after the backend row vanished (e.g. test
                # cleanup or another client deleted it concurrently).
                if response.status_code != 404:
                    response.raise_for_status()
            except (httpx.HTTPStatusError, httpx.RequestError) as err:
                _LOGGER.error("Failed to delete device: %s", err)
                errors["base"] = "cannot_connect"

            if not errors:
                _remove_ha_device(self.hass, device_id)
                await self._persist_devices([
                    d for d in self._entry.data.get(CONF_DEVICES, [])
                    if d.get(CONF_DEVICE_ID) != device_id
                ])
                return await self.async_step_init()

        if not self._devices:
            return await self.async_step_init()

        options = [
            selector.SelectOptionDict(
                value=d[CONF_DEVICE_ID],
                label=f"{d[CONF_DEVICE_NAME]} ({DEVICE_TYPE_LABELS_DE.get(d[CONF_DEVICE_TYPE], d[CONF_DEVICE_TYPE])})",
            )
            for d in self._devices
        ]

        return self.async_show_form(
            step_id="remove_device",
            data_schema=vol.Schema(
                {
                    vol.Required("device_to_remove"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=options,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_done(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        # CN-7 (2026-06-11): reiner Menü-Exit. Persistenz + Reload
        # passieren seit CN-7 unmittelbar nach jeder Add/Edit/Remove-
        # Operation (`_persist_devices`) — der frühere Snapshot-
        # Writeback hier hat zwischenzeitliche Änderungen anderer
        # Pfade überschrieben (Lost-Update).
        return self.async_create_entry(title="", data={})

