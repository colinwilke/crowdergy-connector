"""Tests für #18 (Telemetry-PATCH Retry/Backoff) + #19 (proaktives
Token-Refresh) — beide neu in coordinator.py.

#18: `_patch_telemetry_with_retry` retried transiente Fehler (Transport +
5xx/429) bounded mit Backoff, lässt permanente 4xx (inkl. 404/410) sofort
durch und repliziert das Vor-#18-Terminal-Verhalten nach erschöpften
Retries (letzte Response zurück / letzten RequestError re-raise).

#19: `_jwt_exp` liest den exp-Claim ohne Verifikation; `_token_expires_
within` + `_maybe_proactive_refresh` erneuern das Token VOR Ablauf
(single-flight via seen_token-CAS), best-effort. `_authenticated_request`
ruft den proaktiven Check vor jedem Call.
"""
from __future__ import annotations

import base64
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from custom_components.theothergas import coordinator as coord_mod
from custom_components.theothergas.coordinator import (
    PROACTIVE_REFRESH_MARGIN_S,
    TELEMETRY_RETRY_ATTEMPTS,
    CrowdergyCoordinator,
    _jwt_exp,
)


# ── Helpers ──────────────────────────────────────────────────────────


def _bare_coord() -> CrowdergyCoordinator:
    """Coordinator-Skelett ohne __init__ (Pattern aus test_telemetry_
    composer). Nur die Felder die die getesteten Pfade berühren."""
    coord = CrowdergyCoordinator.__new__(CrowdergyCoordinator)
    coord._access_token = "tok-initial"
    coord._refresh_token = "refresh-initial"
    coord._access_token_exp = None
    coord._connector_version = "9.9.9"
    return coord


def _response(status: int, payload=None) -> httpx.Response:
    return httpx.Response(
        status,
        json=payload if payload is not None else {},
        request=httpx.Request("PATCH", "https://api.example/x"),
    )


def _make_jwt(exp) -> str:
    """Baut ein (unsigniertes) JWT mit optionalem exp-Claim."""
    def b64(d: dict) -> str:
        return (
            base64.urlsafe_b64encode(json.dumps(d).encode())
            .rstrip(b"=")
            .decode()
        )

    header = {"alg": "HS256", "typ": "JWT"}
    claims: dict = {} if exp is None else {"exp": exp}
    return f"{b64(header)}.{b64(claims)}.signaturepart"


# ── #18: Telemetry-PATCH Retry/Backoff ───────────────────────────────


async def test_patch_retry_transport_error_then_success():
    coord = _bare_coord()
    coord._authenticated_request = AsyncMock(
        side_effect=[httpx.ConnectError("boom"), _response(200)]
    )
    with patch.object(coord_mod.asyncio, "sleep", new=AsyncMock()) as slept:
        resp = await coord._patch_telemetry_with_retry("d1", {"power_kw": 1.0})

    assert resp.status_code == 200
    assert coord._authenticated_request.await_count == 2
    slept.assert_awaited_once()  # genau ein Backoff zwischen den Versuchen
    method, path = coord._authenticated_request.await_args.args
    assert (method, path) == ("PATCH", "/api/v1/devices/d1/telemetry")
    assert coord._authenticated_request.await_args.kwargs["json"] == {
        "power_kw": 1.0
    }


async def test_patch_retry_transient_5xx_then_success():
    coord = _bare_coord()
    coord._authenticated_request = AsyncMock(
        side_effect=[_response(503), _response(200)]
    )
    with patch.object(coord_mod.asyncio, "sleep", new=AsyncMock()):
        resp = await coord._patch_telemetry_with_retry("d1", {})

    assert resp.status_code == 200
    assert coord._authenticated_request.await_count == 2


async def test_patch_no_retry_on_permanent_4xx():
    """404/410 (Device backend-seitig weg) ist permanent → genau EIN
    Versuch, die Response geht unverändert an den Caller (der evictet)."""
    coord = _bare_coord()
    coord._authenticated_request = AsyncMock(return_value=_response(404))
    with patch.object(coord_mod.asyncio, "sleep", new=AsyncMock()) as slept:
        resp = await coord._patch_telemetry_with_retry("d1", {})

    assert resp.status_code == 404
    assert coord._authenticated_request.await_count == 1
    slept.assert_not_awaited()


async def test_patch_exhausts_retries_and_reraises_transport_error():
    coord = _bare_coord()
    coord._authenticated_request = AsyncMock(
        side_effect=httpx.ConnectError("down")
    )
    with patch.object(coord_mod.asyncio, "sleep", new=AsyncMock()):
        with pytest.raises(httpx.RequestError):
            await coord._patch_telemetry_with_retry("d1", {})

    assert coord._authenticated_request.await_count == TELEMETRY_RETRY_ATTEMPTS


async def test_patch_exhausts_retries_returns_last_transient_response():
    """Dauerhaftes 5xx → nach erschöpften Retries kommt die letzte
    (transiente) Response zurück; der Caller loggt / raise_for_status."""
    coord = _bare_coord()
    coord._authenticated_request = AsyncMock(return_value=_response(503))
    with patch.object(coord_mod.asyncio, "sleep", new=AsyncMock()):
        resp = await coord._patch_telemetry_with_retry("d1", {})

    assert resp.status_code == 503
    assert coord._authenticated_request.await_count == TELEMETRY_RETRY_ATTEMPTS


# ── #19: JWT-exp + proaktives Refresh ────────────────────────────────


def test_jwt_exp_roundtrip():
    assert _jwt_exp(_make_jwt(1_750_000_000)) == 1_750_000_000.0


def test_jwt_exp_returns_none_on_garbage():
    assert _jwt_exp("not-a-jwt") is None
    assert _jwt_exp("only.two") is None
    assert _jwt_exp(_make_jwt(None)) is None  # exp-Claim fehlt


def test_token_expires_within():
    coord = _bare_coord()
    now = time.time()

    coord._access_token_exp = now + 30.0
    assert coord._token_expires_within(120.0) is True
    assert coord._token_expires_within(10.0) is False

    coord._access_token_exp = now + 1000.0
    assert coord._token_expires_within(120.0) is False

    coord._access_token_exp = None  # nicht dekodierbar → reaktiver Pfad
    assert coord._token_expires_within(120.0) is False


async def test_maybe_proactive_refresh_fires_near_expiry():
    coord = _bare_coord()
    coord._access_token = "soon-to-expire"
    coord._access_token_exp = time.time() + 5.0  # < Margin
    coord._refresh_access_token = AsyncMock(return_value=True)

    await coord._maybe_proactive_refresh()

    coord._refresh_access_token.assert_awaited_once_with(
        seen_token="soon-to-expire"
    )


async def test_maybe_proactive_refresh_noop_when_far_from_expiry():
    coord = _bare_coord()
    coord._access_token_exp = time.time() + PROACTIVE_REFRESH_MARGIN_S + 600.0
    coord._refresh_access_token = AsyncMock(return_value=True)

    await coord._maybe_proactive_refresh()

    coord._refresh_access_token.assert_not_awaited()


async def test_maybe_proactive_refresh_swallows_failure():
    """Schlägt der proaktive Refresh fehl (Backend kurz weg), darf der
    Caller nicht crashen — er läuft mit dem aktuellen Token weiter."""
    coord = _bare_coord()
    coord._access_token_exp = time.time() + 5.0
    coord._refresh_access_token = AsyncMock(
        side_effect=httpx.ConnectError("down")
    )

    await coord._maybe_proactive_refresh()  # darf NICHT raisen


async def test_authenticated_request_invokes_proactive_refresh():
    """`_authenticated_request` triggert den proaktiven Check vor dem
    eigentlichen Call (200 → kein reaktiver 401-Pfad)."""
    coord = _bare_coord()
    coord._maybe_proactive_refresh = AsyncMock()
    coord._client = MagicMock()
    coord._client.request = AsyncMock(return_value=_response(200))

    resp = await coord._authenticated_request("GET", "/api/v1/devices")

    assert resp.status_code == 200
    coord._maybe_proactive_refresh.assert_awaited_once()
    coord._client.request.assert_awaited_once()
