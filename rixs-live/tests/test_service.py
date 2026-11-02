"""RixsLiveService against a fake NATS client and a fake, growing Tiled run."""
import asyncio

import numpy as np
import pytest
from lightfall_endstation_602.sim.model import FocusModel, render_frame

from rixs_live.config import AppConfig
from rixs_live.contract import (
    SUBJ_BIND,
    SUBJ_COMPLETE,
    SUBJ_DISCOVER,
    SUBJ_ERROR,
    SUBJ_FRAME,
    SUBJ_RESULT,
    SUBJ_STATUS,
    SUBJ_STOP,
)
from rixs_live.focus import FrameScore
from rixs_live.service import RixsLiveService

FRAME_KEYS = {"run_uid", "seq_num", "position", "fwhm_px", "fwhm_mev", "resolving_power",
              "score_valid", "failure_reason", "centroid", "angle_deg", "preview"}


class FakeNats:
    def __init__(self):
        self.published = []      # (subject, payload)
        self.subscriptions = {}  # subject -> cb
        self.tiled_credentials = None

    async def connect(self): ...
    async def close(self): ...
    async def authenticate(self): return None

    async def publish(self, subject, payload):
        self.published.append((subject, payload))

    async def subscribe(self, subject, cb):
        self.subscriptions[subject] = cb

    async def reply(self, msg, payload):
        self.published.append((getattr(msg, "reply", "?"), payload))

    def on(self, subject):
        return [p for s, p in self.published if s == subject]


class Msg:
    def __init__(self, reply="inbox1"):
        self.reply = reply


class FakeStream:
    """A primary stream whose rows become visible as the test reveals them."""

    def __init__(self, frames, positions):
        self._cols = {
            "marana_image": np.asarray(frames, dtype=float),
            "sm3_pitch": np.asarray(positions, dtype=float),
            "sm3_pitch_setpoint": np.asarray(positions, dtype=float) + 99.0,  # decoy
            "seq_num": np.arange(1, len(positions) + 1),
        }
        self.visible = 0

    def __iter__(self):
        return iter(self._cols)

    def __contains__(self, key):
        return key in self._cols

    def __getitem__(self, key):
        return self._cols[key][: self.visible]


class FakeRun:
    def __init__(self, start, stream=None):
        self.metadata = {"start": start}
        self.stream = stream

    def __getitem__(self, key):
        if key != "primary" or self.stream is None or self.stream.visible == 0:
            raise KeyError(key)
        return self.stream


class FakeTiled:
    def __init__(self):
        self.runs = {}

    def __getitem__(self, uid):
        return self.runs[uid]  # KeyError for unknown runs, like a Tiled container


def _rixs_md(shape, **over):
    md = {"contract_version": 1, "kind": "zeroth_order_focus",
          "image_field": "marana_image", "motor_field": "sm3_pitch",
          "frame_shape": list(shape), "mono_energy_ev": 850.0}
    md.update(over)
    return md


def _add_run(tiled, uid, frames, positions, **md_over):
    shape = np.asarray(frames).shape[1:]
    start = {"uid": uid, "plan_name": "scan", "num_points": len(positions),
             "rixs": _rixs_md(shape, **md_over)}
    stream = FakeStream(frames, positions)
    tiled.runs[uid] = FakeRun(start, stream)
    return stream


def _service(fake, tiled_fake, **cfg_over):
    cfg = AppConfig(**{"poll_interval_s": 0.01, "bind_lookup_timeout_s": 0.3,
                       "drain_settle_s": 0.2, "drain_timeout_s": 2.0, **cfg_over})
    return RixsLiveService(cfg, nats_client=fake, tiled_connect=lambda url, key: tiled_fake)


def _bind(uid):
    return {"run_uid": uid, "tiled_url": "http://local",
            "lightfall_prefix": "als.7011", "contract_version": 1}


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
    best = 0.25
    model = FocusModel(best_pitch=best)
    positions = [float(p) for p in np.linspace(best - 1.0, best + 1.0, 7)]
    rng = np.random.default_rng(7)
    frames = [render_frame(model, p, 1.0, shape=(2048, 2048), rng=rng) for p in positions]
    fake, tiled = FakeNats(), FakeTiled()
    stream = _add_run(tiled, "runA", frames, positions)
    svc = _service(fake, tiled, dispersion_mev_per_px=2.5)

    await svc._handle_bind(_bind("runA"), None)
    assert svc.busy
    stream.visible = 4
    await _until(lambda: len(fake.on(SUBJ_FRAME)) == 4, timeout=60)
    stream.visible = 7
    await _until(lambda: len(fake.on(SUBJ_FRAME)) == 7, timeout=60)
    await svc._handle_stop({"run_uid": "runA"}, None)
    await svc.wait_idle()

    msgs = fake.on(SUBJ_FRAME)
    assert [m["seq_num"] for m in msgs] == [1, 2, 3, 4, 5, 6, 7]
    assert all(set(m) == FRAME_KEYS for m in msgs)
    assert np.allclose([m["position"] for m in msgs], positions)  # not the setpoint decoy
    assert all(m["score_valid"] for m in msgs)
    assert msgs[3]["fwhm_mev"] == pytest.approx(msgs[3]["fwhm_px"] * 2.5)
    assert msgs[3]["resolving_power"] is not None
    assert set(msgs[0]["preview"]) == {"b64", "shape", "scale"}

    (done,) = fake.on(SUBJ_COMPLETE)
    assert done["run_uid"] == "runA" and done["motor_field"] == "sm3_pitch"
    assert done["fit"]["ok"], done["fit"]
    assert abs(done["fit"]["optimal_position"] - best) < 0.3
    assert len(done["points"]) == 7
    assert set(done["points"][0]) == {"position", "fwhm_px", "fwhm_mev",
                                      "resolving_power", "score_valid"}
    assert done["best_measured"]["position"] == pytest.approx(positions[3])
    assert svc.results["runA"] == done
    assert fake.on(SUBJ_STATUS)[-1]["state"] == "idle"
    assert fake.on(SUBJ_STATUS)[-1]["frames_done"] == 7
    assert not svc.busy and not fake.on(SUBJ_ERROR)


async def test_stop_drains_frames_not_yet_polled(stub_score):
    positions = [-0.8, -0.4, 0.0, 0.4, 0.8]
    fake, tiled = FakeNats(), FakeTiled()
    stream = _add_run(tiled, "runA", _parabola_frames(positions), positions)
    svc = _service(fake, tiled, poll_interval_s=60.0)  # poll loop sleeps through the test
    await svc._handle_bind(_bind("runA"), None)
    await asyncio.sleep(0.05)
    stream.visible = 5
    await svc._handle_stop({"run_uid": "runA"}, None)
    assert [m["seq_num"] for m in fake.on(SUBJ_FRAME)] == [1, 2, 3, 4, 5]
    (done,) = fake.on(SUBJ_COMPLETE)
    assert done["fit"]["ok"] and abs(done["fit"]["optimal_position"] - 0.2) < 1e-6


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
    (done,) = fake.on(SUBJ_COMPLETE)
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
    (done,) = fake.on(SUBJ_COMPLETE)
    assert done["best_measured"] is None
    assert not done["fit"]["ok"]


# --------------------------------------------------------------------- results


async def test_result_request_returns_stored(stub_score):
    positions = [-0.5, 0.0, 0.5, 1.0]
    fake, tiled = FakeNats(), FakeTiled()
    stream = _add_run(tiled, "runA", _parabola_frames(positions), positions)
    stream.visible = 4
    svc = _service(fake, tiled)
    await svc._register_subscriptions()
    await svc._handle_bind(_bind("runA"), None)
    await svc._handle_stop({"run_uid": "runA"}, None)
    (done,) = fake.on(SUBJ_COMPLETE)

    await fake.subscriptions[SUBJ_RESULT]({"run_uid": "runA"}, Msg("r1"))
    assert fake.on("r1") == [done]
    await fake.subscriptions[SUBJ_RESULT]({"run_uid": "ghost"}, Msg("r2"))
    assert fake.on("r2") == [{"error": "unknown run ghost"}]


async def test_result_request_pending_while_bound_and_draining(stub_score):
    """A result request for the bound run says pending (not unknown) until complete."""
    pos = [-0.8, -0.4, 0.0, 0.4, 0.8]
    fake, tiled = FakeNats(), FakeTiled()
    stream = _add_run(tiled, "runA", _parabola_frames(pos), pos)  # num_points = 5
    svc = _service(fake, tiled, drain_settle_s=0.1, drain_timeout_s=5.0)
    await svc._register_subscriptions()
    ask = fake.subscriptions[SUBJ_RESULT]
    await svc._handle_bind(_bind("runA"), None)
    stream.visible = 2
    await _until(lambda: len(fake.on(SUBJ_FRAME)) == 2)

    await ask({"run_uid": "runA"}, Msg("bound"))
    assert fake.on("bound") == [{"pending": True, "run_uid": "runA",
                                 "frames_done": 2, "total": 5}]

    # Drain: the stop is waiting for the 3 queued frames.
    stop = asyncio.get_running_loop().create_task(
        svc._handle_stop({"run_uid": "runA"}, None))
    await asyncio.sleep(0.2)
    assert not stop.done()
    await ask({"run_uid": "runA"}, Msg("draining"))
    (reply,) = fake.on("draining")
    assert reply["pending"] is True and reply["total"] == 5
    stream.visible = 5
    await stop
    (done,) = fake.on(SUBJ_COMPLETE)
    await ask({"run_uid": "runA"}, Msg("after"))
    assert fake.on("after") == [done]
    await ask({"run_uid": "ghost"}, Msg("ghost"))
    assert fake.on("ghost") == [{"error": "unknown run ghost"}]


async def test_result_request_pending_during_bind(stub_score):
    fake, tiled = FakeNats(), FakeTiled()
    pos = [0.0, 0.5, 1.0]
    staged = FakeTiled()
    _add_run(staged, "late", _parabola_frames(pos), pos)
    svc = _service(fake, tiled, bind_lookup_timeout_s=5.0)
    await svc._register_subscriptions()
    bind = asyncio.get_running_loop().create_task(svc._handle_bind(_bind("late"), None))
    await asyncio.sleep(0.1)  # bind is retrying the Tiled lookup
    await fake.subscriptions[SUBJ_RESULT]({"run_uid": "late"}, Msg("r"))
    assert fake.on("r") == [{"pending": True, "run_uid": "late",
                             "frames_done": 0, "total": None}]
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
        errors = fake.on(SUBJ_ERROR)
        assert len(errors) == i, payload
        assert errors[-1]["run_uid"] == payload["run_uid"] and errors[-1]["error"]
        assert not svc.busy
    assert not fake.on(SUBJ_FRAME)

    s = _add_run(tiled, "good", frames, [0.0, 0.5, 1.0])
    s.visible = 3
    await svc._handle_bind(_bind("good"), None)
    assert svc.busy
    await svc._handle_stop({"run_uid": "good"}, None)
    assert fake.on(SUBJ_COMPLETE)[0]["run_uid"] == "good"
    assert len(fake.on(SUBJ_ERROR)) == 4


async def test_poll_error_publishes_error_and_unbinds(stub_score):
    fake, tiled = FakeNats(), FakeTiled()
    _add_run(tiled, "runA", _parabola_frames([0.0]), [0.0])
    svc = _service(fake, tiled)
    await svc._handle_bind(_bind("runA"), None)

    def boom(start):
        raise RuntimeError("tiled connection dropped")

    svc._current["reader"].read_new = boom
    await _until(lambda: fake.on(SUBJ_ERROR))
    assert "tiled connection dropped" in fake.on(SUBJ_ERROR)[-1]["error"]
    await _until(lambda: not svc.busy)
    assert fake.on(SUBJ_STATUS)[-1]["state"] == "idle"
    await svc._handle_stop({"run_uid": "runA"}, None)  # now unbound: no-op
    assert len(fake.on(SUBJ_ERROR)) == 1 and not fake.on(SUBJ_COMPLETE)


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
    await _until(lambda: len(fake.on(SUBJ_FRAME)) == 1)

    await svc._handle_bind(_bind("runB"), None)  # replaces runA without completing it
    assert svc.busy and svc._current["run_uid"] == "runB"
    a.visible = 3
    await svc._handle_stop({"run_uid": "runA"}, None)  # stale stop: ignored
    assert not fake.on(SUBJ_COMPLETE)

    b.visible = 3
    await svc._handle_stop({"run_uid": "runB"}, None)
    (done,) = fake.on(SUBJ_COMPLETE)
    assert done["run_uid"] == "runB"
    assert [m["run_uid"] for m in fake.on(SUBJ_FRAME)] == ["runA"] + ["runB"] * 3
    assert "runA" not in svc.results


# ------------------------------------------------------------------- discovery


async def test_discovery_reply():
    fake, tiled = FakeNats(), FakeTiled()
    svc = _service(fake, tiled, dispersion_mev_per_px=2.5)
    await svc._register_subscriptions()
    assert {SUBJ_BIND, SUBJ_STOP, SUBJ_RESULT, SUBJ_DISCOVER,
            "rixs.meta.actions", "rixs.meta.events"} <= set(fake.subscriptions)
    for i, subj in enumerate((SUBJ_DISCOVER, "rixs.meta.actions", "rixs.meta.events")):
        await fake.subscriptions[subj]({}, Msg(f"d{i}"))
        (reply,) = fake.on(f"d{i}")
        assert reply == {"app_name": "rixs-live", "app_version": "",
                         "actions": [SUBJ_RESULT],
                         "events": [SUBJ_STATUS, SUBJ_ERROR, SUBJ_FRAME, SUBJ_COMPLETE],
                         "dispersion_mev_per_px": 2.5}


async def test_status_progress(stub_score):
    fake, tiled = FakeNats(), FakeTiled()
    s = _add_run(tiled, "runA", _parabola_frames([0.0, 0.5, 1.0]), [0.0, 0.5, 1.0])
    svc = _service(fake, tiled)
    await svc._handle_bind(_bind("runA"), None)
    assert fake.on(SUBJ_STATUS)[0] == {"run_uid": "runA", "state": "binding",
                                       "frames_done": 0, "total": 3}
    s.visible = 3
    await _until(lambda: len(fake.on(SUBJ_FRAME)) == 3)
    await _until(lambda: any(m["state"] == "scoring" and m["frames_done"] == 3
                             for m in fake.on(SUBJ_STATUS)))
    await svc._handle_stop({"run_uid": "runA"}, None)
    assert fake.on(SUBJ_STATUS)[-1] == {"run_uid": "runA", "state": "idle",
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
    assert svc.busy and not fake.on(SUBJ_ERROR)
    stream.visible = 3
    await svc._handle_stop({"run_uid": "late"}, None)
    (done,) = fake.on(SUBJ_COMPLETE)
    assert len(done["points"]) == 3 and done["fit"]["ok"]


async def test_bind_gives_up_after_lookup_timeout():
    fake, tiled = FakeNats(), FakeTiled()
    svc = _service(fake, tiled, bind_lookup_timeout_s=0.3)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await svc._handle_bind(_bind("never"), None)
    assert 0.25 <= loop.time() - t0 < 2.0
    (err,) = fake.on(SUBJ_ERROR)
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
    assert not fake.on(SUBJ_COMPLETE)
    tiled.runs.update(staged.runs)
    await bind
    (done,) = fake.on(SUBJ_COMPLETE)
    assert len(done["points"]) == 3 and not svc.busy


async def test_frames_landing_after_stop_are_included(stub_score):
    pos = [-0.8, -0.4, 0.0, 0.4, 0.8]
    fake, tiled = FakeNats(), FakeTiled()
    stream = _add_run(tiled, "runA", _parabola_frames(pos), pos)  # num_points = 5
    svc = _service(fake, tiled, drain_settle_s=0.1, drain_timeout_s=5.0)
    await svc._handle_bind(_bind("runA"), None)
    stream.visible = 2
    await _until(lambda: len(fake.on(SUBJ_FRAME)) == 2)
    # Known total: the drain waits past drain_settle_s for the queued events.
    task = _later(0.5, lambda: setattr(stream, "visible", 5))
    await svc._handle_stop({"run_uid": "runA"}, None)
    await task
    (done,) = fake.on(SUBJ_COMPLETE)
    assert [p["position"] for p in done["points"]] == pos
    assert [m["seq_num"] for m in fake.on(SUBJ_FRAME)] == [1, 2, 3, 4, 5]


async def test_late_frames_unknown_total_settle(stub_score):
    pos = [-0.8, -0.4, 0.0, 0.4, 0.8]
    fake, tiled = FakeNats(), FakeTiled()
    stream = _add_run(tiled, "runA", _parabola_frames(pos), pos)
    del tiled.runs["runA"].metadata["start"]["num_points"]
    svc = _service(fake, tiled, drain_settle_s=1.0, drain_timeout_s=5.0)
    await svc._handle_bind(_bind("runA"), None)
    stream.visible = 2
    await _until(lambda: len(fake.on(SUBJ_FRAME)) == 2)
    task = _later(0.3, lambda: setattr(stream, "visible", 5))  # within the settle window
    await svc._handle_stop({"run_uid": "runA"}, None)
    await task
    (done,) = fake.on(SUBJ_COMPLETE)
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
    (done,) = fake.on(SUBJ_COMPLETE)
    assert len(done["points"]) == 3
    assert fake.on(SUBJ_STATUS)[-1]["state"] == "idle"


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
    (done,) = fake.on(SUBJ_COMPLETE)
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
    assert len([m for m in msgs if "public subjects" in m]) == 1
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
