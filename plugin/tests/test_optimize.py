"""qerlin_optimize against a fake tsuchinoko bridge and a fake rixs-live score cache."""
import inspect
import math
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest
from bluesky import RunEngine
from ophyd.sim import SynAxis

from lightfall_endstation_602 import contract as c
from lightfall_endstation_602 import optimize
from lightfall_endstation_602.binder import RixsRunBinder
from lightfall_endstation_602.client import RixsServiceClient
from lightfall_endstation_602.optimize import (
    OptimizeHooks,
    ScoreTimeoutError,
    qerlin_optimize,
    score_of,
)
from lightfall_endstation_602.sim.devices import (
    AxisTracker, SimMarana, SimMonoEnergy, SimSampleX, SimSM3Pitch, get_sim_state,
)
from lightfall_endstation_602.sim.model import FocusModel


class FakeBridge:
    """NATSPlanBridge stand-in: targets are queued up front, publishes recorded."""

    def __init__(self, batches, stops=()):
        self.batches = list(batches)
        self.stops = list(stops)  # tsuchinoko.experiment.stop requests seen on the bus
        self.subscribed = []
        self.published = []
        self.cleaned = False

    def subscribe(self, subject):
        self.subscribed.append(subject)

    def try_get(self, subject):
        if subject == "tsuchinoko.experiment.stop":
            return self.stops.pop(0) if self.stops else None
        assert subject == "tsuchinoko.targets"
        return self.batches.pop(0) if self.batches else None

    def publish(self, subject, payload):
        self.published.append((subject, payload))

    def cleanup(self):
        self.cleaned = True

    def on(self, subject):
        return [p for s, p in self.published if s == subject]


class FakeScores:
    """rixs-live stand-in: scores frame ``seq`` from the motors' positions *now*.

    The plan polls right after reading the frame, so the motors still sit at that
    frame's target; ``missing`` seq_nums never get a score (timeouts).
    """

    def __init__(self, motors, missing=(), invalid=()):
        self.motors = motors
        self.missing = set(missing)
        self.invalid = set(invalid)
        self.asked = []
        self.given = {}

    @staticmethod
    def fwhm(pos):
        return 5.0 + sum((p - c0) ** 2 for p, c0 in zip(pos, (104.0, -0.8, 0.0)))

    def get_score(self, run_uid, seq_num):
        self.asked.append((run_uid, seq_num))
        if seq_num in self.missing:
            return None
        if seq_num not in self.given:
            pos = [float(m.position) for m in self.motors]
            if seq_num in self.invalid:
                self.given[seq_num] = {"run_uid": run_uid, "seq_num": seq_num,
                                       "fwhm_px": None, "score_valid": False}
            else:
                self.given[seq_num] = {"run_uid": run_uid, "seq_num": seq_num,
                                       "fwhm_px": self.fwhm(pos), "score_valid": True}
        return self.given[seq_num]


@pytest.fixture
def rig():
    state = get_sim_state()
    old = state.model, state.axes
    state.model, state.axes = FocusModel(best_pitch=104.0), AxisTracker()
    cam = SimMarana(name="marana", frame_shape=(64, 64))
    sm3 = SimSM3Pitch(name="sm3_pitch")
    sx = SimSampleX(name="sample_x")
    aux = SynAxis(name="aux")
    mono = SimMonoEnergy(name="mono_energy")
    yield SimpleNamespace(cam=cam, motors=[sm3, sx, aux], mono=mono)
    state.model, state.axes = old


def _hooks(bridge, client):
    return OptimizeHooks(
        bridge=bridge, client=client,
        credentials=lambda: ("http://tiled", "secret-key", "socks5://localhost:1080"),
        prefix=lambda: "als.602",
        state=SimpleNamespace(stop_requested=True, pause_requested=True),
    )


def _run(plan):
    docs = []
    RunEngine({})(plan, lambda n, d: docs.append((n, d)))
    return docs


def _events(docs, stream):
    descs = {d["uid"] for n, d in docs if n == "descriptor" and d["name"] == stream}
    return [d for n, d in docs if n == "event" and d["descriptor"] in descs]


TARGETS_3D = [[100.0, -0.9, 0.5], [104.0, -0.8, 0.0], [108.0, -0.7, -0.5],
              [102.0, -0.85, 0.2]]


@pytest.mark.parametrize("n", [1, 2, 3])
def test_optimize_streams_scores_and_handshake(rig, n):
    motors = rig.motors[:n]
    targets = [t[:n] for t in TARGETS_3D]
    bridge = FakeBridge([{"targets": targets[:2], "iteration": 1},
                         {"targets": targets[2:], "iteration": 2}])
    scores = FakeScores(motors)
    hooks = _hooks(bridge, scores)
    docs = _run(qerlin_optimize(rig.cam, motors, rig.mono, exposure_s=0.25, timeout=0.3,
                                score_timeout=1.0, poll_interval=0.01, hooks=hooks))
    names = [m.name for m in motors]

    start = next(d for k, d in docs if k == "start")
    rixs = start[c.MD_KEY]
    assert rixs["image_stream"] == "frames"
    assert rixs["motor_field"] == names[0] and rixs["motor_fields"] == names
    assert rixs["image_field"] == "marana_image" and rixs["frame_shape"] == [64, 64]
    assert rixs["mono_energy_ev"] == 850.0
    assert start["tsuchinoko"]["experiment_id"]
    assert [list(d) for d in start["hints"]["dimensions"]] == [[[f], "primary"] for f in names]
    assert start["plan_name"] == "qerlin_optimize"

    (bind,) = bridge.on("tsuchinoko.experiment.bind_run")
    assert bind == {"run_uid": start["uid"], "tiled_url": "http://tiled",
                    "tiled_api_key": "secret-key", "proxy_url": "socks5://localhost:1080",
                    "lightfall_prefix": "als.602", "motor_names": names,
                    "detector_name": "rixs_score"}
    assert bridge.subscribed == ["tsuchinoko.targets", "tsuchinoko.experiment.stop"]
    assert bridge.cleaned
    assert hooks.state.stop_requested is False  # reset at start

    frames = _events(docs, "frames")
    primary = _events(docs, "primary")
    assert [e["seq_num"] for e in frames] == [1, 2, 3, 4]
    assert [e["seq_num"] for e in primary] == [1, 2, 3, 4]
    for e, t in zip(frames, targets):
        assert {"marana_image", *names} <= set(e["data"])
        assert [e["data"][f] for f in names] == pytest.approx(t)
    for e, t in zip(primary, targets):
        assert "marana_image" not in e["data"]
        assert [e["data"][f] for f in names] == pytest.approx(t)
        fwhm = FakeScores.fwhm(t)
        assert e["data"]["rixs_fwhm_px"] == pytest.approx(fwhm)
        assert e["data"]["rixs_score"] == pytest.approx(optimize.SCORE_SCALE / fwhm)
    assert scores.asked[-1] == (start["uid"], 4)
    assert {uid for uid, _ in scores.asked} == {start["uid"]}

    measured = bridge.on("als.602.adaptive.measured")
    assert measured == [{"iteration": 1, "n_new_points": 1}] * 2 + \
        [{"iteration": 2, "n_new_points": 1}] * 2
    stop = next(d for k, d in docs if k == "stop")
    assert stop["exit_status"] == "success"
    assert stop["num_events"] == {"frames": 4, "primary": 4}
    assert rig.cam.exposure_time.get() == 0.25


def test_invalid_frame_scores_zero(rig):
    motors = rig.motors[:2]
    bridge = FakeBridge([{"targets": [[104.0, -0.8], [100.0, -0.9]]}])
    docs = _run(qerlin_optimize(rig.cam, motors, None, timeout=0.1, score_timeout=1.0,
                                poll_interval=0.01,
                                hooks=_hooks(bridge, FakeScores(motors, invalid={2}))))
    primary = _events(docs, "primary")
    assert primary[0]["data"]["rixs_score"] > 0
    assert primary[1]["data"]["rixs_score"] == 0.0
    assert math.isnan(primary[1]["data"]["rixs_fwhm_px"])
    assert next(d for k, d in docs if k == "start")[c.MD_KEY]["mono_energy_ev"] is None


def test_one_timeout_records_zero_and_continues(rig):
    motors = rig.motors[:2]
    bridge = FakeBridge([{"targets": [[104.0, -0.8], [100.0, -0.9], [108.0, -0.7]]}])
    docs = _run(qerlin_optimize(rig.cam, motors, None, timeout=0.1, score_timeout=0.05,
                                poll_interval=0.01,
                                hooks=_hooks(bridge, FakeScores(motors, missing={2}))))
    scores = [e["data"]["rixs_score"] for e in _events(docs, "primary")]
    assert scores[0] > 0 and scores[1] == 0.0 and scores[2] > 0
    assert len(bridge.on("als.602.adaptive.measured")) == 3
    assert next(d for k, d in docs if k == "stop")["exit_status"] == "success"


def test_three_consecutive_timeouts_abort(rig):
    motors = rig.motors[:2]
    targets = [[100.0 + i, -0.8] for i in range(6)]
    bridge = FakeBridge([{"targets": targets}])
    docs = []
    RE = RunEngine({})
    with pytest.raises(ScoreTimeoutError, match="probably not running"):
        RE(qerlin_optimize(rig.cam, motors, None, timeout=0.1, score_timeout=0.05,
                           poll_interval=0.01,
                           hooks=_hooks(bridge, FakeScores(motors, missing={1, 2, 3, 4}))),
           lambda n, d: docs.append((n, d)))
    primary = _events(docs, "primary")
    assert [e["data"]["rixs_score"] for e in primary] == [0.0, 0.0, 0.0]
    assert len(bridge.on("als.602.adaptive.measured")) == 3
    stop = next(d for k, d in docs if k == "stop")
    assert stop["exit_status"] == "fail" and "probably not running" in stop["reason"]
    assert bridge.cleaned


def test_timeouts_must_be_consecutive(rig):
    motors = rig.motors[:1]
    targets = [[100.0 + i] for i in range(6)]
    bridge = FakeBridge([{"targets": targets}])
    docs = _run(qerlin_optimize(rig.cam, motors, None, timeout=0.1, score_timeout=0.03,
                                poll_interval=0.01,
                                hooks=_hooks(bridge, FakeScores(motors, missing={1, 2, 4, 5}))))
    assert len(_events(docs, "primary")) == 6
    assert next(d for k, d in docs if k == "stop")["exit_status"] == "success"


def test_target_length_mismatch_fails(rig):
    motors = rig.motors[:2]
    bridge = FakeBridge([{"targets": [[104.0]]}])
    with pytest.raises(ValueError, match="2 motors"):
        RunEngine({})(qerlin_optimize(rig.cam, motors, None, timeout=0.1, poll_interval=0.01,
                                      hooks=_hooks(bridge, FakeScores(motors))))


def test_no_motors_rejected(rig):
    with pytest.raises(ValueError):
        next(qerlin_optimize(rig.cam, [], hooks=_hooks(FakeBridge([]), FakeScores([]))))


def test_stop_request_ends_run(rig):
    motors = rig.motors[:1]
    hooks = _hooks(FakeBridge([{"targets": [[104.0], [105.0], [106.0]]}]), None)

    class StopAfterFirst(FakeScores):
        def get_score(self, run_uid, seq_num):
            hooks.state.stop_requested = True  # e.g. the adaptive panel's Stop button
            return super().get_score(run_uid, seq_num)

    hooks.client = StopAfterFirst(motors)
    docs = _run(qerlin_optimize(rig.cam, motors, None, timeout=5.0, poll_interval=0.01,
                                hooks=hooks))
    assert len(_events(docs, "primary")) == 1
    assert next(d for k, d in docs if k == "stop")["exit_status"] == "success"


def test_score_of():
    assert score_of(None)[0] == 0.0 and math.isnan(score_of(None)[1])
    assert optimize.SCORE_SCALE == 100.0
    assert score_of({"fwhm_px": 4.0, "score_valid": True}) == (25.0, 4.0)
    assert score_of({"fwhm_px": 4.0, "score_valid": True}, 1.0) == (0.25, 4.0)
    assert score_of({"fwhm_px": 4.0, "score_valid": False})[0] == 0.0
    assert score_of({"fwhm_px": None, "score_valid": True})[0] == 0.0
    assert score_of({"fwhm_px": 0.0, "score_valid": True})[0] == 0.0
    assert score_of({"fwhm_px": float("nan"), "score_valid": True})[0] == 0.0


def test_binder_binds_optimize_run(rig, fake_ipc):
    """The start doc's rixs key makes the binder bind the run; the stop doc stops it."""
    client = RixsServiceClient(ipc=fake_ipc)
    re = MagicMock()
    binder = RixsRunBinder(client, run_engine_getter=lambda: re,
                           credentials_getter=lambda: ("http://t", None, None),
                           prefix_getter=lambda: "als.602", proxy_getter=lambda url: None)
    binder.enable()
    on_doc = re.subscribe.call_args[0][0]
    motors = rig.motors[:2]
    docs = _run(qerlin_optimize(rig.cam, motors, None, timeout=0.1, poll_interval=0.01,
                                hooks=_hooks(FakeBridge([{"targets": [[104.0, -0.8]]}]),
                                             FakeScores(motors))))
    for name, doc in docs:
        on_doc(name, doc)
    uid = next(d for k, d in docs if k == "start")["uid"]
    subjects = [s for s, _ in fake_ipc.published]
    assert subjects == [c.SUBJ_BIND, c.SUBJ_STOP]
    assert fake_ipc.published[0][1]["run_uid"] == uid
    assert fake_ipc.published[1][1]["num_events"] == {"frames": 1, "primary": 1}


def test_lightfall_hooks_require_enabled_binder(monkeypatch):
    import lightfall.ipc.service as ipc_service

    from lightfall_endstation_602 import runtime
    monkeypatch.setattr(ipc_service, "get_ipc_service", lambda: object())
    monkeypatch.setattr(runtime, "_client", None)
    monkeypatch.setattr(runtime, "_binder", None)
    with pytest.raises(RuntimeError, match="rixs_service_status"):
        optimize.lightfall_hooks()
    monkeypatch.setattr(ipc_service, "get_ipc_service", lambda: None)
    with pytest.raises(RuntimeError, match="NATS"):
        optimize.lightfall_hooks()


def test_lightfall_hooks_wire_lightfall(monkeypatch, fake_ipc):
    import lightfall.ipc.service as ipc_service
    from lightfall.acquire.nats_bridge import NATSPlanBridge
    from lightfall.acquire.plans import adaptive

    from lightfall_endstation_602 import runtime
    client = RixsServiceClient(ipc=fake_ipc)
    monkeypatch.setattr(ipc_service, "get_ipc_service", lambda: fake_ipc)
    monkeypatch.setattr(runtime, "_client", client)
    monkeypatch.setattr(runtime, "_binder", SimpleNamespace(enabled=True))
    hooks = optimize.lightfall_hooks()
    assert isinstance(hooks.bridge, NATSPlanBridge) and hooks.client is client
    fake_ipc.replies["tsuchinoko.status"] = {"status": "ok", "state": "Running"}
    assert hooks.status() == {"status": "ok", "state": "Running"}
    assert fake_ipc.requests[-1] == ("tsuchinoko.status", {})
    assert hooks.state is adaptive._state
    assert hooks.credentials is adaptive._get_tiled_credentials


def test_plan_plugin_registers_optimize():
    from typing import get_args, get_type_hints

    from lightfall.acquire.plan_ui import get_plan_ui_class
    from lightfall.acquire.plans.adaptive import AdaptiveExperimentPanel
    from lightfall.ui.annotations import DeviceFilter

    from lightfall_endstation_602 import plan_plugin
    p = plan_plugin.QerlinOptimizePlanPlugin()
    assert p.name == "qerlin_optimize" and p.category == "rixs"
    fn = p.get_plan_function()
    params = inspect.signature(fn).parameters
    for k in ("marana", "motors", "mono", "exposure_s", "timeout", "score_timeout"):
        assert k in params
    assert params["mono"].default is None
    hints = get_type_hints(fn, include_extras=True)
    assert DeviceFilter(category="motor") in get_args(hints["motors"])[1:]
    assert get_plan_ui_class(fn) is AdaptiveExperimentPanel


def test_frame_seq_matches_point_index_with_real_score_cache(rig, fake_ipc):
    """The real client's cache, filled as rixs-live would (by run uid + seq_num)."""
    client = RixsServiceClient(ipc=fake_ipc)
    motors = rig.motors[:2]
    targets = [[104.0, -0.8], [100.0, -0.9]]
    real = FakeScores(motors)

    class Feeder:
        """Publishes each frame's score into the client the first time it is asked."""

        def get_score(self, run_uid, seq_num):
            if client.get_score(run_uid, seq_num) is None:
                d = real.get_score(run_uid, seq_num)
                fake_ipc.emit(c.SUBJ_FRAME, {**d, "preview": {"b64": "x"}})
            return client.get_score(run_uid, seq_num)

    docs = _run(qerlin_optimize(rig.cam, motors, None, timeout=0.1, poll_interval=0.01,
                                hooks=_hooks(FakeBridge([{"targets": targets}]), Feeder())))
    uid = next(d for k, d in docs if k == "start")["uid"]
    widths = [e["data"]["rixs_fwhm_px"] for e in _events(docs, "primary")]
    assert widths == pytest.approx([FakeScores.fwhm(t) for t in targets])
    assert "preview" not in client.get_score(uid, 1)
    assert np.isfinite(widths).all()



def test_resent_targets_are_not_measured_twice(rig):
    """Live run b69213c0: a slow point made tsuchinoko re-send it; the copy got measured."""
    motors = rig.motors[:2]
    a = [[104.0, -0.8]]
    bridge = FakeBridge([{"targets": a, "iteration": 1}])

    class SlowScores(FakeScores):
        def get_score(self, run_uid, seq_num):
            if seq_num == 1 and not bridge.batches:  # tsuchinoko's 10 s wait ran out twice
                bridge.batches += [{"targets": a, "iteration": 2},
                                   {"targets": a, "iteration": 3}]
            return super().get_score(run_uid, seq_num)

    docs = _run(qerlin_optimize(rig.cam, motors, None, timeout=0.1, score_timeout=1.0,
                                poll_interval=0.01, hooks=_hooks(bridge, SlowScores(motors))))
    assert len(_events(docs, "primary")) == 1
    assert len(bridge.on("als.602.adaptive.measured")) == 1

def test_new_targets_queued_before_measured_are_kept(rig):
    motors = rig.motors[:1]
    bridge = FakeBridge([{"targets": [[104.0]]}])

    class Scores(FakeScores):
        def get_score(self, run_uid, seq_num):
            if seq_num == 1 and not bridge.batches:
                bridge.batches += [{"targets": [[104.0]]}, {"targets": [[106.0]]}]
            return super().get_score(run_uid, seq_num)

    docs = _run(qerlin_optimize(rig.cam, motors, None, timeout=0.1, score_timeout=1.0,
                                poll_interval=0.01, hooks=_hooks(bridge, Scores(motors))))
    assert [e["data"]["sm3_pitch"] for e in _events(docs, "primary")] == [104.0, 106.0]



def _timed(plan):
    import time
    t0 = time.monotonic()
    docs = _run(plan)
    return docs, time.monotonic() - t0


def test_stop_request_on_the_bus_closes_the_run_promptly(rig):
    """tsuchinoko_stop is seen as it is sent; the run does not wait for ``timeout``."""
    motors = rig.motors[:1]
    bridge = FakeBridge([{"targets": [[104.0]]}])

    class StopAfterFirst(FakeScores):
        def get_score(self, run_uid, seq_num):
            bridge.stops.append({})  # the agent calls tsuchinoko_stop
            return super().get_score(run_uid, seq_num)

    docs, dt = _timed(qerlin_optimize(rig.cam, motors, None, timeout=30.0, poll_interval=0.01,
                                      hooks=_hooks(bridge, StopAfterFirst(motors))))
    assert dt < 5.0
    assert len(_events(docs, "primary")) == 1
    assert next(d for k, d in docs if k == "stop")["exit_status"] == "success"


def test_stop_request_mid_batch_skips_the_rest(rig):
    motors = rig.motors[:1]
    bridge = FakeBridge([{"targets": [[104.0], [105.0], [106.0]]}])

    class StopAfterFirst(FakeScores):
        def get_score(self, run_uid, seq_num):
            bridge.stops.append({})
            return super().get_score(run_uid, seq_num)

    docs, dt = _timed(qerlin_optimize(rig.cam, motors, None, timeout=30.0, poll_interval=0.01,
                                      hooks=_hooks(bridge, StopAfterFirst(motors))))
    assert dt < 5.0 and len(_events(docs, "primary")) == 1


@pytest.mark.parametrize("ended", ["Inactive", "Stopping", "Exiting"])
def test_status_inactive_closes_the_run_promptly(rig, ended):
    """tsuchinoko went Inactive by itself (budget reached, stopped from its own UI)."""
    motors = rig.motors[:1]
    bridge = FakeBridge([{"targets": [[104.0]]}])
    hooks = _hooks(bridge, FakeScores(motors))
    hooks.status = lambda: {"status": "ok", "state": ended}
    docs, dt = _timed(qerlin_optimize(rig.cam, motors, None, timeout=30.0, poll_interval=0.01,
                                      status_interval=0.05, hooks=hooks))
    assert dt < 5.0
    assert len(_events(docs, "primary")) == 1
    assert next(d for k, d in docs if k == "stop")["exit_status"] == "success"


def test_inactive_before_the_experiment_started_is_not_its_end(rig):
    """Right after bind_run tsuchinoko may still read Inactive: only ``timeout`` ends that."""
    motors = rig.motors[:1]
    states = iter(["Inactive", "Inactive", "Running"])
    bridge = FakeBridge([])
    hooks = _hooks(bridge, FakeScores(motors))
    asked = []

    def status():
        asked.append(1)
        st = next(states, "Inactive")
        if st == "Running":
            bridge.batches.append({"targets": [[104.0]]})
        return {"status": "ok", "state": st}

    hooks.status = status
    docs, dt = _timed(qerlin_optimize(rig.cam, motors, None, timeout=30.0, poll_interval=0.01,
                                      status_interval=0.05, hooks=hooks))
    assert len(_events(docs, "primary")) == 1  # measured once it started, then ended
    assert len(asked) >= 4 and dt < 5.0


def test_no_status_reply_falls_back_to_timeout(rig):
    motors = rig.motors[:1]
    hooks = _hooks(FakeBridge([{"targets": [[104.0]]}]), FakeScores(motors))
    hooks.status = lambda: None  # tsuchinoko gone
    docs, dt = _timed(qerlin_optimize(rig.cam, motors, None, timeout=0.5, poll_interval=0.01,
                                      status_interval=0.05, hooks=hooks))
    assert dt >= 0.5
    assert next(d for k, d in docs if k == "stop")["exit_status"] == "success"


def test_status_errors_do_not_end_the_run(rig):
    motors = rig.motors[:1]
    hooks = _hooks(FakeBridge([{"targets": [[104.0]]}]), FakeScores(motors))

    def boom():
        raise TimeoutError("no reply")

    hooks.status = boom
    docs, dt = _timed(qerlin_optimize(rig.cam, motors, None, timeout=0.3, poll_interval=0.01,
                                      status_interval=0.05, hooks=hooks))
    assert dt >= 0.3 and len(_events(docs, "primary")) == 1
