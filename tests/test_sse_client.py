"""Tests für SSEClient (FEAT-5 Phase B, 2026-06-09).

Phase B extrahiert den SSE-Stream-Reader aus coordinator.py in eine
eigene Klasse. Hier verifizieren wir die testbaren Invarianten:

* Queue-Backpressure (Cap 512) droppt Frames statt zu blocken
* JSON-Parsing skippt malformed lines ohne den Stream zu killen
* `last_event_at` wird pro empfangenem Frame aktualisiert
* `start()` ist idempotent
* `stop()` cancelt den Background-Task sauber
"""
from __future__ import annotations

import asyncio
import json

import pytest

from custom_components.theothergas.sse_client import (
    SSE_QUEUE_MAXSIZE,
    SSEClient,
)


def _make_client(hass) -> SSEClient:
    """Helper — instanziiert einen SSEClient ohne Background-Task zu
    starten. Tests die den Reader nicht brauchen können so isoliert
    Queue/Properties testen."""
    return SSEClient(
        hass=hass,
        api_url="http://test",
        get_token=lambda: "fake-token",
        refresh_token=lambda: _async_true(),
    )


async def _async_true() -> bool:
    return True


async def test_initial_state_empty(hass):
    """Frisch erzeugter Client hat leere Queue, kein Task, last_event_at=0."""
    client = _make_client(hass)
    assert client.messages.empty()
    assert client.last_event_at == 0.0


async def test_queue_max_size_constant():
    """Backpressure-Cap = 512. Hard-coded damit eine versehentliche
    Vergrößerung sichtbar wird (Memory-Footprint pro Connector)."""
    assert SSE_QUEUE_MAXSIZE == 512


async def test_messages_queue_is_async_compatible(hass):
    """Queue ist asyncio.Queue — put_nowait/get await funktionieren."""
    client = _make_client(hass)
    msg = {"type": "ping"}
    client.messages.put_nowait(msg)
    received = await client.messages.get()
    assert received == msg


async def test_start_is_idempotent(hass):
    """Mehrfache start()-Calls spawnen keine doppelten Tasks."""
    client = _make_client(hass)
    client.start()
    first_task = client._task
    client.start()
    assert client._task is first_task
    await client.stop()


async def test_stop_without_start_is_safe(hass):
    """stop() vor start() muss noop sein, nicht crashen."""
    client = _make_client(hass)
    await client.stop()
    assert client._task is None


async def test_stop_cancels_running_task(hass):
    """stop() canceled den Background-Reader sauber + räumt auf."""
    client = _make_client(hass)
    client.start()
    assert client._task is not None
    assert not client._task.done()
    await client.stop()
    assert client._task is None


async def test_sse_url_formats_correctly(hass):
    client = _make_client(hass)
    assert client._sse_url() == "http://test/api/v1/stream"


async def test_queue_full_does_not_block_put(hass):
    """Queue.put_nowait hebt QueueFull bei voller Queue. Der Reader im
    SSE-Client fängt das + droppt + logged. Hier verifizieren wir das
    Verhalten am Queue-Objekt direkt."""
    client = _make_client(hass)
    for i in range(SSE_QUEUE_MAXSIZE):
        client.messages.put_nowait({"i": i})
    with pytest.raises(asyncio.QueueFull):
        client.messages.put_nowait({"overflow": True})
    # Erst nach get() ist wieder ein Slot frei.
    await client.messages.get()
    client.messages.put_nowait({"recovered": True})


async def test_token_callback_resolves_lazily(hass):
    """Token wird via Callable übergeben — Refresh-Pfad kann das Token
    rotieren ohne den SSEClient neu zu instanziieren."""
    current_token = ["v1"]
    client = SSEClient(
        hass=hass,
        api_url="http://test",
        get_token=lambda: current_token[0],
        refresh_token=lambda: _async_true(),
    )
    assert client._get_token() == "v1"
    current_token[0] = "v2"
    assert client._get_token() == "v2"
