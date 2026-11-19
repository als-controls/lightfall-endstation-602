"""Test-side session client for rixs-live's ServiceHost over a real broker.

A small stand-in for lightfall_utils' ``ServiceClient`` (which Lightfall uses; it is not
installed in this venv). It speaks the same protocol through the vendored helpers:
public ``meta.info`` -> private ``auth.request`` -> rpcs on the session subject, lease
pings, and per-session events under its own delivery root.
"""
from __future__ import annotations

import asyncio
import contextlib
import json

from rixs_live.contract import SERVICE_NAME
from rixs_live.nats_capability import new_token
from rixs_live.svc import protocol as P
from rixs_live.svc.core import NatsCore


class SvcSession:
    def __init__(self, url: str, scope: str, app_name: str = "lightfall") -> None:
        self.url, self.scope, self.app_name = url, scope, app_name
        self.core = NatsCore(url)
        self.events: list[tuple[str, dict]] = []
        self.session_subject: str | None = None
        self._delivery_root = ""
        self._ping: asyncio.Task | None = None

    async def start(self, wait_s: float = 15.0) -> dict:
        """Connect, wait for the service's meta.info, then handshake; returns the info."""
        await self.core.connect()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_s
        while True:
            try:
                info = await self.core.request_public(
                    P.meta_info(P.root(self.scope, SERVICE_NAME)), {}, 1.0)
                break
            except Exception:
                if loop.time() > deadline:
                    raise
                await asyncio.sleep(0.2)
        token = new_token()
        self._delivery_root = P.delivery_root(self.core.priv_root, token)
        await self.core.subscribe(f"{self._delivery_root}.>", self._on_delivery)
        await self.core.flush()
        reply = await self.core.request_private(P.auth_subject(info["priv_root"]), {
            "app_name": self.app_name, "app_version": "test",
            "delivery_token": token, "client_priv_root": self.core.priv_root}, 5.0)
        if reply.get("status") != "approved":
            raise PermissionError(reply)
        self.session_subject = reply["session_subject"]
        self._ping = loop.create_task(self._lease())
        return info

    async def _on_delivery(self, msg) -> None:
        name = msg.subject[len(self._delivery_root) + 1:]
        self.events.append((name, json.loads(msg.data)))

    async def _lease(self) -> None:
        while True:
            await asyncio.sleep(P.PING_S)
            with contextlib.suppress(Exception):
                await self.call("session.ping", {})

    async def call(self, action: str, payload: dict, timeout: float = 5.0) -> dict:
        return await self.core.request_private(
            P.rpc_subject(self.session_subject, action), payload, timeout)

    def on(self, name: str) -> list[dict]:
        return [p for n, p in self.events if n == name]

    async def close(self) -> None:
        if self._ping is not None:
            self._ping.cancel()
        with contextlib.suppress(Exception):
            await self.core.close()
