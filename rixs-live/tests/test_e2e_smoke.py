"""E2E: RunEngine + sim devices -> TiledWriter -> in-memory Tiled; service over a real nats-server.

Lightfall's side is a private session (``svc_session.SvcSession``): bind/stop are rpcs and
the frame/complete/error events arrive on that session only.

Full 2048x2048 frames: QERLIN's line finder rejects broad (defocused) lines on
512x512 frames (see the Task 4 report), so smaller frames would not exercise
the real fit. Scoring is ~1 s per frame on CPU, hence the generous timeout.
"""
import asyncio
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import bluesky.plans as bp
import pytest
from bluesky import RunEngine
from bluesky_tiled_plugins import TiledWriter
from lightfall_endstation_602.contract import build_rixs_md
from lightfall_endstation_602.sim.devices import (
    AxisTracker, SimMarana, SimSM3Pitch, get_sim_state,
)
from lightfall_endstation_602.sim.model import FocusModel
from tiled.catalog import from_uri as catalog_from_uri
from tiled.client import Context, from_context
from tiled.server.app import build_app

from rixs_live.config import AppConfig
from rixs_live.contract import (
    CONTRACT_VERSION,
    EVT_COMPLETE,
    EVT_ERROR,
    EVT_FRAME,
    RPC_BIND,
    RPC_STOP,
)
from rixs_live.service import RixsLiveService
from svc_session import SvcSession

BEST = 104.0  # hidden best SM3 pitch (native units), fixed via an explicit FocusModel
FRAME_SHAPE = (2048, 2048)
COMPLETE_TIMEOUT_S = 120.0


def _nats_binary():
    exe = Path(sys.executable).parent / (
        "nats-server.exe" if sys.platform == "win32" else "nats-server")
    if exe.exists():
        return str(exe)
    return shutil.which("nats-server")


pytestmark = pytest.mark.skipif(_nats_binary() is None, reason="nats-server not found")


async def start_service(svc):
    """What RixsLiveService._main does before it idles: connect, then serve."""
    await svc._nats.connect()
    await svc._host.start(svc._nats.core)
    svc._host.set_state(state="ready")


async def stop_service(svc):
    await svc._host.stop()
    await svc._nats.close()


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def nats_server():
    port = _free_port()
    proc = subprocess.Popen([_nats_binary(), "-a", "127.0.0.1", "-p", str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    else:
        proc.kill()
        pytest.fail("nats-server did not become ready")
    yield f"nats://127.0.0.1:{port}"
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture
def live_tiled_app(tmp_path):
    """Tiled app whose catalog is a SQLite *file*, not ``:memory:``.

    The shared ``tiled_app`` fixture uses ``tiled.catalog.in_memory``, whose single
    in-memory connection cannot take the service's polling reads interleaved with
    TiledWriter's writes (``Could not refresh instance '<Node ...>'`` /
    FOREIGN KEY failures). Here the writes and reads really are concurrent.
    """
    catalog = catalog_from_uri(
        f"sqlite:///{(tmp_path / 'catalog.db').as_posix()}",
        writable_storage=[str(tmp_path / "data"),
                          f"sqlite:///{(tmp_path / 'internal.db').as_posix()}"],
        init_if_not_exists=True)
    return build_app(catalog)


@pytest.fixture
def sim_model(monkeypatch):
    monkeypatch.setenv("LIGHTFALL_SIM_SEED", "1")
    state = get_sim_state()
    monkeypatch.setattr(state, "model", FocusModel(best_pitch=BEST))
    monkeypatch.setattr(state, "axes", AxisTracker())  # no Sample X: at its best
    return state.model


class QueuedWriter:
    """Stand-in for Lightfall's ThreadedTiledWriter: docs go through a queue that a
    background thread drains, with a delay, so bind/stop overtake the docs."""

    def __init__(self, writer, delay_s=0.3):
        self._writer, self._delay = writer, delay_s
        self._q: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def __call__(self, name, doc):
        self._q.put((name, doc))

    def _drain(self):
        while (item := self._q.get()) is not None:
            time.sleep(self._delay)
            self._writer(*item)

    def close(self):
        self._q.put(None)
        self._thread.join(timeout=60)


def _run_scan(writer_client, loop, session, timings, async_writer=False):
    """Blocking: run the scan in this (worker) thread; bind/stop are session rpcs."""
    tw = TiledWriter(writer_client, batch_size=1)
    if async_writer:
        tw = QueuedWriter(tw)
    RE = RunEngine({}, context_managers=[])  # not the main thread: no signal handlers

    def publish(action, payload):
        fut = asyncio.run_coroutine_threadsafe(session.call(action, payload), loop)
        reply = fut.result(timeout=10)
        assert reply.get("status") == "ok" and reply.get("accepted"), reply

    def binder(name, doc):  # subscribed after the writer: the doc is already in Tiled
        if name == "start":
            publish(RPC_BIND, {"run_uid": doc["uid"], "tiled_url": "http://in-memory",
                               "tiled_proxy": None, "contract_version": CONTRACT_VERSION})
        elif name == "stop":
            timings["scan_done"] = time.monotonic()
            publish(RPC_STOP, {"run_uid": doc["run_start"]})

    if async_writer:
        RE.subscribe(binder)  # worst case: bind/stop go out before the writer sees the doc
        RE.subscribe(tw)
    else:
        RE.subscribe(tw)
        RE.subscribe(binder)
    sm3 = SimSM3Pitch(name="sm3_pitch")
    marana = SimMarana(name="marana", frame_shape=FRAME_SHAPE)
    md = {"rixs": build_rixs_md("marana_image", "sm3_pitch", FRAME_SHAPE, 850.0)}
    (uid,) = RE(bp.scan([marana], sm3, BEST - 6.0, BEST + 6.0, 7, md=md))
    if async_writer:
        tw.close()  # off the event loop: the service keeps working while this drains
    return uid


async def _smoke(nats_url, tiled_app, async_writer):
    # One Context: a second Context.from_app on the same app runs the catalog on a
    # second event loop and corrupts its SQLite writes (FOREIGN KEY failures).
    with Context.from_app(tiled_app) as ctx:
        svc_client = writer_client = from_context(ctx)
        cfg = AppConfig(poll_interval_s=0.1, dispersion_mev_per_px=2.5,
                        tiled={"url": "http://in-memory", "api_key": "k"})
        cfg.nats.url = nats_url
        svc = RixsLiveService(cfg, tiled_connect=lambda url, key, proxy=None: svc_client)
        await start_service(svc)
        session = SvcSession(nats_url, svc.scope)
        info = await session.start()
        assert info["name"] == "rixs-live" and info["summary"]["dispersion_mev_per_px"] == 2.5
        # A second connection that never opened a session sees none of the run's events.
        bystander = SvcSession(nats_url, svc.scope, app_name="bystander")

        loop = asyncio.get_running_loop()
        timings = {"t0": time.monotonic()}
        uid = await loop.run_in_executor(None, _run_scan, writer_client, loop, session,
                                         timings, async_writer)

        deadline = time.monotonic() + COMPLETE_TIMEOUT_S
        while (time.monotonic() < deadline and not session.on(EVT_COMPLETE)
               and not session.on(EVT_ERROR)):
            await asyncio.sleep(0.1)
        timings["complete"] = time.monotonic()
        with pytest.raises(PermissionError):  # an untrusted app is refused a session
            await bystander.start()
        assert bystander.events == []
        await bystander.close()
        result = await session.call("result.get", {"run_uid": uid})
        events = {"frame": session.on(EVT_FRAME), "complete": session.on(EVT_COMPLETE),
                  "error": session.on(EVT_ERROR)}
        await session.close()
        await stop_service(svc)

    assert not events["error"], events["error"]
    assert events["complete"], f"no {EVT_COMPLETE} within {COMPLETE_TIMEOUT_S} s"
    done = events["complete"][0]
    assert result["status"] == "ok"
    assert {k: v for k, v in result["result"].items()} == {
        k: v for k, v in done.items() if k not in ("instance_id", "seq")}
    assert done["run_uid"] == uid and done["motor_field"] == "sm3_pitch"
    assert [f["seq_num"] for f in events["frame"]] == [1, 2, 3, 4, 5, 6, 7]
    assert done["fit"]["ok"], done["fit"]
    assert abs(done["fit"]["optimal_position"] - BEST) < 1.5, done["fit"]  # lopsided
    print(f"\ne2e timing: scan {timings['scan_done'] - timings['t0']:.1f} s, "
          f"bind->complete {timings['complete'] - timings['t0']:.1f} s, "
          f"complete after stop {timings['complete'] - timings['scan_done']:.1f} s; "
          f"fit optimum {done['fit']['optimal_position']:.3f} (best {BEST})")


@pytest.mark.parametrize("async_writer", [False, True], ids=["sync-writer", "queued-writer"])
def test_e2e_smoke(nats_server, live_tiled_app, sim_model, async_writer):
    asyncio.run(_smoke(nats_server, live_tiled_app, async_writer))
