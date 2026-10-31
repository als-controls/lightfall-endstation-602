"""Raw nats-py client: connect, Lightfall auth handshake, JSON pub/sub.

Subjects are passed verbatim — the rixs.* namespace is unprefixed on the bus
(XPCS convention). lightfall_prefix is used for the {prefix}.meta.actions
discovery request, which advertises the private root; the auth handshake
itself then goes to {priv_root}.auth.request.
"""
from __future__ import annotations

import json
import ssl

import nats
from loguru import logger

from rixs_live.config import NatsConfig
from rixs_live.nats_capability import (
    PrivateRootUnavailable,
    new_capability,
    private_auth_request,
)


class NatsClient:
    def __init__(self, config: NatsConfig) -> None:
        self._config = config
        self._nc = None
        self._cap = new_capability()
        self.tiled_credentials: dict | None = None

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

    async def connect(self) -> None:
        kwargs = dict(connect_timeout=self._config.connect_timeout,
                      **self._cap.connect_kwargs())
        tls = self._build_tls_context()
        if tls is not None:
            kwargs["tls"] = tls
        self._nc = await nats.connect(self._config.url, **kwargs)
        logger.info(f"connected to NATS at {self._config.url}")

    async def close(self) -> None:
        if self._nc is not None:
            await self._nc.drain()
            self._nc = None

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

    async def publish(self, subject: str, payload: dict) -> None:
        await self._nc.publish(subject, json.dumps(payload).encode())

    async def subscribe(self, subject: str, cb) -> None:
        async def handler(msg):
            try:
                data = json.loads(msg.data) if msg.data else {}
            except json.JSONDecodeError:
                logger.warning(f"non-JSON message on {msg.subject}; dropped")
                return
            await cb(data, msg)

        await self._nc.subscribe(subject, cb=handler)

    async def reply(self, msg, payload: dict) -> None:
        if getattr(msg, "reply", None):
            await self._nc.publish(msg.reply, json.dumps(payload).encode())
