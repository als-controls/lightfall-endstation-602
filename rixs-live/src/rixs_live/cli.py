from __future__ import annotations

import click

from rixs_live.config import AppConfig, load_config


def _run_service(cfg: AppConfig) -> None:  # patched in tests
    from rixs_live.service import RixsLiveService
    RixsLiveService(cfg).main()


@click.group()
def main() -> None:
    """rixs-live: headless RIXS analysis service for ALS 6.0.2."""


@main.command()
@click.option("--nats-url", default=None, help="NATS broker URL")
@click.option("--lightfall-prefix", default=None,
              help="Lightfall's ipc_topic_prefix, e.g. als.602: the service scope "
                   "(als.602.svc.rixs-live) and Lightfall's auth subject")
@click.option("--dispersion", type=float, default=None,
              help="Spectrometer dispersion in meV/px (0 = unknown)")
@click.option("--tiled-url", default=None,
              help="Tiled URL (fallback when the bind and auth handshake give none)")
@click.option("--tiled-api-key", default=None, envvar="RIXS_LIVE_TILED_API_KEY",
              help="Tiled API key for brokers without the auth callout "
                   "(env: RIXS_LIVE_TILED_API_KEY)")
@click.option("--pva-addr-list", default=None,
              help="EPICS_PVA_ADDR_LIST for the PVA frame source, e.g. the IOC host "
                   "(turns broadcast search off)")
@click.option("--pva-channel", "pva_channels", multiple=True,
              help="NTNDArray channel to monitor from startup (repeatable), e.g. "
                   "BL602:marana01:Pva1:Image")
@click.option("--config", "config_path", default=None, type=click.Path(exists=True))
def run(nats_url, lightfall_prefix, dispersion, tiled_url, tiled_api_key, pva_addr_list,
        pva_channels, config_path) -> None:
    """Run the analysis service."""
    cfg = load_config(config_path, nats_url=nats_url,
                      lightfall_prefix=lightfall_prefix, dispersion=dispersion,
                      tiled_url=tiled_url, tiled_api_key=tiled_api_key,
                      pva_addr_list=pva_addr_list, pva_channels=pva_channels)
    _run_service(cfg)
