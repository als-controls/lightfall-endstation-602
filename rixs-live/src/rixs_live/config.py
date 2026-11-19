"""rixs-live configuration: NATS, Tiled, polling and scoring settings (YAML + CLI overrides)."""
from __future__ import annotations

import yaml
from pydantic import BaseModel


class NatsConfig(BaseModel):
    url: str = "nats://127.0.0.1:4222"
    tls_ca: str | None = None
    tls_insecure: bool = False
    lightfall_prefix: str = "als.7011"
    # ServiceHost scope; "" = lightfall_prefix (Lightfall resolves the service at
    # {ipc_topic_prefix}.svc.rixs-live, so the two must agree).
    scope: str = ""
    app_name: str = "rixs-live"
    app_version: str = ""
    auth_timeout: float = 70.0  # > 60s Lightfall trust-dialog timeout
    connect_timeout: float = 5.0
    max_reconnect_attempts: int = -1  # -1 = forever (nats-py default gives up after ~2 min)
    reconnect_time_wait: float = 2.0


class TiledConfig(BaseModel):
    url: str = ""
    api_key: str | None = None


class PvaConfig(BaseModel):
    """PVA frame source (runs whose start doc names a ``pva_channel``)."""
    addr_list: str = ""  # EPICS_PVA_ADDR_LIST, e.g. "602marana.dhcp.lbl.gov"; "" = env
    auto_addr_list: bool | None = None  # EPICS_PVA_AUTO_ADDR_LIST; None = env
    channels: list[str] = []  # monitored from service start (others from their first bind)
    buffer_frames: int = 16  # frames kept per channel (2048x2048 uint16 = 8 MB each)
    frame_timeout_s: float = 20.0  # an event row waits this long for its frame
    stale_slack_s: float = 30.0  # frames stamped this long before run start are stale


class AppConfig(BaseModel):
    nats: NatsConfig = NatsConfig()
    tiled: TiledConfig = TiledConfig()
    pva: PvaConfig = PvaConfig()
    poll_interval_s: float = 0.5
    # Spectrometer dispersion; 0.0 means unknown -> fwhm_mev and resolving power are null.
    dispersion_mev_per_px: float = 0.0
    # Completed scan payloads kept for result.get rpcs (oldest evicted).
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
                tiled_api_key: str | None = None,
                pva_addr_list: str | None = None,
                pva_channels: list[str] | tuple[str, ...] = ()) -> AppConfig:
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
    if pva_addr_list:
        cfg.pva.addr_list = pva_addr_list
        if cfg.pva.auto_addr_list is None:
            cfg.pva.auto_addr_list = False  # an explicit list replaces broadcast search
    for channel in pva_channels:
        if channel not in cfg.pva.channels:
            cfg.pva.channels.append(channel)
    return cfg
