"""Lazy process-wide singletons shared by the panel and the agent tools."""

from __future__ import annotations

from loguru import logger

from lightfall_endstation_602.binder import RixsRunBinder
from lightfall_endstation_602.client import RixsServiceClient

_client: RixsServiceClient | None = None
_binder: RixsRunBinder | None = None


def get_client() -> RixsServiceClient:
    global _client
    if _client is None:
        _client = RixsServiceClient()
    return _client


def get_binder() -> RixsRunBinder:
    global _binder
    if _binder is None:
        _binder = RixsRunBinder(get_client())
    return _binder


def ensure_binder_enabled() -> bool:
    """Enable the binder; False when there is no RunEngine yet."""
    binder = get_binder()
    if binder.enabled:
        return True
    try:
        binder.enable()
    except Exception as ex:
        logger.debug(f"rixs binder not enabled yet: {ex!r}")
        return False
    return True
