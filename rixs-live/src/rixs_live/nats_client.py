"""rixs-live's broker connection: connect, reconnect forever, Lightfall auth handshake.

The connection carries a per-connection capability (token + inbox prefix), so the
broker's auth callout grants it a private ``_PRIV.<p>`` root. The service's
:class:`~rixs_live.svc.host.ServiceHost` runs on it through :attr:`NatsClient.core`.

As a *client* of Lightfall, the service also asks Lightfall for Tiled credentials:
``lightfall_prefix`` names Lightfall's ``{prefix}.meta.actions`` discovery request,
which advertises Lightfall's private root; the auth handshake (which the user
approves in Lightfall's trust dialog) then goes to ``{priv_root}.auth.request``.
"""
from __future__ import annotations

import asyncio
import ssl
from typing import Awaitable, Callable

import nats
from loguru import logger

from rixs_live.config import NatsConfig
from rixs_live.nats_capability import (
    ConnectionCapability,
    PrivateRootUnavailable,
    new_capability,
    private_auth_request,
)
from rixs_live.svc.core import NatsCore


class ServiceCore(NatsCore):
    """A :class:`NatsCore` over a connection :class:`NatsClient` already made.

    The vendored ``NatsCore.connect`` has no connect timeout, reconnect wait or error
    callback, and the vendored file must not be edited; so NatsClient connects with
    its own options and the host gets this view of that same connection.
    """

    def __init__(self, url: str, capability: ConnectionCapability, nc) -> None:
        super().__init__(url, capability=capability)
        self._nc = nc

    async def connect(self) -> None:  # already connected by NatsClient
        return None


class NatsClient:
    def __init__(self, config: NatsConfig) -> None:
        self._config = config
        self._nc = None
        self._cap = new_capability()
        self.core: ServiceCore | None = None  # set by connect()
        self.tiled_credentials: dict | None = None
        # Set by closed_cb: the connection gave up for good (or was closed).
        self.closed = asyncio.Event()
        # Awaited after every successful reconnect (e.g. to re-run the auth handshake).
        self.on_reconnected: Callable[[], Awaitable[None]] | None = None

    def _build_tls_context(self) -> ssl.SSLContext | None:
        cfg = self._config
        if not cfg.tls_ca and not cfg.tls_insecure:
            return None
        ctx = ssl.create_default_context()
        if cfg.tls_ca:
            ctx.load_verify_locations(cfg.tls_ca)
        if cfg.tls_insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    async def _disconnected_cb(self) -> None:
        logger.warning(f"NATS disconnected from {self._config.url}; reconnecting")

    async def _reconnected_cb(self) -> None:
        url = getattr(self._nc, "connected_url", None)
        logger.info(f"NATS reconnected to {url.netloc if url else self._config.url}")
        if self.on_reconnected is not None:
            try:
                await self.on_reconnected()
            except Exception as ex:
                logger.warning(f"post-reconnect hook failed: {ex}")

    async def _closed_cb(self) -> None:
        logger.error(f"NATS connection to {self._config.url} closed")
        self.closed.set()

    async def _error_cb(self, ex: Exception) -> None:
        logger.warning(f"NATS error: {ex!r}")

    @property
    def is_closed(self) -> bool:
        return self.closed.is_set() or (self._nc is not None and self._nc.is_closed)

    async def connect(self) -> None:
        cfg = self._config
        # nats-py defaults to 60 attempts x 2 s, then closes for good; a long-running
        # service must survive any broker restart, so reconnect forever.
        kwargs = dict(connect_timeout=cfg.connect_timeout,
                      max_reconnect_attempts=cfg.max_reconnect_attempts,
                      reconnect_time_wait=cfg.reconnect_time_wait,
                      disconnected_cb=self._disconnected_cb,
                      reconnected_cb=self._reconnected_cb,
                      closed_cb=self._closed_cb,
                      error_cb=self._error_cb,
                      **self._cap.connect_kwargs())
        tls = self._build_tls_context()
        if tls is not None:
            kwargs["tls"] = tls
        self._nc = await nats.connect(self._config.url, **kwargs)
        self.core = ServiceCore(self._config.url, self._cap, self._nc)
        logger.info(f"connected to NATS at {self._config.url}")

    async def close(self) -> None:
        if self._nc is not None:
            nc, self._nc = self._nc, None
            if not nc.is_closed:
                await nc.drain()

    async def authenticate(self) -> dict | None:
        cfg = self._config
        payload = {"app_name": cfg.app_name, "app_version": cfg.app_version}
        try:
            reply = await private_auth_request(
                self._nc, cfg.lightfall_prefix, payload, timeout=cfg.auth_timeout)
        except PrivateRootUnavailable as ex:
            logger.warning(
                f"auth skipped: Lightfall did not advertise a private root "
                f"(is the service running with the auth callout?): {ex}")
            return None
        except Exception as ex:
            logger.warning(f"auth.request failed: {ex}")
            return None
        if reply.get("status") != "approved":
            logger.warning(f"auth denied: {reply.get('reason', '')}")
            return None
        self.tiled_credentials = {
            "tiled_token": reply.get("tiled_token"),
            "tiled_url": reply.get("tiled_url", ""),
            "session_id": reply.get("session_id"),
        }
        logger.info("auth approved; Tiled credentials cached")
        return self.tiled_credentials
