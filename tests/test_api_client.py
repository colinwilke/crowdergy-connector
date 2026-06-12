"""CrowdergyAuthSession + claim_pairing_code (Connector-Arch 2026-06-12).

Pinnt die Cluster-A-Semantik des geteilten Auth-Pfads: Single-Flight-
Refresh mit CAS, 401-retry-once, Pre-Entry-Modus ohne Refresh, Token-
Persistenz via Callback — und den Claim-Fallback auf den /box/*-Altpfad.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from homeassistant.core import HomeAssistant

from custom_components.theothergas.api_client import (
    CannotConnect,
    CrowdergyAuthSession,
    InvalidPairingCode,
    authenticated_request,
    claim_pairing_code,
)
from custom_components.theothergas.const import DOMAIN


def _response(status: int, payload: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status,
        json=payload or {},
        request=httpx.Request("GET", "https://api.example/x"),
    )


class _FakeBackendClient:
    """Minimaler httpx-Client-Ersatz: 401 für veraltete Tokens, 200
    sonst; /auth/refresh rotiert auf ein zählbares neues Paar."""

    def __init__(self) -> None:
        self.valid_token = "access-0"
        self.refresh_calls = 0
        self.request_calls = 0

    async def request(self, method, path, headers=None, **kwargs):
        self.request_calls += 1
        await asyncio.sleep(0)
        token = (headers or {}).get("Authorization", "").removeprefix("Bearer ")
        if token != self.valid_token:
            return _response(401)
        return _response(200, {"ok": True})

    async def post(self, path, json=None, **kwargs):
        assert path == "/api/v1/auth/refresh"
        self.refresh_calls += 1
        await asyncio.sleep(0)
        self.valid_token = f"access-{self.refresh_calls}"
        return _response(
            200,
            {
                "access_token": self.valid_token,
                "refresh_token": f"refresh-{self.refresh_calls}",
            },
        )

    async def aclose(self) -> None:
        pass


def _session(
    hass: HomeAssistant,
    backend: _FakeBackendClient,
    *,
    access_token: str = "stale",
    refresh_token: str | None = "refresh-0",
    on_tokens_rotated=None,
) -> CrowdergyAuthSession:
    session = CrowdergyAuthSession(
        hass,
        api_url="https://api.example",
        access_token=access_token,
        refresh_token=refresh_token,
        on_tokens_rotated=on_tokens_rotated,
    )
    # Lazy-Client-Bau kurzschließen — wir testen die Auth-Semantik,
    # nicht den Executor-Pfad.
    session._client = backend  # type: ignore[assignment]
    return session


async def test_parallel_401s_trigger_exactly_one_refresh(hass: HomeAssistant):
    """Cluster A: N parallele Requests mit abgelaufenem Token → genau
    EIN /auth/refresh (Single-Flight + CAS), alle Retries laufen mit
    dem neuen Token durch."""
    backend = _FakeBackendClient()
    session = _session(hass, backend)

    responses = await asyncio.gather(
        *[session.async_request("GET", "/api/v1/devices") for _ in range(5)]
    )

    assert backend.refresh_calls == 1
    assert [r.status_code for r in responses] == [200] * 5


async def test_cas_skips_refresh_when_token_already_rotated(
    hass: HomeAssistant,
):
    backend = _FakeBackendClient()
    backend.valid_token = "access-fresh"
    session = _session(hass, backend, access_token="access-fresh")

    # Caller sah noch das alte Token — die Session ist aber schon
    # weiter → kein Refresh-Call, nur "nimm das aktuelle Token".
    assert await session.async_refresh_tokens(seen_token="stale") is True
    assert backend.refresh_calls == 0


async def test_rotation_persists_via_callback(hass: HomeAssistant):
    backend = _FakeBackendClient()
    rotated: list[tuple[str, str]] = []
    session = _session(
        hass, backend, on_tokens_rotated=lambda a, r: rotated.append((a, r))
    )

    assert await session.async_refresh_tokens() is True
    assert rotated == [("access-1", "refresh-1")]
    assert session.access_token == "access-1"
    assert session.refresh_token == "refresh-1"


async def test_401_retries_exactly_once(hass: HomeAssistant):
    backend = _FakeBackendClient()
    session = _session(hass, backend)

    response = await session.async_request("GET", "/api/v1/devices")

    assert response.status_code == 200
    assert backend.request_calls == 2  # 401 + Retry
    assert backend.refresh_calls == 1


async def test_pre_entry_mode_never_refreshes(hass: HomeAssistant):
    """CN-12: ohne Refresh-Token (Initial-Flow) kein Refresh-Versuch —
    der 401 geht unverändert an den Caller."""
    backend = _FakeBackendClient()
    session = _session(hass, backend, refresh_token=None)

    response = await session.async_request("GET", "/api/v1/devices")

    assert response.status_code == 401
    assert backend.refresh_calls == 0
    assert backend.request_calls == 1


async def test_authenticated_request_reuses_coordinator_session(
    hass: HomeAssistant,
):
    """Läuft der Coordinator eines Entries, nutzt der Options-Flow-
    Pfad DESSEN Session (ein Token-Paar, ein Refresh-Lock)."""
    backend = _FakeBackendClient()
    backend.valid_token = "access-live"
    session = _session(hass, backend, access_token="access-live")
    entry = SimpleNamespace(entry_id="entry-1")
    hass.data.setdefault(DOMAIN, {})["entry-1"] = SimpleNamespace(api=session)

    response = await authenticated_request(
        hass, entry, "GET", "/api/v1/devices"
    )

    assert response.status_code == 200
    assert backend.request_calls == 1


async def test_authenticated_request_pre_entry_requires_credentials(
    hass: HomeAssistant,
):
    with pytest.raises(ValueError):
        await authenticated_request(hass, None, "GET", "/api/v1/devices")


# ── claim_pairing_code ──────────────────────────────────────────────


class _FakeClaimClient:
    def __init__(self, connector_status: int, box_status: int) -> None:
        self.connector_status = connector_status
        self.box_status = box_status
        self.calls: list[tuple[str, dict]] = []

    async def post(self, url, json=None, **kwargs):
        self.calls.append((url, json))
        if url.endswith("/api/v1/connector/claim"):
            status = self.connector_status
        else:
            status = self.box_status
        if status == 200:
            return _response(
                200,
                {
                    "access_token": "a",
                    "refresh_token": "r",
                    "user_id": "user-1",
                },
            )
        return _response(status)

    async def aclose(self) -> None:
        pass


def _patch_claim_client(fake: _FakeClaimClient):
    return patch(
        "custom_components.theothergas.api_client.httpx.AsyncClient",
        return_value=fake,
    )


async def test_claim_canonical_path_sends_client_id(hass: HomeAssistant):
    fake = _FakeClaimClient(connector_status=200, box_status=500)
    with _patch_claim_client(fake):
        tokens = await claim_pairing_code(
            hass, "https://api.example", "ABCD-2345", "ha-instance-1"
        )

    assert tokens["user_id"] == "user-1"
    assert fake.calls == [
        (
            "https://api.example/api/v1/connector/claim",
            {"code": "ABCD-2345", "client_id": "ha-instance-1"},
        )
    ]


async def test_claim_falls_back_to_box_path_with_box_id(hass: HomeAssistant):
    """Älteres Backend ohne /connector/*: 404 → EIN Fallback auf
    /box/claim mit dem Alt-Feldnamen `box_id` (extra=forbid drüben)."""
    fake = _FakeClaimClient(connector_status=404, box_status=200)
    with _patch_claim_client(fake):
        tokens = await claim_pairing_code(
            hass, "https://api.example", "ABCD-2345", "ha-instance-1"
        )

    assert tokens["access_token"] == "a"
    assert [u for u, _ in fake.calls] == [
        "https://api.example/api/v1/connector/claim",
        "https://api.example/api/v1/box/claim",
    ]
    assert fake.calls[1][1] == {"code": "ABCD-2345", "box_id": "ha-instance-1"}


async def test_claim_invalid_code_raises_after_both_paths_404(
    hass: HomeAssistant,
):
    fake = _FakeClaimClient(connector_status=404, box_status=404)
    with _patch_claim_client(fake), pytest.raises(InvalidPairingCode):
        await claim_pairing_code(
            hass, "https://api.example", "AAAA-AAAA", None
        )
    assert len(fake.calls) == 2


async def test_claim_server_error_raises_cannot_connect(hass: HomeAssistant):
    fake = _FakeClaimClient(connector_status=500, box_status=500)
    with _patch_claim_client(fake), pytest.raises(CannotConnect):
        await claim_pairing_code(
            hass, "https://api.example", "ABCD-2345", None
        )
    # 500 ist KEIN Fallback-Trigger (nur 404/405 = Route fehlt).
    assert len(fake.calls) == 1
