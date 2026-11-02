"""rixs-live configuration: NATS, Tiled, polling and scoring settings (YAML + CLI overrides)."""
from __future__ import annotations

import yaml
from pydantic import BaseModel


class NatsConfig(BaseModel):
    url: str = "nats://127.0.0.1:4222"
    tls_ca: str | None = None
    tls_insecure: bool = False
    lightfall_prefix: str = "als.7011"
    app_name: str = "rixs-live"
    app_version: str = ""
    auth_timeout: float = 70.0  # > 60s Lightfall trust-dialog timeout
    connect_timeout: float = 5.0
    max_reconnect_attempts: int = -1  # -1 = forever (nats-py default gives up after ~2 min)
    reconnect_time_wait: float = 2.0


class TiledConfig(BaseModel):
    url: str = ""
    api_key: str | None = None


class AppConfig(BaseModel):
    nats: NatsConfig = NatsConfig()
    tiled: TiledConfig = TiledConfig()
    poll_interval_s: float = 0.5
    # Spectrometer dispersion; 0.0 means unknown -> fwhm_mev and resolving power are null.
    dispersion_mev_per_px: float = 0.0
    # Completed scan payloads kept for SUBJ_RESULT requests (oldest evicted).
    keep_results: int = 20
    # Lightfall's writer is asynchronous (a queue drained by a thread), so a bind can
    # arrive before the run exists in Tiled and a stop before the last events land.
    bind_lookup_timeout_s: float = 10.0  # keep retrying the run lookup this long
    drain_settle_s: float = 2.0  # total unknown: drain ends after this long with no new rows
    drain_timeout_s: float = 15.0  # cap on drain time spent waiting with no new rows


def load_config(path: str | None = None, *, nats_url: str | None = None,
                lightfall_prefix: str | None = None,
                dispersion: float | None = None,
                tiled_url: str | None = None,
                tiled_api_key: str | None = None) -> AppConfig:
    data = {}
    if path:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
    cfg = AppConfig(**data)
    if nats_url:
        cfg.nats.url = nats_url
    if lightfall_prefix:
        cfg.nats.lightfall_prefix = lightfall_prefix
    if dispersion is not None:
        cfg.dispersion_mev_per_px = dispersion
    if tiled_url:
        cfg.tiled.url = tiled_url
    if tiled_api_key:
        cfg.tiled.api_key = tiled_api_key
    return cfg
