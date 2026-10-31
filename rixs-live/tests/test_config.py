from click.testing import CliRunner

from rixs_live.cli import main
from rixs_live.config import AppConfig, load_config


def test_defaults():
    cfg = AppConfig()
    assert cfg.nats.app_name == "rixs-live"
    assert cfg.poll_interval_s == 0.5
    assert cfg.dispersion_mev_per_px == 0.0
    assert cfg.keep_results == 20
    assert cfg.tiled.url == ""


def test_yaml_and_overrides(tmp_path):
    p = tmp_path / "c.yml"
    p.write_text("nats:\n  url: nats://broker:4222\ndispersion_mev_per_px: 1.5\n")
    cfg = load_config(str(p), nats_url="nats://cli-wins:4222")
    assert cfg.nats.url == "nats://cli-wins:4222"
    assert cfg.dispersion_mev_per_px == 1.5
    assert load_config(str(p), dispersion=2.5).dispersion_mev_per_px == 2.5


def test_cli_run_wires_config(monkeypatch):
    captured = {}
    monkeypatch.setattr("rixs_live.cli._run_service",
                        lambda cfg: captured.setdefault("cfg", cfg))
    result = CliRunner().invoke(
        main, ["run", "--nats-url", "nats://x:4222", "--lightfall-prefix", "als.dev",
               "--dispersion", "2.5"])
    assert result.exit_code == 0, result.output
    assert captured["cfg"].nats.url == "nats://x:4222"
    assert captured["cfg"].nats.lightfall_prefix == "als.dev"
    assert captured["cfg"].dispersion_mev_per_px == 2.5
