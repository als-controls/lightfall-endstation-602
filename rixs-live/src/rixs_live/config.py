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


def load_config(path: str | None = None, *, nats_url: str | None = None,
                lightfall_prefix: str | None = None,
                dispersion: float | None = None) -> AppConfig:
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
    return cfg
