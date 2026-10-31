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
@click.option("--lightfall-prefix", default=None, help="Lightfall topic prefix (auth subject)")
@click.option("--dispersion", type=float, default=None,
              help="Spectrometer dispersion in meV/px (0 = unknown)")
@click.option("--config", "config_path", default=None, type=click.Path(exists=True))
def run(nats_url, lightfall_prefix, dispersion, config_path) -> None:
    """Run the analysis service."""
    cfg = load_config(config_path, nats_url=nats_url,
                      lightfall_prefix=lightfall_prefix, dispersion=dispersion)
    _run_service(cfg)
