"""RixsLiveService on a run whose start doc names a PVA channel (the real Marana)."""
import numpy as np

from test_pva_io import CHANNEL, COUNTER, FakeContext, Run, Stream, ntnd
from test_service import (
    FakeNats,
    FakeTiled,
    _bind,
    _rixs_md,
    _until,
    stub_score,  # noqa: F401 - fixture
)

from rixs_live.config import AppConfig, load_config
from rixs_live.contract import EVT_COMPLETE, EVT_ERROR, EVT_FRAME
from rixs_live.service import RixsLiveService

SHAPE = (6, 8)


class GrowingStream(Stream):
    """Stream whose first ``visible`` rows exist (no image column: it is external)."""

    def __init__(self, positions, counters):
        super().__init__(positions, counters)
        self._full = dict(self)
        self.visible = 0

    def __getitem__(self, key):
        col = self._full[key]
        col.shape = (self.visible,)
        return col


def _service(ctx, **cfg_over):
    cfg = AppConfig(**{"poll_interval_s": 0.01, "bind_lookup_timeout_s": 0.3,
                       "drain_settle_s": 0.2, "drain_timeout_s": 2.0, **cfg_over})
    tiled = FakeTiled()
    fake = FakeNats()
    svc = RixsLiveService(cfg, nats_client=fake, emit=fake.emit,
                          tiled_connect=lambda url, key, proxy=None: tiled,
                          pva_context_factory=lambda: ctx)
    return svc, fake, tiled


def _add_pva_run(tiled, uid, positions, counters):
    stream = GrowingStream(positions, counters)
    start = {"uid": uid, "time": 1_700_000_000.0, "plan_name": "scan",
             "num_points": len(positions),
             "rixs": _rixs_md(SHAPE, pva_channel=CHANNEL, frame_counter_field=COUNTER)}
    run = Run(stream)
    run.metadata = {"start": start}
    tiled.runs[uid] = run
    return stream


def _frame(width_px):
    return np.full(SHAPE, width_px, dtype=np.uint16)  # stub_score: FWHM = frame mean


async def test_pva_run_scores_frames_from_the_channel(stub_score):  # noqa: F811
    ctx = FakeContext()
    svc, nats, tiled = _service(ctx)
    positions = [1.0, 2.0, 3.0]
    stream = _add_pva_run(tiled, "runP", positions, counters=[21, 22, 23])
    await svc._handle_bind(_bind("runP"), None)
    assert CHANNEL in ctx.monitors  # started by the bind
    for k, (uid, width) in enumerate([(21, 5), (22, 4), (23, 6)], start=1):
        ctx.post(CHANNEL, ntnd(_frame(width), uid, stamp=1_700_000_000.0 + k))
        stream.visible = k
        await _until(lambda k=k: len(nats.on(EVT_FRAME)) == k)
    frames = nats.on(EVT_FRAME)
    assert [(f["seq_num"], f["position"], f["fwhm_px"]) for f in frames] == [
        (1, 1.0, 5.0), (2, 2.0, 4.0), (3, 3.0, 6.0)]
    await svc._handle_stop({"run_uid": "runP", "exit_status": "success",
                                         "num_events": {"primary": 3}}, None)
    (done,) = nats.on(EVT_COMPLETE)
    assert done["best_measured"] == {"position": 2.0, "fwhm_px": 4.0}
    assert not nats.on(EVT_ERROR)


async def test_configured_channels_are_monitored_before_any_bind(stub_score):  # noqa: F811
    """Frames that land while a bind is still finding the run in Tiled are kept."""
    ctx = FakeContext()
    svc, nats, tiled = _service(ctx, pva={"channels": [CHANNEL]})
    svc._pva_buffer(CHANNEL)  # what _main does at startup
    ctx.post(CHANNEL, ntnd(_frame(3), 31, stamp=1_700_000_000.5))  # before the bind
    stream = _add_pva_run(tiled, "runQ", [7.0], counters=[31])
    stream.visible = 1
    await svc._handle_bind(_bind("runQ"), None)
    await _until(lambda: len(nats.on(EVT_FRAME)) == 1)
    assert nats.on(EVT_FRAME)[0]["fwhm_px"] == 3.0
    await svc.wait_idle()
    svc.close_pva()
    assert ctx.sub.closed


async def test_missing_frame_is_a_loud_error(stub_score):  # noqa: F811
    ctx = FakeContext()
    svc, nats, tiled = _service(ctx, pva={"frame_timeout_s": 0.2})
    stream = _add_pva_run(tiled, "runR", [1.0], counters=[41])
    stream.visible = 1
    await svc._handle_bind(_bind("runR"), None)
    await _until(lambda: nats.on(EVT_ERROR))
    (err,) = nats.on(EVT_ERROR)
    assert err["run_uid"] == "runR" and "uniqueId 41" in err["error"]
    assert CHANNEL in err["error"] and not svc.busy


def test_cli_options_reach_the_pva_config():
    cfg = load_config(pva_addr_list="602marana.dhcp.lbl.gov", pva_channels=[CHANNEL])
    assert cfg.pva.addr_list == "602marana.dhcp.lbl.gov"
    assert cfg.pva.auto_addr_list is False
    assert cfg.pva.channels == [CHANNEL]
