"""RixsLiveService against fake session delivery and a fake, growing Tiled run."""
import asyncio

import numpy as np
import pytest
from lightfall_endstation_602.sim.model import FocusModel, render_frame

from rixs_live.config import AppConfig
from rixs_live.contract import (
    CONTRACT_VERSION,
    EVT_COMPLETE,
    EVT_ERROR,
    EVT_FRAME,
    EVT_STATUS,
    RPC_BIND,
    RPC_RESULT,
    RPC_STOP,
)
from rixs_live.focus import FrameScore
from rixs_live.service import RixsLiveService

FRAME_KEYS = {"run_uid", "seq_num", "motor_field", "motor_units", "position", "positions",
              "fwhm_px", "fwhm_mev", "resolving_power", "score_valid", "failure_reason",
              "centroid", "angle_deg", "preview"}


class FakeNats:
    """NatsClient stand-in that also records every session event the service emits."""

    def __init__(self):
        self.published = []  # (event name, payload, session)
        self.tiled_credentials = None
        self.core = None

    async def connect(self): ...
    async def close(self): ...
    async def authenticate(self): return None

    async def emit(self, session, name, payload):
        self.published.append((name, payload, session))

    def on(self, name):
        return [p for n, p, _ in self.published if n == name]

    def sessions_of(self, name):
        return [s for n, _, s in self.published if n == name]


class FakeStream:
    """A primary stream whose rows become visible as the test reveals them."""

    def __init__(self, frames, positions, table_order=None, motor="sm3_pitch", extra=None):
        self._cols = {
            "marana_image": np.asarray(frames, dtype=float),
            motor: np.asarray(positions, dtype=float),
            f"{motor}_setpoint": np.asarray(positions, dtype=float) + 99.0,  # decoy
            "seq_num": np.arange(1, len(positions) + 1),
        }
        for name, values in (extra or {}).items():  # further motors of an N-D run
            self._cols[name] = np.asarray(values, dtype=float)
        if table_order is not None:  # table columns in scrambled storage order
            idx = np.asarray(table_order) - 1
            for k in [motor, f"{motor}_setpoint", "seq_num", *(extra or {})]:
                self._cols[k] = self._cols[k][idx]
        self.visible = 0

    def __iter__(self):
        return iter(self._cols)

    def __contains__(self, key):
        return key in self._cols

    def __getitem__(self, key):
        return self._cols[key][: self.visible]


class FakeRun:
    def __init__(self, start, stream=None, stream_name="primary"):
        self.metadata = {"start": start}
        self.stream = stream
        self.stream_name = stream_name

    def __getitem__(self, key):
        if key != self.stream_name or self.stream is None or self.stream.visible == 0:
            raise KeyError(key)
        return self.stream


class FakeTiled:
    def __init__(self):
        self.runs = {}

    def __getitem__(self, uid):
        return self.runs[uid]  # KeyError for unknown runs, like a Tiled container


def _rixs_md(shape, **over):
    md = {"contract_version": CONTRACT_VERSION, "kind": "zeroth_order_focus",
          "image_field": "marana_image", "motor_field": "sm3_pitch",
          "frame_shape": list(shape), "mono_energy_ev": 850.0}
    md.update(over)
    return md


def _add_run(tiled, uid, frames, positions, table_order=None, extra=None, **md_over):
    shape = np.asarray(frames).shape[1:]
    start = {"uid": uid, "plan_name": "scan", "num_points": len(positions),
             "rixs": _rixs_md(shape, **md_over)}
    stream = FakeStream(frames, positions, table_order,
                        motor=start["rixs"]["motor_field"], extra=extra)
    tiled.runs[uid] = FakeRun(start, stream,
                              stream_name=start["rixs"].get("image_stream", "primary"))
    return stream


def _service(fake, tiled_fake, **cfg_over):
    cfg = AppConfig(**{"poll_interval_s": 0.01, "bind_lookup_timeout_s": 0.3,
                       "drain_settle_s": 0.2, "drain_timeout_s": 2.0, **cfg_over})
    return RixsLiveService(cfg, nats_client=fake, emit=fake.emit,
                           tiled_connect=lambda url, key, proxy=None: tiled_fake)


def _bind(uid):
    return {"run_uid": uid, "tiled_url": "http://local",
            "contract_version": CONTRACT_VERSION}


async def _until(pred, timeout=30.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not pred():
        if loop.time() > deadline:
            raise AssertionError("timed out waiting for condition")
        await asyncio.sleep(0.01)


@pytest.fixture
def stub_score(monkeypatch):
    """Fast scorer: FWHM = frame mean, so synthetic frames encode their own width."""
    def fake(img, dispersion_mev_per_px, mono_energy_ev):
        w = float(np.mean(img))
        return FrameScore(w, None, None, True, None, (1.0, 2.0), 0.5)
    monkeypatch.setattr("rixs_live.service.score_frame", fake)


def _parabola_frames(positions, best=0.2, shape=(8, 8)):
    return [np.full(shape, 3.0 + (p - best) ** 2) for p in positions]


# --------------------------------------------------------------------------- core


async def test_bind_score_complete():
    """7 real 2048^2 sim frames through the real QERLIN scorer, revealed over 2 polls."""
    best = 104.0
    model = FocusModel(best_pitch=best)
    positions = [float(p) for p in np.linspace(best - 6.0, best + 6.0, 7)]
    rng = np.random.default_rng(7)
    frames = [render_frame(model, p, 1.0, shape=(2048, 2048), rng=rng) for p in positions]
    fake, tiled = FakeNats(), FakeTiled()
    stream = _add_run(tiled, "runA", frames, positions)
    svc = _service(fake, tiled, dispersion_mev_per_px=2.5)

    await svc._handle_bind(_bind("runA"), None)
    assert svc.busy
    stream.visible = 4
    await _until(lambda: len(fake.on(EVT_FRAME)) == 4, timeout=60)
    stream.visible = 7
    await _until(lambda: len(fake.on(EVT_FRAME)) == 7, timeout=60)
    await svc._handle_stop({"run_uid": "runA"}, None)
    await svc.wait_idle()

    msgs = fake.on(EVT_FRAME)
    assert [m["seq_num"] for m in msgs] == [1, 2, 3, 4, 5, 6, 7]
    assert all(set(m) == FRAME_KEYS for m in msgs)
    assert np.allclose([m["position"] for m in msgs], positions)  # not the setpoint decoy
    assert all(m["score_valid"] for m in msgs)
    assert msgs[3]["fwhm_mev"] == pytest.approx(msgs[3]["fwhm_px"] * 2.5)
    assert msgs[3]["resolving_power"] is not None
    assert set(msgs[0]["preview"]) == {"b64", "shape", "scale"}

    (done,) = fake.on(EVT_COMPLETE)
    assert done["run_uid"] == "runA" and done["motor_field"] == "sm3_pitch"
    assert done["motor_units"] == "" and msgs[0]["motor_units"] == ""
    assert msgs[0]["motor_field"] == "sm3_pitch"
    assert done["fit"]["ok"], done["fit"]
    assert abs(done["fit"]["optimal_position"] - best) < 1.5  # lopsided curve: vertex bias
    assert len(done["points"]) == 7
    assert set(done["points"][0]) == {"position", "fwhm_px", "fwhm_mev",
                                      "resolving_power", "score_valid"}
    assert done["best_measured"]["position"] == pytest.approx(positions[3])
    assert svc.results["runA"] == done
    assert fake.on(EVT_STATUS)[-1]["state"] == "idle"
    assert fake.on(EVT_STATUS)[-1]["frames_done"] == 7
    assert not svc.busy and not fake.on(EVT_ERROR)


async def test_stop_drains_frames_not_yet_polled(stub_score):
    positions = [-0.8, -0.4, 0.0, 0.4, 0.8]
    fake, tiled = FakeNats(), FakeTiled()
    stream = _add_run(tiled, "runA", _parabola_frames(positions), positions)
    svc = _service(fake, tiled, poll_interval_s=60.0)  # poll loop sleeps through the test
    await svc._handle_bind(_bind("runA"), None)
    await asyncio.sleep(0.05)
    stream.visible = 5
    await svc._handle_stop({"run_uid": "runA"}, None)
    assert [m["seq_num"] for m in fake.on(EVT_FRAME)] == [1, 2, 3, 4, 5]
    (done,) = fake.on(EVT_COMPLETE)
    assert done["fit"]["ok"] and abs(done["fit"]["optimal_position"] - 0.2) < 1e-6


async def test_any_motor_field_and_units_pass_through(stub_score):
    """Nothing is SM3-specific: a Sample X (mm) scan reads its own column and units."""
    positions = [-0.88, -0.84, -0.80, -0.76, -0.72]
    fake, tiled = FakeNats(), FakeTiled()
    stream = _add_run(tiled, "runX", _parabola_frames(positions, best=-0.8), positions,
                      motor_field="sample_x", motor_units="mm")
    stream.visible = 5
    svc = _service(fake, tiled)
    await svc._handle_bind(_bind("runX"), None)
    await svc._handle_stop({"run_uid": "runX"}, None)
    frames = fake.on(EVT_FRAME)
    assert [f["position"] for f in frames] == pytest.approx(positions)
    assert {(f["motor_field"], f["motor_units"]) for f in frames} == {("sample_x", "mm")}
    (done,) = fake.on(EVT_COMPLETE)
    assert (done["motor_field"], done["motor_units"]) == ("sample_x", "mm")
    assert done["fit"]["ok"] and done["fit"]["optimal_position"] == pytest.approx(-0.8)


async def test_complete_with_invalid_points(monkeypatch):
    def fake_score(img, d, e):
        w = float(np.mean(img))
        if w > 3.5:
            return FrameScore(None, None, None, False, "line not found", None, None)
        return FrameScore(w, None, None, True, None, None, None)
    monkeypatch.setattr("rixs_live.service.score_frame", fake_score)
    positions = [-1.0, -0.1, 0.2, 0.5, 2.0]
    fake, tiled = FakeNats(), FakeTiled()
    stream = _add_run(tiled, "runA", _parabola_frames(positions), positions)
    stream.visible = 5
    svc = _service(fake, tiled)
    await svc._handle_bind(_bind("runA"), None)
    await svc._handle_stop({"run_uid": "runA"}, None)
    (done,) = fake.on(EVT_COMPLETE)
    assert [p["score_valid"] for p in done["points"]] == [False, True, True, True, False]
    assert done["fit"]["ok"]
    assert done["best_measured"] == {"position": 0.2, "fwhm_px": 3.0}


async def test_no_valid_points_gives_null_best(monkeypatch):
    monkeypatch.setattr("rixs_live.service.score_frame",
                        lambda img, d, e: FrameScore(None, None, None, False, "x", None, None))
    fake, tiled = FakeNats(), FakeTiled()
    stream = _add_run(tiled, "runA", _parabola_frames([0.0, 1.0]), [0.0, 1.0])
    stream.visible = 2
    svc = _service(fake, tiled)
    await svc._handle_bind(_bind("runA"), None)
    await svc._handle_stop({"run_uid": "runA"}, None)
    (done,) = fake.on(EVT_COMPLETE)
    assert done["best_measured"] is None
    assert not done["fit"]["ok"]


# --------------------------------------------------------------------- results


async def test_result_request_returns_stored(stub_score):
    positions = [-0.5, 0.0, 0.5, 1.0]
    fake, tiled = FakeNats(), FakeTiled()
    stream = _add_run(tiled, "runA", _parabola_frames(positions), positions)
    stream.visible = 4
    svc = _service(fake, tiled)
    await svc._handle_bind(_bind("runA"), None)
    await svc._handle_stop({"run_uid": "runA"}, None)
    (done,) = fake.on(EVT_COMPLETE)

    assert await svc._rpc_result(None, {"run_uid": "runA"}) == {"result": done}
    assert await svc._rpc_result(None, {"run_uid": "ghost"}) == {"result": None}


async def test_result_request_pending_while_bound_and_draining(stub_score):
    """A result request for the bound run says pending (not unknown) until complete."""
    pos = [-0.8, -0.4, 0.0, 0.4, 0.8]
    fake, tiled = FakeNats(), FakeTiled()
    stream = _add_run(tiled, "runA", _parabola_frames(pos), pos)  # num_points = 5
    svc = _service(fake, tiled, drain_settle_s=0.1, drain_timeout_s=5.0)

    async def ask(uid):
        return await svc._rpc_result(None, {"run_uid": uid})

    await svc._handle_bind(_bind("runA"), None)
    stream.visible = 2
    await _until(lambda: len(fake.on(EVT_FRAME)) == 2)

    assert await ask("runA") == {"pending": True, "run_uid": "runA",
                                 "frames_done": 2, "total": 5}

    # Drain: the stop is waiting for the 3 queued frames.
    stop = asyncio.get_running_loop().create_task(
        svc._handle_stop({"run_uid": "runA"}, None))
    await asyncio.sleep(0.2)
    assert not stop.done()
    reply = await ask("runA")
    assert reply["pending"] is True and reply["total"] == 5
    stream.visible = 5
    await stop
    (done,) = fake.on(EVT_COMPLETE)
    assert await ask("runA") == {"result": done}
    assert await ask("ghost") == {"result": None}


async def test_result_request_pending_during_bind(stub_score):
    fake, tiled = FakeNats(), FakeTiled()
    pos = [0.0, 0.5, 1.0]
    staged = FakeTiled()
    _add_run(staged, "late", _parabola_frames(pos), pos)
    svc = _service(fake, tiled, bind_lookup_timeout_s=5.0)
    bind = asyncio.get_running_loop().create_task(svc._handle_bind(_bind("late"), None))
    await asyncio.sleep(0.1)  # bind is retrying the Tiled lookup
    assert await svc._rpc_result(None, {"run_uid": "late"}) == {
        "pending": True, "run_uid": "late", "frames_done": 0, "total": None}
    tiled.runs.update(staged.runs)
    await bind
    await svc.wait_idle()


async def test_results_capped_oldest_evicted(stub_score):
    fake, tiled = FakeNats(), FakeTiled()
    svc = _service(fake, tiled, keep_results=2)
    for uid in ("r1", "r2", "r3"):
        _add_run(tiled, uid, _parabola_frames([0.0, 0.5, 1.0]), [0.0, 0.5, 1.0]).visible = 3
        await svc._handle_bind(_bind(uid), None)
        await svc._handle_stop({"run_uid": uid}, None)
    assert list(svc.results) == ["r2", "r3"]


# ------------------------------------------------------------------ bad binds


async def test_bad_bind_publishes_error_and_recovers(stub_score):
    fake, tiled = FakeNats(), FakeTiled()
    frames = _parabola_frames([0.0, 0.5, 1.0])
    _add_run(tiled, "v99", frames, [0.0, 0.5, 1.0], contract_version=99)
    _add_run(tiled, "noshape", frames, [0.0, 0.5, 1.0])
    del tiled.runs["noshape"].metadata["start"]["rixs"]["frame_shape"]
    svc = _service(fake, tiled)

    bad_bind_version = _bind("v99")
    bad_bind_version["contract_version"] = 99
    # bind-level version 99, start-doc version 99, missing frame_shape, run absent in Tiled
    cases = [bad_bind_version, _bind("v99"), _bind("noshape"), _bind("missing-in-tiled")]
    for i, payload in enumerate(cases, start=1):
        await svc._handle_bind(payload, None)
        errors = fake.on(EVT_ERROR)
        assert len(errors) == i, payload
        assert errors[-1]["run_uid"] == payload["run_uid"] and errors[-1]["error"]
        assert not svc.busy
    assert not fake.on(EVT_FRAME)

    s = _add_run(tiled, "good", frames, [0.0, 0.5, 1.0])
    s.visible = 3
    await svc._handle_bind(_bind("good"), None)
    assert svc.busy
    await svc._handle_stop({"run_uid": "good"}, None)
    assert fake.on(EVT_COMPLETE)[0]["run_uid"] == "good"
    assert len(fake.on(EVT_ERROR)) == 4


async def test_poll_error_publishes_error_and_unbinds(stub_score):
    fake, tiled = FakeNats(), FakeTiled()
    _add_run(tiled, "runA", _parabola_frames([0.0]), [0.0])
    svc = _service(fake, tiled)
    await svc._handle_bind(_bind("runA"), None)

    def boom(start):
        raise RuntimeError("tiled connection dropped")

    svc._current["reader"].read_new_rows = boom
    await _until(lambda: fake.on(EVT_ERROR))
    assert "tiled connection dropped" in fake.on(EVT_ERROR)[-1]["error"]
    await _until(lambda: not svc.busy)
    assert fake.on(EVT_STATUS)[-1]["state"] == "idle"
    await svc._handle_stop({"run_uid": "runA"}, None)  # now unbound: no-op
    assert len(fake.on(EVT_ERROR)) == 1 and not fake.on(EVT_COMPLETE)


async def test_non_rixs_run_ignored():
    fake, tiled = FakeNats(), FakeTiled()
    tiled.runs["plain"] = FakeRun({"uid": "plain", "plan_name": "count"})
    svc = _service(fake, tiled)
    await svc._handle_bind(_bind("plain"), None)
    assert not svc.busy
    assert fake.published == []


async def test_stop_unknown_run_is_noop():
    fake, tiled = FakeNats(), FakeTiled()
    svc = _service(fake, tiled)
    await svc._handle_stop({"run_uid": "ghost"}, None)
    assert fake.published == []


# ---------------------------------------------------------------------- rebind


async def test_rebind_replaces_current(stub_score):
    fake, tiled = FakeNats(), FakeTiled()
    pos = [0.0, 0.5, 1.0]
    a = _add_run(tiled, "runA", _parabola_frames(pos), pos)
    b = _add_run(tiled, "runB", _parabola_frames(pos), pos)
    svc = _service(fake, tiled)
    await svc._handle_bind(_bind("runA"), None)
    a.visible = 1
    await _until(lambda: len(fake.on(EVT_FRAME)) == 1)

    await svc._handle_bind(_bind("runB"), None)  # replaces runA without completing it
    assert svc.busy and svc._current["run_uid"] == "runB"
    a.visible = 3
    await svc._handle_stop({"run_uid": "runA"}, None)  # stale stop: ignored
    assert not fake.on(EVT_COMPLETE)

    b.visible = 3
    await svc._handle_stop({"run_uid": "runB"}, None)
    (done,) = fake.on(EVT_COMPLETE)
    assert done["run_uid"] == "runB"
    assert [m["run_uid"] for m in fake.on(EVT_FRAME)] == ["runA"] + ["runB"] * 3
    assert "runA" not in svc.results


# ------------------------------------------------------------------- discovery


def test_service_info_and_rpcs():
    """meta.info carries what discovery used to; the commands are private rpcs."""
    svc = _service(FakeNats(), FakeTiled(), dispersion_mev_per_px=2.5,
                   nats={"lightfall_prefix": "als.602"})
    host = svc._host
    assert (host.name, host.scope, host.contract) == ("rixs-live", "als.602", "1.0")
    assert host.summary == {"app_name": "rixs-live", "dispersion_mev_per_px": 2.5,
                            "rixs_contract_version": CONTRACT_VERSION}
    assert {RPC_BIND, RPC_STOP, RPC_RESULT} <= set(host._handlers)
    assert host._trust({"app_name": "lightfall"}) is True
    assert host._trust({"app_name": "someone-else"}) is False


def test_explicit_scope_overrides_the_prefix():
    svc = _service(FakeNats(), FakeTiled(),
                   nats={"lightfall_prefix": "als.602", "scope": "als.602.test"})
    assert svc._host.scope == "als.602.test"


async def test_rpcs_answer_at_once_and_run_in_the_background(stub_score):
    pos = [-0.4, 0.0, 0.4]
    fake, tiled = FakeNats(), FakeTiled()
    stream = _add_run(tiled, "runA", _parabola_frames(pos), pos)
    stream.visible = 3
    svc = _service(fake, tiled)
    session = object()
    assert await svc._rpc_bind(session, _bind("runA")) == {"accepted": True}
    assert await svc._rpc_stop(session, {"run_uid": "runA"}) == {"accepted": True}
    await _until(lambda: fake.on(EVT_COMPLETE))
    assert set(fake.sessions_of(EVT_FRAME)) == {session}  # only the binding session
    assert fake.sessions_of(EVT_COMPLETE) == [session]


async def test_rpcs_refuse_a_missing_run_uid():
    from rixs_live.svc.host import ServiceError

    svc = _service(FakeNats(), FakeTiled())
    for rpc in (svc._rpc_bind, svc._rpc_stop, svc._rpc_result):
        with pytest.raises(ServiceError) as info:
            await rpc(None, {})
        assert info.value.code == "bad_request"


async def test_emit_goes_to_live_sessions_only():
    from rixs_live.svc.host import Session

    svc = RixsLiveService(AppConfig(), nats_client=FakeNats())
    sent = []

    async def host_emit(session, name, payload):
        sent.append((session.token, name))

    svc._host.emit = host_emit
    live = Session(token="t1", app_name="lightfall", user=None,
                   delivery_root="_PRIV.x.d.y", last_ping=0.0)
    svc._host._sessions["t1"] = live
    gone = Session(token="t2", app_name="lightfall", user=None,
                   delivery_root="_PRIV.x.d.z", last_ping=0.0)
    await svc._emit(live, EVT_STATUS, {"run_uid": "u"})
    await svc._emit(gone, EVT_STATUS, {"run_uid": "u"})
    await svc._emit(None, EVT_STATUS, {"run_uid": "u"})
    assert sent == [("t1", EVT_STATUS)]


async def test_status_progress(stub_score):
    fake, tiled = FakeNats(), FakeTiled()
    s = _add_run(tiled, "runA", _parabola_frames([0.0, 0.5, 1.0]), [0.0, 0.5, 1.0])
    svc = _service(fake, tiled)
    await svc._handle_bind(_bind("runA"), None)
    assert fake.on(EVT_STATUS)[0] == {"run_uid": "runA", "state": "binding",
                                       "frames_done": 0, "total": 3}
    s.visible = 3
    await _until(lambda: len(fake.on(EVT_FRAME)) == 3)
    await _until(lambda: any(m["state"] == "scoring" and m["frames_done"] == 3
                             for m in fake.on(EVT_STATUS)))
    await svc._handle_stop({"run_uid": "runA"}, None)
    assert fake.on(EVT_STATUS)[-1] == {"run_uid": "runA", "state": "idle",
                                        "frames_done": 3, "total": 3}


# ------------------------------------------------- asynchronous writer (Lightfall)
# Lightfall's ThreadedTiledWriter drains a queue on a background thread, so the
# binder's bind/stop can overtake the docs they refer to.


def _later(delay, fn):
    async def run():
        await asyncio.sleep(delay)
        fn()
    return asyncio.get_running_loop().create_task(run())


async def test_bind_waits_for_run_to_appear(stub_score):
    fake, tiled = FakeNats(), FakeTiled()
    pos = [0.0, 0.5, 1.0]
    staged = FakeTiled()
    stream = _add_run(staged, "late", _parabola_frames(pos), pos)
    svc = _service(fake, tiled, bind_lookup_timeout_s=5.0)
    task = _later(0.4, lambda: tiled.runs.update(staged.runs))  # container lands late
    await svc._handle_bind(_bind("late"), None)
    await task
    assert svc.busy and not fake.on(EVT_ERROR)
    stream.visible = 3
    await svc._handle_stop({"run_uid": "late"}, None)
    (done,) = fake.on(EVT_COMPLETE)
    assert len(done["points"]) == 3 and done["fit"]["ok"]


async def test_bind_gives_up_after_lookup_timeout():
    fake, tiled = FakeNats(), FakeTiled()
    svc = _service(fake, tiled, bind_lookup_timeout_s=0.3)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await svc._handle_bind(_bind("never"), None)
    assert 0.25 <= loop.time() - t0 < 2.0
    (err,) = fake.on(EVT_ERROR)
    assert err["run_uid"] == "never" and not svc.busy


async def test_stop_during_bind_retry_is_deferred(stub_score):
    fake, tiled = FakeNats(), FakeTiled()
    pos = [0.0, 0.5, 1.0]
    staged = FakeTiled()
    stream = _add_run(staged, "late", _parabola_frames(pos), pos)
    stream.visible = 3
    svc = _service(fake, tiled, bind_lookup_timeout_s=5.0)
    bind = asyncio.get_running_loop().create_task(svc._handle_bind(_bind("late"), None))
    await asyncio.sleep(0.1)
    await svc._handle_stop({"run_uid": "late"}, None)  # overtakes the run container
    assert not fake.on(EVT_COMPLETE)
    tiled.runs.update(staged.runs)
    await bind
    (done,) = fake.on(EVT_COMPLETE)
    assert len(done["points"]) == 3 and not svc.busy


async def test_frames_landing_after_stop_are_included(stub_score):
    pos = [-0.8, -0.4, 0.0, 0.4, 0.8]
    fake, tiled = FakeNats(), FakeTiled()
    stream = _add_run(tiled, "runA", _parabola_frames(pos), pos)  # num_points = 5
    svc = _service(fake, tiled, drain_settle_s=0.1, drain_timeout_s=5.0)
    await svc._handle_bind(_bind("runA"), None)
    stream.visible = 2
    await _until(lambda: len(fake.on(EVT_FRAME)) == 2)
    # Known total: the drain waits past drain_settle_s for the queued events.
    task = _later(0.5, lambda: setattr(stream, "visible", 5))
    await svc._handle_stop({"run_uid": "runA"}, None)
    await task
    (done,) = fake.on(EVT_COMPLETE)
    assert [p["position"] for p in done["points"]] == pos
    assert [m["seq_num"] for m in fake.on(EVT_FRAME)] == [1, 2, 3, 4, 5]


async def test_late_frames_unknown_total_settle(stub_score):
    pos = [-0.8, -0.4, 0.0, 0.4, 0.8]
    fake, tiled = FakeNats(), FakeTiled()
    stream = _add_run(tiled, "runA", _parabola_frames(pos), pos)
    del tiled.runs["runA"].metadata["start"]["num_points"]
    svc = _service(fake, tiled, drain_settle_s=1.0, drain_timeout_s=5.0)
    await svc._handle_bind(_bind("runA"), None)
    stream.visible = 2
    await _until(lambda: len(fake.on(EVT_FRAME)) == 2)
    task = _later(0.3, lambda: setattr(stream, "visible", 5))  # within the settle window
    await svc._handle_stop({"run_uid": "runA"}, None)
    await task
    (done,) = fake.on(EVT_COMPLETE)
    assert len(done["points"]) == 5


async def test_drain_capped_when_frames_never_arrive(stub_score):
    pos = [-0.8, -0.4, 0.0, 0.4, 0.8]
    fake, tiled = FakeNats(), FakeTiled()
    stream = _add_run(tiled, "runA", _parabola_frames(pos), pos)  # expects 5
    stream.visible = 3
    svc = _service(fake, tiled, drain_settle_s=0.1, drain_timeout_s=0.5)
    await svc._handle_bind(_bind("runA"), None)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await svc._handle_stop({"run_uid": "runA"}, None)
    assert 0.45 <= loop.time() - t0 < 3.0
    (done,) = fake.on(EVT_COMPLETE)
    assert len(done["points"]) == 3
    assert fake.on(EVT_STATUS)[-1]["state"] == "idle"


async def test_slow_backlog_not_cut_off_by_drain_cap(monkeypatch):
    import time

    def slow(img, d, e):
        time.sleep(0.1)  # scoring a backlog takes longer than the drain cap
        return FrameScore(float(np.mean(img)), None, None, True, None, None, None)

    monkeypatch.setattr("rixs_live.service.score_frame", slow)
    pos = [float(p) for p in np.linspace(-1.0, 1.0, 10)]
    fake, tiled = FakeNats(), FakeTiled()
    stream = _add_run(tiled, "runA", _parabola_frames(pos), pos)
    svc = _service(fake, tiled, poll_interval_s=60.0, drain_timeout_s=0.3)
    await svc._handle_bind(_bind("runA"), None)
    await asyncio.sleep(0.05)
    stream.visible = 5
    # The rest lands while the first five are still being scored (0.5 s > cap 0.3 s).
    task = _later(0.25, lambda: setattr(stream, "visible", 10))
    await svc._handle_stop({"run_uid": "runA"}, None)
    await task
    (done,) = fake.on(EVT_COMPLETE)
    assert len(done["points"]) == 10


def test_credentials_never_from_bind_and_warns_once():
    from loguru import logger
    fake = FakeNats()
    svc = _service(fake, None, tiled={"url": "http://cfg", "api_key": "cfgkey"})
    msgs = []
    sink = logger.add(lambda m: msgs.append(str(m)), level="WARNING")
    try:
        data = {"tiled_url": "http://bind", "tiled_api_key": "leaked"}
        assert svc._resolve_credentials(data) == ("http://bind", "cfgkey")
        assert svc._resolve_credentials(data) == ("http://bind", "cfgkey")
    finally:
        logger.remove(sink)
    assert len([m for m in msgs if "auth handshake" in m]) == 1
    assert "leaked" not in "".join(msgs)


def test_credentials_cached_auth_token_beats_config():
    fake = FakeNats()
    fake.tiled_credentials = {"tiled_url": "http://auth", "tiled_token": "tok"}
    svc = _service(fake, None, tiled={"url": "http://cfg", "api_key": "cfgkey"})
    assert svc._resolve_credentials({"tiled_api_key": "leaked"}) == ("http://auth", "tok")
    assert svc._resolve_credentials({"tiled_url": "http://bind"}) == ("http://bind", "tok")


def test_credentials_config_fallback_and_none():
    svc = _service(FakeNats(), None, tiled={"url": "http://cfg", "api_key": "cfgkey"})
    assert svc._resolve_credentials({}) == ("http://cfg", "cfgkey")
    assert _service(FakeNats(), None)._resolve_credentials({}) == ("", None)


async def test_aborted_run_completes_without_waiting_for_num_points(stub_score):
    pos = [-0.8, -0.4, 0.0, 0.4, 0.8]
    fake, tiled = FakeNats(), FakeTiled()
    stream = _add_run(tiled, "runA", _parabola_frames(pos), pos)  # promises 5
    stream.visible = 2
    svc = _service(fake, tiled, drain_settle_s=0.2, drain_timeout_s=10.0)
    await svc._handle_bind(_bind("runA"), None)
    await _until(lambda: len(fake.on(EVT_FRAME)) == 2)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await svc._handle_stop({"run_uid": "runA", "exit_status": "abort"}, None)
    assert loop.time() - t0 < 3.0
    (done,) = fake.on(EVT_COMPLETE)
    assert len(done["points"]) == 2 and not svc.busy


async def test_aborted_run_still_collects_late_rows_within_settle(stub_score):
    pos = [-0.8, -0.4, 0.0, 0.4, 0.8]
    fake, tiled = FakeNats(), FakeTiled()
    stream = _add_run(tiled, "runA", _parabola_frames(pos), pos)
    stream.visible = 2
    svc = _service(fake, tiled, drain_settle_s=1.0, drain_timeout_s=10.0)
    await svc._handle_bind(_bind("runA"), None)
    await _until(lambda: len(fake.on(EVT_FRAME)) == 2)
    task = _later(0.3, lambda: setattr(stream, "visible", 3))
    await svc._handle_stop({"run_uid": "runA", "exit_status": "fail"}, None)
    await task
    (done,) = fake.on(EVT_COMPLETE)
    assert len(done["points"]) == 3


async def test_stop_num_events_used_as_expected_total(stub_score):
    pos = [-0.8, -0.4, 0.0, 0.4, 0.8]
    fake, tiled = FakeNats(), FakeTiled()
    stream = _add_run(tiled, "runA", _parabola_frames(pos), pos)  # num_points = 5
    stream.visible = 3
    svc = _service(fake, tiled, drain_settle_s=0.1, drain_timeout_s=10.0)
    await svc._handle_bind(_bind("runA"), None)
    await _until(lambda: len(fake.on(EVT_FRAME)) == 3)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    # Aborted after 3 events: num_events wins over num_points, so no waiting for 5.
    await svc._handle_stop({"run_uid": "runA", "exit_status": "abort",
                            "num_events": {"primary": 3}}, None)
    assert loop.time() - t0 < 3.0
    assert len(fake.on(EVT_COMPLETE)[0]["points"]) == 3


async def test_stop_num_events_waits_for_late_rows(stub_score):
    pos = [-0.8, -0.4, 0.0, 0.4, 0.8]
    fake, tiled = FakeNats(), FakeTiled()
    stream = _add_run(tiled, "runA", _parabola_frames(pos), pos)
    del tiled.runs["runA"].metadata["start"]["num_points"]  # unknown from the start doc
    stream.visible = 2
    svc = _service(fake, tiled, drain_settle_s=0.1, drain_timeout_s=5.0)
    await svc._handle_bind(_bind("runA"), None)
    await _until(lambda: len(fake.on(EVT_FRAME)) == 2)
    task = _later(0.6, lambda: setattr(stream, "visible", 4))  # past drain_settle_s
    await svc._handle_stop({"run_uid": "runA", "exit_status": "success",
                            "num_events": {"primary": 4}}, None)
    await task
    assert len(fake.on(EVT_COMPLETE)[0]["points"]) == 4


async def test_old_style_stop_payload_still_waits_for_num_points(stub_score):
    pos = [-0.8, -0.4, 0.0, 0.4, 0.8]
    fake, tiled = FakeNats(), FakeTiled()
    stream = _add_run(tiled, "runA", _parabola_frames(pos), pos)
    stream.visible = 2
    svc = _service(fake, tiled, drain_settle_s=0.1, drain_timeout_s=5.0)
    await svc._handle_bind(_bind("runA"), None)
    await _until(lambda: len(fake.on(EVT_FRAME)) == 2)
    task = _later(0.5, lambda: setattr(stream, "visible", 5))
    await svc._handle_stop({"run_uid": "runA"}, None)  # no exit_status / num_events
    await task
    assert len(fake.on(EVT_COMPLETE)[0]["points"]) == 5


async def test_scrambled_table_rows_give_correct_optimal_position(stub_score):
    """PostgreSQL-style storage order: columns scrambled, array in acquisition order."""
    positions = [-1.0, -0.8, -0.6, -0.4, -0.2, 0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
    n = len(positions)
    order = list(range(2, n + 1, 2)) + list(range(1, n + 1, 2))  # evens then odds
    fake, tiled = FakeNats(), FakeTiled()
    stream = _add_run(tiled, "runS", _parabola_frames(positions, best=0.2), positions,
                      table_order=order)
    svc = _service(fake, tiled)
    stream.visible = n
    await svc._handle_bind(_bind("runS"), None)
    await _until(lambda: len(fake.on(EVT_FRAME)) == n)
    await svc._handle_stop({"run_uid": "runS"}, None)
    await svc.wait_idle()
    msgs = fake.on(EVT_FRAME)
    assert [m["seq_num"] for m in msgs] == list(range(1, n + 1))
    assert np.allclose([m["position"] for m in msgs], positions)
    (done,) = fake.on(EVT_COMPLETE)
    assert done["fit"]["ok"], done["fit"]
    assert abs(done["fit"]["optimal_position"] - 0.2) < 0.05


# ------------------------------------------------------------------ N-D runs


async def test_single_motor_frames_carry_positions(stub_score):
    positions = [0.0, 0.5, 1.0]
    fake, tiled = FakeNats(), FakeTiled()
    stream = _add_run(tiled, "run1", _parabola_frames(positions), positions)
    stream.visible = 3
    svc = _service(fake, tiled)
    await svc._handle_bind(_bind("run1"), None)
    await svc._handle_stop({"run_uid": "run1"}, None)
    assert [f["positions"] for f in fake.on(EVT_FRAME)] == [
        {"sm3_pitch": p} for p in positions]
    (done,) = fake.on(EVT_COMPLETE)
    assert "motor_fields" not in done and "positions" not in done["points"][0]


async def test_image_stream_is_read_instead_of_primary(stub_score):
    """A start doc naming image_stream="frames" reads that stream, not primary."""
    positions = [-0.8, -0.4, 0.0, 0.4, 0.8]
    fake, tiled = FakeNats(), FakeTiled()
    stream = _add_run(tiled, "runF", _parabola_frames(positions), positions,
                      image_stream="frames")
    stream.visible = 5
    svc = _service(fake, tiled)
    await svc._handle_bind(_bind("runF"), None)
    assert svc._current["info"].image_stream == "frames"
    await svc._handle_stop({"run_uid": "runF", "num_events": {"frames": 5, "primary": 5}},
                           None)
    assert [m["seq_num"] for m in fake.on(EVT_FRAME)] == [1, 2, 3, 4, 5]
    (done,) = fake.on(EVT_COMPLETE)
    assert done["fit"]["ok"]  # one motor: still fitted
    assert not fake.on(EVT_ERROR)


async def test_image_stream_ignores_rows_in_primary(stub_score):
    """Only the named stream holds frames: rows in primary are not read."""
    fake, tiled = FakeNats(), FakeTiled()
    stream = _add_run(tiled, "runG", _parabola_frames([0.0, 1.0]), [0.0, 1.0],
                      image_stream="frames")
    stream.visible = 2
    tiled.runs["runG"].stream_name = "primary"  # rows exist, but not in "frames"
    svc = _service(fake, tiled, drain_settle_s=0.05)
    await svc._handle_bind(_bind("runG"), None)
    await svc._handle_stop({"run_uid": "runG", "exit_status": "abort"}, None)
    assert not fake.on(EVT_FRAME)
    (done,) = fake.on(EVT_COMPLETE)
    assert done["points"] == [] and done["best_measured"] is None


async def test_image_stream_num_events_sets_expected_total(stub_score):
    """num_events of the image stream (not primary) is the frame count to wait for."""
    positions = [0.0, 0.5, 1.0]
    fake, tiled = FakeNats(), FakeTiled()
    stream = _add_run(tiled, "runE", _parabola_frames(positions), positions,
                      image_stream="frames")
    stream.visible = 3
    svc = _service(fake, tiled)
    await svc._handle_bind(_bind("runE"), None)
    cur = svc._current
    assert svc._expected_frames(cur, {"num_events": {"frames": 3, "primary": 7}}) == 3


@pytest.mark.parametrize("n_motors", [2, 3])
async def test_multi_motor_run_complete_payload(stub_score, n_motors):
    """N-D run: positions for every motor, no 1D fit, best = max-score point."""
    fields = ["sm3_pitch", "sample_x", "aux"][:n_motors]
    pitch = [100.0, 104.0, 108.0, 102.0, 106.0]
    sx = [-0.9, -0.8, -0.7, -0.85, -0.75]
    aux = [1.0, 2.0, 3.0, 4.0, 5.0]
    widths = [9.0, 4.0, 12.0, 6.0, 7.0]  # best (lowest FWHM) is point 2
    frames = [np.full((8, 8), w) for w in widths]
    extra = {k: v for k, v in {"sample_x": sx, "aux": aux}.items() if k in fields}
    fake, tiled = FakeNats(), FakeTiled()
    stream = _add_run(tiled, "runN", frames, pitch, table_order=[3, 1, 5, 2, 4],
                      extra=extra, image_stream="frames", motor_fields=fields)
    stream.visible = 5
    svc = _service(fake, tiled)
    await svc._handle_bind(_bind("runN"), None)
    await svc._handle_stop({"run_uid": "runN"}, None)
    cols = {"sm3_pitch": pitch, "sample_x": sx, "aux": aux}
    msgs = fake.on(EVT_FRAME)
    assert [m["seq_num"] for m in msgs] == [1, 2, 3, 4, 5]
    for i, m in enumerate(msgs):  # scrambled table: still aligned by seq_num
        assert m["positions"] == {f: pytest.approx(cols[f][i]) for f in fields}
        assert m["position"] == pytest.approx(pitch[i])
        assert m["fwhm_px"] == pytest.approx(widths[i])
    (done,) = fake.on(EVT_COMPLETE)
    assert done["motor_fields"] == fields
    assert done["fit"] == {"ok": False, "optimal_position": None, "coeffs": None,
                           "reason": "multi-motor run: no 1D fit"}
    assert set(done["points"][0]) == {"position", "positions", "fwhm_px", "fwhm_mev",
                                      "resolving_power", "score_valid"}
    assert done["best_measured"] == {
        "position": pytest.approx(104.0), "fwhm_px": pytest.approx(4.0),
        "positions": {f: pytest.approx(cols[f][1]) for f in fields}}
    assert svc.results["runN"] == done and not fake.on(EVT_ERROR)


async def test_multi_motor_missing_column_holds_frames(stub_score):
    """A motor column that is not there yet holds the frames back (KeyError -> retry)."""
    fake, tiled = FakeNats(), FakeTiled()
    stream = _add_run(tiled, "runM", _parabola_frames([0.0, 1.0]), [0.0, 1.0],
                      motor_fields=["sm3_pitch", "sample_x"])
    stream.visible = 2
    svc = _service(fake, tiled, drain_settle_s=0.05)
    await svc._handle_bind(_bind("runM"), None)
    await svc._handle_stop({"run_uid": "runM", "exit_status": "abort"}, None)
    assert not fake.on(EVT_FRAME) and not fake.on(EVT_ERROR)


class _AuthCountingNats(FakeNats):
    """Each handshake hands out the key of the Lightfall session currently running."""

    def __init__(self, key="k1"):
        super().__init__()
        self.key, self.auth_calls = key, 0

    async def authenticate(self):
        self.auth_calls += 1
        self.tiled_credentials = {"tiled_url": "", "tiled_token": self.key}
        return self.tiled_credentials


async def test_bind_from_new_lightfall_session_reauthenticates():
    fake = _AuthCountingNats()
    keys = []
    svc = RixsLiveService(AppConfig(bind_lookup_timeout_s=0.05), nats_client=fake,
                          emit=fake.emit,
                          tiled_connect=lambda url, key, proxy=None: keys.append(key) or FakeTiled())
    await svc._handle_bind({**_bind("r1"), "lightfall_session": "s1"}, None)
    await svc._handle_bind({**_bind("r2"), "lightfall_session": "s1"}, None)
    assert fake.auth_calls == 1  # same session: cached key reused
    fake.key = "k2"  # Lightfall restarted: old key revoked, new session, new key
    await svc._handle_bind({**_bind("r3"), "lightfall_session": "s2"}, None)
    assert fake.auth_calls == 2
    assert keys == ["k1", "k1", "k2"]


async def test_bind_without_session_marker_keeps_cached_key():
    fake = _AuthCountingNats()
    fake.tiled_credentials = {"tiled_url": "", "tiled_token": "old"}
    keys = []
    svc = RixsLiveService(AppConfig(bind_lookup_timeout_s=0.05), nats_client=fake,
                          emit=fake.emit,
                          tiled_connect=lambda url, key, proxy=None: keys.append(key) or FakeTiled())
    await svc._handle_bind(_bind("r1"), None)
    assert fake.auth_calls == 0 and keys == ["old"]


async def test_failed_reauth_retries_on_next_bind():
    fake = _AuthCountingNats()
    ok = False

    async def flaky():
        fake.auth_calls += 1
        return {"tiled_token": "k"} if ok else None
    fake.authenticate = flaky
    svc = RixsLiveService(AppConfig(bind_lookup_timeout_s=0.05), nats_client=fake,
                          emit=fake.emit, tiled_connect=lambda *a, **k: FakeTiled())
    await svc._handle_bind({**_bind("r1"), "lightfall_session": "s1"}, None)
    ok = True
    await svc._handle_bind({**_bind("r2"), "lightfall_session": "s1"}, None)
    await svc._handle_bind({**_bind("r3"), "lightfall_session": "s1"}, None)
    assert fake.auth_calls == 2
