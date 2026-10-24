"""Event-loop-safe construction of short-lived httpx clients.

`httpx.AsyncClient(...)` loads the CA bundle synchronously
(`load_verify_locations`) when it is created. Inside the event loop
Home Assistant flags that as a blocking call, so every client is built in
the executor and only used (and closed) in the loop.
"""
from __future__ import annotations

from typing import Any

import httpx


async def async_create_client(hass: Any, **kwargs: Any) -> httpx.AsyncClient:
    """Build an `httpx.AsyncClient` in the executor. The caller owns it and
    must close it (`async with` or `aclose()`)."""
    return await hass.async_add_executor_job(
        lambda: httpx.AsyncClient(**kwargs)
    )
