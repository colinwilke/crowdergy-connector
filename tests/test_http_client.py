"""httpx clients are built in the executor, never in the event loop.

`httpx.AsyncClient(...)` loads the CA bundle synchronously; HA reports
that as a blocking call when it happens inside the loop.
"""
from __future__ import annotations

import threading
from unittest.mock import patch

import httpx
from homeassistant.core import HomeAssistant

from custom_components.theothergas import config_flow, entity_mapper
from custom_components.theothergas.http_client import async_create_client


async def test_client_is_constructed_off_the_event_loop(hass: HomeAssistant):
    loop_thread = threading.get_ident()
    built_in: list[int] = []
    real = httpx.AsyncClient

    def _spy(**kwargs):
        built_in.append(threading.get_ident())
        return real(**kwargs)

    with patch.object(httpx, "AsyncClient", _spy):
        client = await async_create_client(hass, timeout=3.0)
    try:
        assert built_in and built_in[0] != loop_thread
        assert client.timeout.connect == 3.0
    finally:
        await client.aclose()


async def test_config_flow_and_mapper_use_the_helper(hass: HomeAssistant):
    """Every short-lived client in the flow/mapper goes through the
    helper — a direct `httpx.AsyncClient(` would be back in the loop."""
    import inspect

    for module in (config_flow, entity_mapper):
        source = inspect.getsource(module)
        assert "httpx.AsyncClient(" not in source, module.__name__
        assert "async_create_client(" in source, module.__name__


async def test_fetch_account_email_builds_client_via_helper(
    hass: HomeAssistant,
):
    calls: list[dict] = []

    async def _fake(hass_arg, **kwargs):
        calls.append(kwargs)
        raise httpx.ConnectError("offline")

    with patch.object(config_flow, "async_create_client", _fake):
        email = await config_flow._fetch_account_email(
            hass, "https://api.example", "tok",
        )

    assert email is None
    assert calls == [{"timeout": 10.0}]
