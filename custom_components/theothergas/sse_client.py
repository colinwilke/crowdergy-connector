"""SSEClient — Reconnecting Server-Sent-Events Reader extrahiert aus
coordinator.py (FEAT-5 Phase B, 2026-06-09).

Trennt das **Stream-Reading** vom **Message-Dispatch**:

* `SSEClient` öffnet die Backend-SSE-Verbindung, kümmert sich um
  Reconnect-Backoff (1 s → 60 s) und Auth-401-Refresh, und pusht
  jeden geparsten JSON-Frame in eine asyncio.Queue.
* Der Coordinator konsumiert die Queue in einem separaten Task und
  dispatcht Telemetry-/Command-Frames auf die jeweiligen Apply-
  Methoden.

Die Trennung macht den SSE-Pfad testbar ohne den Apply-Stack
mitzustarten (Cluster E Connector Test-Setup), und entkoppelt zwei
Failure-Domains: ein hängender Apply-Call blockiert nicht mehr den
Stream-Reader (Queue overflow → Drop + Warning statt Stream-Stall).

Memo: `project_connector_coordinator_split_plan_2026_06_09.md` Phase B.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers import aiohttp_client

_LOGGER = logging.getLogger(__name__)

# SSE-Reconnect-Backoff. Identisch zu den Werten die vor der Extraktion
# direkt in coordinator.py standen — keine Verhaltensänderung in Phase B.
SSE_RECONNECT_INITIAL = 1
SSE_RECONNECT_MAX = 60

# Read-Timeout im SSE-Body-Stream. Backend sendet alle 15 s einen
# Keep-Alive-Ping; fehlt der für SSE_READ_TIMEOUT_S Sekunden, ist die
# TCP-Verbindung wahrscheinlich halb-tot (NAT-Drop, Proxy-Idle-Kill,
# Server hängt). Ohne Timeout würde `async for raw in resp.content`
# ewig blocken → User-Befehle fielen ins Leere bis Connector-Reload.
SSE_READ_TIMEOUT_S = 60

# Queue-Cap (Phase-B Backpressure). Bei normaler Last steht da 1-2
# Frames, mehr nur wenn der Consumer-Task hängt. 512 ist großzügig
# damit kurze Apply-Spikes nichts droppen, klein genug damit ein
# echter Consumer-Stall sichtbar wird.
SSE_QUEUE_MAXSIZE = 512

# CN-11 (2026-06-11): nach so vielen aufeinanderfolgenden 401-Zyklen
# (Handshake-401 → Refresh-Versuch → erneut 401) gilt die Auth als
# endgültig tot — der Reader stoppt und meldet via `on_auth_failed`
# (Coordinator startet dann den Reauth-Flow). Reset des Zählers erst
# nach erfolgreichem Stream (erstes Body-Byte).
SSE_AUTH_FAILURE_LIMIT = 5


class SSEClient:
    """Reconnecting SSE-Listener mit Auth-Refresh-Callback.

    Owns:
    * den Background-Reader-Task (`start()` / `stop()`)
    * die in-memory Message-Queue
    * den `last_event_at`-Timestamp (Telemetry-Liveness)

    NICHT Owner von:
    * Auth-State (das bleibt im Coordinator, durchgereicht via
      `get_token` und `refresh_token` Callbacks)
    * Message-Dispatch (Consumer im Coordinator nimmt Frames ab und
      mapped auf Apply-Methoden)
    """

    def __init__(
        self,
        *,
        hass: HomeAssistant,
        api_url: str,
        get_token: Callable[[], str],
        refresh_token: Callable[[], Awaitable[bool]],
        on_auth_failed: Callable[[], None] | None = None,
    ) -> None:
        self._hass = hass
        self._api_url = api_url
        self._get_token = get_token
        self._refresh_token = refresh_token
        # CN-11: Callback wenn SSE_AUTH_FAILURE_LIMIT 401-Zyklen
        # erreicht sind — Coordinator hängt hier
        # `entry.async_start_reauth(hass)` dran.
        self._on_auth_failed = on_auth_failed
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=SSE_QUEUE_MAXSIZE
        )
        self._task: asyncio.Task | None = None
        self._last_event_at: float = 0.0

    @property
    def messages(self) -> asyncio.Queue[dict[str, Any]]:
        """Read-only Zugriff auf die Inbound-Queue. Coordinator
        consumer-task `await client.messages.get()`-ed sich Frames raus."""
        return self._queue

    @property
    def last_event_at(self) -> float:
        return self._last_event_at

    def _sse_url(self) -> str:
        return f"{self._api_url}/api/v1/stream"

    def start(self, *, task_name: str = "crowdergy-sse") -> None:
        """Idempotent — schon laufender Task wird nicht erneut gespawnt."""
        if self._task and not self._task.done():
            return
        self._task = self._hass.async_create_background_task(
            self._run_loop(), name=task_name,
        )

    async def stop(self) -> None:
        """Cancel + await — ruhiger Shutdown ohne hängende Task-References."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _run_loop(self) -> None:
        """Reconnecting SSE-Reader. Identische Semantik zur vorigen
        coordinator._run_sse_loop-Implementation; Frames landen jetzt
        in self._queue statt direkt _handle_ws_message zu rufen."""
        delay = SSE_RECONNECT_INITIAL
        auth_failures = 0
        session = aiohttp_client.async_get_clientsession(self._hass)
        while True:
            try:
                async with session.get(
                    self._sse_url(),
                    headers={
                        "Accept": "text/event-stream",
                        "Cache-Control": "no-cache",
                        # Header-Bearer statt ?token=… — letzteres leakte
                        # in nginx access logs (deprecated 2026-05-27).
                        "Authorization": f"Bearer {self._get_token()}",
                    },
                    timeout=aiohttp.ClientTimeout(
                        total=None, sock_read=SSE_READ_TIMEOUT_S,
                    ),
                ) as resp:
                    if resp.status == 401:
                        # CN-11 (2026-06-11): vorher lief bei Refresh-
                        # Erfolg ein `continue` OHNE Sleep — wenn das
                        # Backend trotz frischem Token weiter 401t
                        # (Revocation, Clock-Skew, Backend-Bug), war
                        # das ein Hot-Loop. Jetzt: Zähler + Backoff
                        # auch im Erfolgsfall; nach
                        # SSE_AUTH_FAILURE_LIMIT Zyklen Reauth + Stop.
                        auth_failures += 1
                        if auth_failures >= SSE_AUTH_FAILURE_LIMIT:
                            _LOGGER.error(
                                "Crowdergy SSE: %d consecutive auth "
                                "failures — token pair looks dead, "
                                "stopping stream and starting reauth.",
                                auth_failures,
                            )
                            if self._on_auth_failed is not None:
                                self._on_auth_failed()
                            return
                        refreshed = await self._refresh_token()
                        backoff = min(
                            SSE_RECONNECT_INITIAL * (2 ** (auth_failures - 1)),
                            SSE_RECONNECT_MAX,
                        )
                        _LOGGER.warning(
                            "Crowdergy SSE got 401 (cycle %d/%d, token "
                            "refresh %s) — retrying in %ss",
                            auth_failures, SSE_AUTH_FAILURE_LIMIT,
                            "ok" if refreshed else "failed", backoff,
                        )
                        await asyncio.sleep(backoff)
                        continue
                    if resp.status != 200:
                        _LOGGER.warning(
                            "Crowdergy SSE handshake failed (%s) — "
                            "retrying in %ss",
                            resp.status, delay,
                        )
                        raise aiohttp.ClientError(f"status {resp.status}")
                    _LOGGER.info(
                        "Crowdergy SSE connected to %s/api/v1/stream",
                        self._api_url,
                    )
                    # Reset back-off erst NACH erstem Body-Byte — ein
                    # handshake-ok / immediate-disconnect-Loop würde
                    # sonst bei 1 s floor hängen und den Backend
                    # hämmern.
                    saw_body = False
                    async for raw in resp.content:
                        if not saw_body:
                            saw_body = True
                            delay = SSE_RECONNECT_INITIAL
                            # Stream lebt → Auth ist gesund, Zähler
                            # zurücksetzen (CN-11).
                            auth_failures = 0
                        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                        if not line.startswith("data: "):
                            continue
                        body = line[len("data: "):]
                        try:
                            msg = json.loads(body)
                        except json.JSONDecodeError as err:
                            _LOGGER.warning(
                                "Crowdergy SSE: bad JSON frame: %s", err,
                            )
                            continue
                        self._last_event_at = time.time()
                        # Backpressure: bei stehendem Consumer Frame
                        # droppen statt den Reader zu blockieren.
                        # 512 Slots = mehrere Minuten Burst.
                        try:
                            self._queue.put_nowait(msg)
                        except asyncio.QueueFull:
                            _LOGGER.warning(
                                "Crowdergy SSE queue full — "
                                "dropping frame (consumer stalled?)"
                            )
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                # asyncio.TimeoutError = sock_read=SSE_READ_TIMEOUT_S
                # ausgelöst, d.h. half-open Stream → reconnect.
                _LOGGER.warning(
                    "Crowdergy SSE stream stale or client-error: %s — "
                    "reconnecting in %ss",
                    err, delay,
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("Unexpected SSE error: %s", err)

            await asyncio.sleep(delay)
            delay = min(delay * 2, SSE_RECONNECT_MAX)
