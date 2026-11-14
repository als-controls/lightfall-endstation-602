"""E2E: qerlin_optimize (2 motors) + rixs-live over a real nats-server + in-process Tiled.

A fake tsuchinoko publishes the targets, waits for ``adaptive.measured`` and reads
``rixs_score`` back from the run's primary stream the way tsuchinoko's TiledReader
does (``run["primary"].read()``, one row per point, motors by name). The plan runs
in a worker thread with the plugin's real generator; only Lightfall's NATS bridge,
score cache and binder are stand-ins (this venv has no Lightfall).

Full 2048x2048 sim frames so QERLIN's real scorer runs (~1 s per frame).
"""
import asyncio
import json
import queue
import time
from types import SimpleNamespace

import nats as natslib
import numpy as np
import pytest
from bluesky import RunEngine
from bluesky_tiled_plugins import TiledWriter
from lightfall_endstation_602.optimize import OptimizeHooks, qerlin_optimize
from lightfall_endstation_602.sim.devices import (
    AxisTracker, SimMarana, SimSampleX, SimSM3Pitch, get_sim_state,
)
from lightfall_endstation_602.sim.model import FocusModel
from test_e2e_smoke import _nats_binary, live_tiled_app, nats_server  # noqa: F401
from tiled.client import Context, from_context

from rixs_live.config import AppConfig
from rixs_live.contract import (
    CONTRACT_VERSION,
    SUBJ_BIND,
    SUBJ_COMPLETE,
    SUBJ_ERROR,
    SUBJ_FRAME,
    SUBJ_STOP,
)
from rixs_live.service import RixsLiveService

pytestmark = pytest.mark.skipif(_nats_binary() is None, reason="nats-server not found")

PREFIX = "als.602"
FRAME_SHAPE = (2048, 2048)
BATCHES = [
    [[98.0, -0.90], [104.0, -0.80], [110.0, -0.70]],
    [[104.0, -0.85], [101.0, -0.80], [107.0, -0.76]],
]
BEST_TARGET = [104.0, -0.80]
DONE_TIMEOUT_S = 180.0


@pytest.fixture
def sim_2d(monkeypatch):
    state = get_sim_state()
    monkeypatch.setattr(state, "model", FocusModel(best_pitch=104.0, best_sample_x=-0.80))
    monkeypatch.setattr(state, "axes", AxisTracker())
    return state


class ThreadBridge:
    """NATSPlanBridge stand-in usable from the RunEngine thread over a nats-py client."""

    def __init__(self, nc, loop):
        self._nc, self._loop = nc, loop
        self._queues: dict[str, queue.Queue] = {}
        self._subs = []

    def _call(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=10)

    def subscribe(self, subject):
        q: queue.Queue = queue.Queue()
        self._queues[subject] = q

        async def cb(msg):
            q.put(json.loads(msg.data))

        self._subs.append(self._call(self._nc.subscribe(subject, cb=cb)))
        self._call(self._nc.flush())

    def try_get(self, subject):
        try:
            return self._queues[subject].get_nowait()
        except queue.Empty:
            return None

    def publish(self, subject, payload):
        self._call(self._nc.publish(subject, json.dumps(payload).encode()))

    def cleanup(self):
        for sub in self._subs:
            try:
                self._call(sub.unsubscribe())
            except Exception:
                pass


class ScoreCache:
    """RixsServiceClient's score cache, fed from rixs.zeroth.frame (thread-safe dict ops)."""

    def __init__(self):
        self.scores = {}

    async def on_frame(self, msg):
        d = json.loads(msg.data)
        self.scores[(d["run_uid"], int(d["seq_num"]))] = d

    def get_score(self, run_uid, seq_num):
        return self.scores.get((run_uid, int(seq_num)))


def _run_plan(writer_client, bridge, cache, publish, timings):
    """Blocking (worker thread): the optimize plan with a writer and a rixs binder."""
    tw = TiledWriter(writer_client, batch_size=1)
    RE = RunEngine({}, context_managers=[])

    def binder(name, doc):  # after the writer: the doc is already in Tiled
        if name == "start" and "rixs" in doc:
            publish(SUBJ_BIND, {"run_uid": doc["uid"], "tiled_url": "http://in-memory",
                                "lightfall_prefix": PREFIX, "tiled_proxy": None,
                                "contract_version": CONTRACT_VERSION})
        elif name == "stop":
            timings["stop_doc"] = doc
            publish(SUBJ_STOP, {"run_uid": doc["run_start"],
                                "exit_status": doc.get("exit_status"),
                                "num_events": doc.get("num_events")})

    RE.subscribe(tw)
    RE.subscribe(binder)
    sm3 = SimSM3Pitch(name="sm3_pitch")
    sx = SimSampleX(name="sample_x")
    marana = SimMarana(name="marana", frame_shape=FRAME_SHAPE)
    hooks = OptimizeHooks(
        bridge=bridge, client=cache,
        credentials=lambda: ("http://in-memory", None, None),
        prefix=lambda: PREFIX,
        state=SimpleNamespace(stop_requested=False, pause_requested=False))
    (uid,) = RE(qerlin_optimize(marana, [sm3, sx], None, exposure_s=1.0, timeout=3.0,
                                score_timeout=60.0, poll_interval=0.05, hooks=hooks))
    return uid


async def _e2e(nats_url, tiled_app):
    with Context.from_app(tiled_app) as ctx:
        client = from_context(ctx)
        cfg = AppConfig(poll_interval_s=0.1, dispersion_mev_per_px=2.5,
                        tiled={"url": "http://in-memory", "api_key": "k"})
        cfg.nats.url = nats_url
        cfg.nats.lightfall_prefix = PREFIX
        svc = RixsLiveService(cfg, tiled_connect=lambda url, key, proxy=None: client)
        await svc._nats.connect()
        await svc._register_subscriptions()

        loop = asyncio.get_running_loop()
        nc = await natslib.connect(nats_url)        # the plan's (Lightfall's) connection
        tsu = await natslib.connect(nats_url)       # the fake tsuchinoko's
        cache = ScoreCache()
        events = {"frame": [], "complete": [], "error": [], "reads": [], "bind": []}

        def collector(key):
            async def cb(msg):
                events[key].append(json.loads(msg.data))
            return cb

        await nc.subscribe(SUBJ_FRAME, cb=cache.on_frame)
        await nc.subscribe(SUBJ_FRAME, cb=collector("frame"))
        await nc.subscribe(SUBJ_COMPLETE, cb=collector("complete"))
        await nc.subscribe(SUBJ_ERROR, cb=collector("error"))

        # -- fake tsuchinoko: targets out, measured in, TiledReader-style read back.
        tsu_state = {"batch": 0, "measured": 0, "sent": 0}

        async def send_next_batch():
            if tsu_state["batch"] < len(BATCHES):
                batch = BATCHES[tsu_state["batch"]]
                tsu_state["batch"] += 1
                tsu_state["sent"] += len(batch)
                await tsu.publish("tsuchinoko.targets", json.dumps(
                    {"targets": batch, "iteration": tsu_state["batch"]}).encode())

        def tiled_read(run_uid, bind):
            ds = client[run_uid]["primary"].read()
            return {"values": ds[bind["detector_name"]].values.tolist(),
                    "positions": [ds[m].values.tolist() for m in bind["motor_names"]]}

        async def on_bind(msg):
            events["bind"].append(json.loads(msg.data))
            await send_next_batch()

        async def on_measured(msg):
            tsu_state["measured"] += 1
            if tsu_state["measured"] == tsu_state["sent"]:
                bind = events["bind"][-1]
                events["reads"].append(
                    await loop.run_in_executor(None, tiled_read, bind["run_uid"], bind))
                await send_next_batch()

        await tsu.subscribe("tsuchinoko.experiment.bind_run", cb=on_bind)
        await tsu.subscribe(f"{PREFIX}.adaptive.measured", cb=on_measured)
        await nc.flush()
        await tsu.flush()

        def publish(subject, payload):
            asyncio.run_coroutine_threadsafe(
                nc.publish(subject, json.dumps(payload).encode()), loop).result(timeout=10)

        timings = {"t0": time.monotonic()}
        bridge = ThreadBridge(nc, loop)
        uid = await loop.run_in_executor(None, _run_plan, client, bridge, cache, publish,
                                         timings)
        timings["plan_done"] = time.monotonic()
        deadline = time.monotonic() + DONE_TIMEOUT_S
        while time.monotonic() < deadline and not events["complete"] and not events["error"]:
            await asyncio.sleep(0.1)
        primary = await loop.run_in_executor(None, lambda: client[uid]["primary"].read())
        await tsu.drain()
        await nc.drain()
        await svc._nats.close()
    return uid, events, timings, primary


def test_e2e_optimize_2d(nats_server, live_tiled_app, sim_2d):  # noqa: F811
    uid, events, timings, primary = asyncio.run(_e2e(nats_server, live_tiled_app))
    targets = [t for b in BATCHES for t in b]
    n = len(targets)

    assert not events["error"], events["error"]
    assert timings["stop_doc"]["exit_status"] == "success", timings["stop_doc"]
    (bind,) = events["bind"]
    assert bind["run_uid"] == uid and bind["detector_name"] == "rixs_score"
    assert bind["motor_names"] == ["sm3_pitch", "sample_x"]

    frames = sorted(events["frame"], key=lambda f: f["seq_num"])
    assert [f["seq_num"] for f in frames] == list(range(1, n + 1))
    for f, t in zip(frames, targets):
        assert [f["positions"]["sm3_pitch"], f["positions"]["sample_x"]] == pytest.approx(t)
        assert f["score_valid"], f

    # What tsuchinoko read after each batch: aligned rows, scores = 1/FWHM of that frame.
    assert [len(r["values"]) for r in events["reads"]] == [3, 6]
    last = events["reads"][-1]
    assert np.allclose(np.array(last["positions"]).T, targets)
    want = [1.0 / f["fwhm_px"] for f in frames]
    assert last["values"] == pytest.approx(want)
    assert np.allclose(primary["rixs_fwhm_px"].values, [f["fwhm_px"] for f in frames])
    assert targets[int(np.argmax(last["values"]))] == BEST_TARGET

    (done,) = events["complete"]
    assert done["run_uid"] == uid and done["motor_fields"] == ["sm3_pitch", "sample_x"]
    assert done["fit"]["ok"] is False and done["fit"]["reason"] == "multi-motor run: no 1D fit"
    best = done["best_measured"]["positions"]
    assert [best["sm3_pitch"], best["sample_x"]] == pytest.approx(BEST_TARGET)
    print(f"\ne2e optimize: plan {timings['plan_done'] - timings['t0']:.1f} s for {n} "
          f"points; best FWHM {done['best_measured']['fwhm_px']:.2f} px at {best}; "
          f"scores {[round(v, 4) for v in last['values']]}")
