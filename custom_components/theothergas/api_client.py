"""Shared backend auth/session layer (Connector-Arch 2026-06-12).

DER eine Auth-Pfad zum Crowdergy-Backend. Vorher gab es drei parallele
Implementierungen (config_flow `_refresh_token` +
`_authenticated_config_request`, Coordinator `_refresh_access_token` +
`_authenticated_request`, box_services über die config_flow-Helper) —
mit getrennten Token-Kopien und getrennten (bzw. fehlenden) Locks gegen
ein SINGLE-USE-Refresh-Token: ein 401, der zwei Pfade gleichzeitig
traf, konnte eine Logout-Kaskade auslösen. Jetzt gilt:

* `CrowdergyAuthSession` kapselt Token-Paar, Single-Flight-Refresh mit
  Compare-and-Swap (Cluster-A-Semantik 1:1 aus dem Coordinator
  portiert) und den 401-retry-once-Request.
* `authenticated_request(hass, entry, ...)` ist der Einstieg für
  Config-/Options-Flow und Box-Services. Läuft der Coordinator des
  Entries bereits, wird DESSEN Session wiederverwendet — damit
  konkurrieren Options-Flow und Coordinator nie mehr um dasselbe
  Refresh-Token.
* `claim_pairing_code(...)` tauscht einen Pairing-Code gegen ein
  JWT-Paar (kanonisch `POST /api/v1/connector/claim`, Fallback auf den
  Alt-Pfad `/api/v1/box/claim` für ältere Backends).

Regel (CLAUDE.md): NIE eine zweite Refresh-Implementierung anlegen —
neue Backend-Calls gehen durch `CrowdergyAuthSession.async_request`
bzw. `authenticated_request`.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import httpx
from homeassistant.core import HomeAssistant

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_API_URL,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class CannotConnect(Exception):
    """Backend nicht erreichbar oder unerwartete Antwort."""


class InvalidPairingCode(Exception):
    """Pairing-Code unbekannt, abgelaufen oder bereits verbraucht —
    das Backend antwortet bewusst mit EINEM generischen 404 für alle
    drei Fälle (kein Brute-Force-Orakel)."""


class CrowdergyAuthSession:
    """Token-Paar + authentifizierte Requests gegen das Backend.

    `refresh_token=None` = Pre-Entry-Modus (Initial-Flow, CN-12): der
    Access-Token ist Sekunden alt, ein Refresh würde das Paar rotieren
    ohne dass es irgendwo persistiert werden könnte (Backend
    invalidiert per Use) — daher dort kein 401-Retry.

    Rotierte Tokens werden über `on_tokens_rotated(access, refresh)`
    nach außen gemeldet (Entry-gebundene Caller persistieren sie via
    `async_update_entry`).
    """

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        api_url: str,
        access_token: str,
        refresh_token: str | None = None,
        on_tokens_rotated: Callable[[str, str], None] | None = None,
        connector_version: str | None = None,
    ) -> None:
        self._hass = hass
        self._api_url = api_url.rstrip("/")
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._on_tokens_rotated = on_tokens_rotated
        # Mutable — der Coordinator lädt die Manifest-Version deferred
        # in `async_init()` und setzt sie hier nach.
        self.connector_version: str | None = connector_version
        # Client-Bau ist blocking I/O (synchroner CA-Load,
        # load_verify_locations) — lazy im Executor, nie im Event-Loop
        # (v3.5.1-Fix, gilt hier weiter).
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()
        # Cluster A Connector (2026-06-09), unverändert übernommen:
        # Single-Flight-Lock + CAS gegen parallele 401s. Das Backend
        # invalidiert das Refresh-Token per Use — ohne Lock gewinnt
        # nur ein Caller, der Rest löst eine Logout-Kaskade aus.
        self._refresh_lock = asyncio.Lock()

    @property
    def api_url(self) -> str:
        return self._api_url

    @property
    def access_token(self) -> str:
        return self._access_token

    @property
    def refresh_token(self) -> str | None:
        return self._refresh_token

    def _auth_headers(self) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._access_token}"}
        if self.connector_version:
            # Lets the backend stamp users.connector_version so iOS
            # can surface an "Update verfügbar" banner.
            headers["X-Crowdergy-Connector-Version"] = self.connector_version
        return headers

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            async with self._client_lock:
                if self._client is None:
                    self._client = await self._hass.async_add_executor_job(
                        lambda: httpx.AsyncClient(
                            base_url=self._api_url, timeout=15.0
                        )
                    )
        return self._client

    async def async_refresh_tokens(
        self, *, seen_token: str | None = None
    ) -> bool:
        """Single-flight Refresh mit Compare-and-Swap.

        `seen_token`: der Access-Token, den der Caller bei seinem 401
        gesehen hat. Hat ein anderer Caller während des Lock-Waits
        bereits rotiert (CAS missed), refreshen wir nicht nochmal —
        der Caller retryt sein Original-Request mit dem aktuellen
        Token.
        """
        if self._refresh_token is None:
            return False
        client = await self._ensure_client()
        async with self._refresh_lock:
            if seen_token is not None and self._access_token != seen_token:
                # Anderer Caller hat bereits rotiert — neues Token
                # kommentarlos übernehmen.
                return True
            try:
                response = await client.post(
                    "/api/v1/auth/refresh",
                    json={"refresh_token": self._refresh_token},
                )
                if response.status_code == 200:
                    tokens = response.json()
                    self._access_token = tokens["access_token"]
                    self._refresh_token = tokens["refresh_token"]
                    if self._on_tokens_rotated is not None:
                        self._on_tokens_rotated(
                            self._access_token, self._refresh_token
                        )
                    return True
                _LOGGER.warning(
                    "Token refresh returned %s", response.status_code
                )
            except httpx.RequestError as err:
                _LOGGER.error("Token refresh failed: %s", err)
            return False

    async def async_request(
        self, method: str, path: str, **kwargs: Any
    ) -> httpx.Response:
        """Authentifizierter Request mit 401 → Refresh → ein Retry."""
        client = await self._ensure_client()
        # Snapshot des aktuellen Tokens für CAS — wenn ein anderer
        # Caller während unseres 401-Roundtrips bereits rotiert,
        # lassen wir den nächsten Refresh sausen.
        seen_token = self._access_token
        response = await client.request(
            method, path, headers=self._auth_headers(), **kwargs
        )
        if response.status_code == 401 and self._refresh_token is not None:
            if await self.async_refresh_tokens(seen_token=seen_token):
                response = await client.request(
                    method, path, headers=self._auth_headers(), **kwargs
                )
        return response

    async def async_close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _entry_session(hass: HomeAssistant, entry: Any) -> CrowdergyAuthSession:
    """Transiente, Entry-gebundene Session (Config-/Options-Flow ohne
    laufenden Coordinator). Rotierte Tokens landen im Entry, damit der
    nächste Call vom neuen Paar startet."""

    def _persist(access: str, refresh: str) -> None:
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_ACCESS_TOKEN: access,
                CONF_REFRESH_TOKEN: refresh,
            },
        )

    return CrowdergyAuthSession(
        hass,
        api_url=entry.data[CONF_API_URL],
        access_token=entry.data[CONF_ACCESS_TOKEN],
        refresh_token=entry.data[CONF_REFRESH_TOKEN],
        on_tokens_rotated=_persist,
    )


async def authenticated_request(
    hass: HomeAssistant,
    entry: Any,
    method: str,
    path: str,
    *,
    api_url: str | None = None,
    access_token: str | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """Authentifizierter Backend-Call aus Config-/Options-Flow oder
    Box-Services.

    Drei Modi:
    * `entry=None` + explizite `api_url`/`access_token`: Pre-Entry
      (Initial-Flow, CN-12) — kein 401-Retry.
    * Entry mit laufendem Coordinator: dessen Session wird
      WIEDERVERWENDET (ein Token-Paar, ein Refresh-Lock — kein Race
      zwischen Options-Flow und Coordinator mehr).
    * Entry ohne Coordinator (z.B. Entry disabled): transiente
      Session, rotierte Tokens werden in den Entry persistiert.
    """
    if entry is None:
        if not api_url or not access_token:
            raise ValueError(
                "authenticated config request without entry needs "
                "explicit api_url + access_token"
            )
        session = CrowdergyAuthSession(
            hass, api_url=api_url, access_token=access_token
        )
        try:
            return await session.async_request(method, path, **kwargs)
        finally:
            await session.async_close()

    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    api = getattr(coordinator, "api", None)
    if api is not None:
        return await api.async_request(method, path, **kwargs)

    session = _entry_session(hass, entry)
    try:
        return await session.async_request(method, path, **kwargs)
    finally:
        await session.async_close()


async def claim_pairing_code(
    hass: HomeAssistant,
    api_url: str,
    code: str,
    client_id: str | None = None,
) -> dict[str, Any]:
    """Pairing-Code gegen ein JWT-Paar tauschen.

    Kanonisch `POST /api/v1/connector/claim` (Backend ≥ 2026-06-12);
    bei 404/405 EIN Fallback auf den Alt-Pfad `/api/v1/box/claim` mit
    dem Alt-Feldnamen `box_id` — damit funktioniert der Flow auch
    gegen ältere/self-hosted Backends, und das Deploy-Ordering
    Backend↔Connector ist nicht hart. Kostet bei ungültigem Code gegen
    ein neues Backend einen zweiten Request (beide 404 → derselbe
    `InvalidPairingCode`); das generische 404 deckt unbekannt/
    abgelaufen/verbraucht ab.
    """
    base = api_url.rstrip("/")
    payload: dict[str, str] = {"code": code}
    if client_id:
        payload["client_id"] = client_id
    client = await hass.async_add_executor_job(
        lambda: httpx.AsyncClient(timeout=15.0)
    )
    try:
        try:
            response = await client.post(
                f"{base}/api/v1/connector/claim", json=payload
            )
            if response.status_code in (404, 405):
                legacy: dict[str, str] = {"code": code}
                if client_id:
                    legacy["box_id"] = client_id
                response = await client.post(
                    f"{base}/api/v1/box/claim", json=legacy
                )
        except httpx.RequestError as err:
            raise CannotConnect(str(err)) from err
        if response.status_code == 404:
            raise InvalidPairingCode
        if response.status_code >= 400:
            raise CannotConnect(f"claim returned {response.status_code}")
        try:
            tokens = response.json()
        except ValueError as err:
            raise CannotConnect(f"claim returned invalid JSON: {err}") from err
        if not isinstance(tokens, dict) or "access_token" not in tokens:
            raise CannotConnect("claim response missing access_token")
        return tokens
    finally:
        await client.aclose()


async def fetch_account_email(
    hass: HomeAssistant, api_url: str, access_token: str
) -> str | None:
    """Best-effort `GET /users/me` für den Entry-Titel nach dem Claim
    (der Claim-Response trägt bewusst keine E-Mail). Fehler werden
    geschluckt — der Titel fällt dann auf die User-ID zurück."""
    client = await hass.async_add_executor_job(
        lambda: httpx.AsyncClient(timeout=15.0)
    )
    try:
        response = await client.get(
            f"{api_url.rstrip('/')}/api/v1/users/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if response.status_code == 200:
            email = response.json().get("email")
            if isinstance(email, str) and email:
                return email
    except (httpx.RequestError, ValueError):
        pass
    finally:
        await client.aclose()
    return None
