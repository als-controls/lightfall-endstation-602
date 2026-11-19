"""``qerlin_optimize``: N-D spectrometer focusing driven by tsuchinoko/gpCAM.

The plan speaks tsuchinoko's adaptive handshake (the same one as Lightfall's
``adaptive_experiment``: ``bind_run``, ``tsuchinoko.targets``,
``{prefix}.adaptive.measured``), but its "detector" is rixs-live's score of each
Marana frame. Per target it:

1. moves the motors;
2. reads ``[marana] + motors`` into the ``frames`` stream (rixs-live binds the run,
   reads the images from that stream and scores each one);
3. polls the client's score cache for that frame (same run uid, ``seq_num`` = the
   point index), yielding ``bps.sleep`` between polls so the RunEngine stays live;
4. sets ``rixs_score`` (1/FWHM px when valid, QERLIN's own convention, else 0,
   so gpCAM maximizes sharpness) and ``rixs_fwhm_px`` (NaN when invalid);
5. reads ``motors + [rixs_score, rixs_fwhm_px]`` into ``primary``, which
   tsuchinoko's TiledReader reads;
6. publishes ``adaptive.measured``; just before that it drops any queued copies of
   the batch it has just measured (see ``_drop_resent``).

The run closes when tsuchinoko's experiment ends (see ``_EndWatch``: a
``tsuchinoko.experiment.stop`` request on the bus, or ``tsuchinoko.status``
reporting ``Inactive``/``Stopping``/``Exiting``), when Lightfall's Stop is pressed, or,
as a fallback, after ``timeout`` s without new targets.

This module imports nothing from Lightfall at import time (the rixs-live venv
imports the plugin without Lightfall); ``lightfall_hooks()`` reaches Lightfall's
NATS bridge, credentials and adaptive-plan state lazily.
"""
from __future__ import annotations

import math
import time
from collections import deque
import uuid
from dataclasses import dataclass
from typing import Any, Callable

import bluesky.plan_stubs as bps
from loguru import logger
from ophyd import Signal

from .contract import MD_KEY, build_rixs_md
from .plans import frame_shape as marana_frame_shape
from .plans import motor_units, pva_frame_source, read_mono_energy, set_exposure

PLAN_NAME = "qerlin_optimize"
IMAGE_STREAM = "frames"
SCORE_FIELD = "rixs_score"
FWHM_FIELD = "rixs_fwhm_px"
TARGETS_SUBJECT = "tsuchinoko.targets"
BIND_SUBJECT = "tsuchinoko.experiment.bind_run"
STOP_SUBJECT = "tsuchinoko.experiment.stop"
STATUS_SUBJECT = "tsuchinoko.status"
# tsuchinoko CoreState names (tsuchinoko.core.CoreState)
ACTIVE_STATES = frozenset({"Starting", "Running", "Pausing", "Paused", "Resuming"})
ENDED_STATES = frozenset({"Inactive", "Stopping", "Exiting"})
MAX_CONSECUTIVE_TIMEOUTS = 3


class ScoreTimeoutError(RuntimeError):
    """rixs-live scored none of several consecutive frames (probably not running)."""


@dataclass
class OptimizeHooks:
    """What the plan needs from its surroundings; tests pass fakes.

    ``bridge``: ``subscribe/try_get/publish/cleanup`` (Lightfall's NATSPlanBridge).
    ``client``: ``get_score(run_uid, seq_num) -> dict | None`` (RixsServiceClient).
    ``credentials()``: ``(tiled_url, tiled_api_key, proxy_url)`` for tsuchinoko.
    ``prefix()``: Lightfall's NATS topic prefix.
    ``state``: object with ``stop_requested``/``pause_requested`` flags (and,
    optionally, Qt signals ``status_changed``/``iteration_changed``/``targets_received``).
    ``status()`` (optional): tsuchinoko's ``tsuchinoko.status`` reply (``{"state": ...}``)
    or None when it does not answer.
    """

    bridge: Any
    client: Any
    credentials: Callable[[], tuple[str, str | None, str | None]]
    prefix: Callable[[], str]
    state: Any
    status: Callable[[], dict | None] | None = None


def lightfall_hooks() -> OptimizeHooks:
    """Hooks into a running Lightfall; raises RuntimeError with what to fix.

    Runs on the RunEngine thread, so it creates no QObjects: the rixs client and
    binder must already exist and the binder be enabled (``rixs_service_status`` or
    the QERLIN Focus panel does that on the GUI thread), else the run would never
    be scored.
    """
    from lightfall.acquire.nats_bridge import NATSPlanBridge
    from lightfall.acquire.plans import adaptive
    from lightfall.ipc.service import get_ipc_service

    from . import runtime

    ipc = get_ipc_service()
    if ipc is None:
        raise RuntimeError("NATS not available: qerlin_optimize requires Lightfall IPC")
    client, binder = runtime._client, runtime._binder
    if client is None or binder is None or not binder.enabled:
        raise RuntimeError(
            "the rixs-live binder is not enabled, so no frame would be scored: call "
            "rixs_service_status (or open the QERLIN Focus panel) and retry")
    return OptimizeHooks(
        bridge=NATSPlanBridge(ipc),
        client=client,
        credentials=adaptive._get_tiled_credentials,
        prefix=adaptive._get_lightfall_prefix,
        state=adaptive._state,
        status=lambda: ipc.request(STATUS_SUBJECT, {}, timeout_ms=1000),
    )


def _field(device) -> str:
    """Primary data key of ``device`` (its first hinted field, else its name)."""
    hints = getattr(device, "hints", None) or {}
    return (hints.get("fields") or [device.name])[0]


def score_of(entry: dict | None) -> tuple[float, float]:
    """``(rixs_score, rixs_fwhm_px)`` = ``(1/FWHM, FWHM)`` from a cached frame score.

    ``(0, NaN)`` when the frame is unscorable or was not scored in time (None).
    """
    if not entry:
        return 0.0, math.nan
    fwhm = entry.get("fwhm_px")
    try:
        fwhm = float(fwhm)
    except (TypeError, ValueError):
        fwhm = math.nan
    if not entry.get("score_valid") or not math.isfinite(fwhm) or fwhm <= 0:
        return 0.0, math.nan
    return 1.0 / fwhm, fwhm


def _drop_resent(bridge, targets: list, pending: deque) -> int:
    """Drain queued target messages: drop re-sent copies of ``targets``, keep the rest.

    tsuchinoko's LightfallEngine waits only 10 s for ``adaptive.measured``; when a point
    takes longer (31 s on the beamline) it re-publishes the same targets, once per 10 s.
    Measuring those copies adds rows tsuchinoko then reads one step late, so it asks for
    each point again and every later point is measured twice (live run b69213c0).
    Called right before publishing ``adaptive.measured``: until tsuchinoko has that
    signal it cannot have chosen new targets, so a queued message equal to the batch
    just measured is a re-send. Other messages go to ``pending``, in order.
    """
    dropped = 0
    while (msg := bridge.try_get(TARGETS_SUBJECT)) is not None:
        if msg.get("targets") == targets:
            dropped += 1
        else:
            pending.append(msg)
    if dropped:
        logger.info(f"qerlin_optimize: dropped {dropped} re-sent copies of {targets}")
    return dropped


class _EndWatch:
    """Notices tsuchinoko's experiment ending, so the run need not wait for ``timeout``.

    tsuchinoko publishes no state events, so: (1) the plan subscribes to
    ``tsuchinoko.experiment.stop`` and sees every stop request (the agent's
    ``tsuchinoko_stop``, tsuchinoko's own UI client) as it is sent; (2) while waiting
    for targets it asks ``tsuchinoko.status`` every ``interval`` s and ends on
    Inactive/Stopping/Exiting, e.g. after a stop it missed or an internal stop_at.
    An ended state only counts once the experiment was seen active (a status in
    ACTIVE_STATES or a target received), so a slow start after bind_run is not
    mistaken for the end. No reply (tsuchinoko gone) never ends it: ``timeout`` does.
    """

    def __init__(self, bridge, status: Callable[[], dict | None] | None, interval: float):
        self.bridge, self.status, self.interval = bridge, status, interval
        self.seen_active = False
        self.reason: str | None = None
        self._next = time.monotonic() + interval
        bridge.subscribe(STOP_SUBJECT)

    def check_stop(self) -> bool:
        if self.reason is None and self.bridge.try_get(STOP_SUBJECT) is not None:
            self.reason = "tsuchinoko was stopped"
        return self.reason is not None

    def poll(self) -> bool:
        """``check_stop`` plus, at most every ``interval`` s, a status request."""
        if self.check_stop() or self.status is None:
            return self.reason is not None
        now = time.monotonic()
        if now < self._next:
            return False
        self._next = now + self.interval
        try:
            reply = self.status()
        except Exception as ex:  # noqa: BLE001 - treat like no reply
            logger.debug(f"qerlin_optimize: tsuchinoko.status failed: {ex!r}")
            reply = None
        st = (reply or {}).get("state")
        if st in ACTIVE_STATES:
            self.seen_active = True
        elif st in ENDED_STATES and self.seen_active:
            self.reason = f"tsuchinoko is {st}"
        return self.reason is not None


def _emit(state, signal: str, *args) -> None:
    sig = getattr(state, signal, None)
    if sig is not None:
        try:
            sig.emit(*args)
        except Exception as ex:  # noqa: BLE001 - UI feedback only
            logger.debug(f"qerlin_optimize: {signal} emit failed: {ex!r}")


def qerlin_optimize(marana, motors, mono=None, exposure_s: float = 1.0,
                    timeout: float = 300.0, score_timeout: float = 60.0, *,
                    status_interval: float = 5.0,
                    poll_interval: float = 0.1, hooks: OptimizeHooks | None = None,
                    md: dict | None = None):
    """Measure the targets tsuchinoko/gpCAM sends; score each Marana frame via rixs-live.

    ``motors`` (N >= 1) are moved to each target in order (target i is in the axes
    order of tsuchinoko's ``parameter_bounds``). ``mono`` (optional) only labels
    the run's mono energy for resolving power. The run closes once tsuchinoko's
    experiment ends (stop request seen, or its status Inactive, polled every
    ``status_interval`` s); ``timeout`` is the fallback: how long to wait for
    new targets before closing the run anyway; ``score_timeout`` how long to wait for each
    frame's score. A frame not scored in time records ``rixs_score`` 0; after
    ``MAX_CONSECUTIVE_TIMEOUTS`` in a row the plan fails with ScoreTimeoutError.
    """
    motors = list(motors)
    if not motors:
        raise ValueError("qerlin_optimize needs at least one motor")
    hooks = hooks or lightfall_hooks()
    state, bridge, client = hooks.state, hooks.bridge, hooks.client
    state.stop_requested = False
    state.pause_requested = False
    if hasattr(state, "current_iteration"):
        state.current_iteration = 0

    fields = [_field(m) for m in motors]
    mono_energy = yield from read_mono_energy(mono)
    frame_shape = marana_frame_shape(marana)
    yield from set_exposure(marana, exposure_s)

    score_sig = Signal(name=SCORE_FIELD, value=0.0)
    fwhm_sig = Signal(name=FWHM_FIELD, value=math.nan)
    lightfall_prefix = hooks.prefix()
    experiment_id = str(uuid.uuid4())
    _md = {
        "plan_name": PLAN_NAME,
        "motors": [m.name for m in motors],
        "tsuchinoko": {"experiment_id": experiment_id},
        "hints": {"dimensions": [([f], "primary") for f in fields]},
        MD_KEY: build_rixs_md(f"{marana.name}_image", fields[0], frame_shape, mono_energy,
                              motor_units=motor_units(motors[0]),
                              image_stream=IMAGE_STREAM, motor_fields=fields,
                              **pva_frame_source(marana)),
    }
    _md.update(md or {})

    bridge.subscribe(TARGETS_SUBJECT)
    end = _EndWatch(bridge, hooks.status, status_interval)
    try:
        run_uid = yield from bps.open_run(md=_md)
        tiled_url, tiled_api_key, proxy_url = hooks.credentials()
        bridge.publish(BIND_SUBJECT, {
            "run_uid": run_uid,
            "tiled_url": tiled_url,
            "tiled_api_key": tiled_api_key,
            "proxy_url": proxy_url,
            "lightfall_prefix": lightfall_prefix,
            "motor_names": [m.name for m in motors],
            "detector_name": SCORE_FIELD,
        })
        yield from bps.sleep(0.5)  # let tsuchinoko process bind_run

        point = 0  # == seq_num of the frame in the frames stream
        pending: deque = deque()  # target messages read while draining re-sends
        consecutive_timeouts = 0
        iteration = 0
        deadline = time.monotonic() + timeout
        while not state.stop_requested:
            while state.pause_requested and not state.stop_requested:
                yield from bps.sleep(poll_interval)
            if end.check_stop():
                break
            msg = pending.popleft() if pending else bridge.try_get(TARGETS_SUBJECT)
            if msg is None:
                if end.poll():
                    break
                if time.monotonic() > deadline:
                    _emit(state, "status_changed", "Timeout waiting for targets")
                    logger.info("qerlin_optimize: no new targets; closing the run")
                    break
                yield from bps.sleep(poll_interval)
                continue
            end.seen_active = True
            targets = msg.get("targets", [])
            iteration = msg.get("iteration", iteration + 1)
            if hasattr(state, "current_iteration"):
                state.current_iteration = iteration
            _emit(state, "iteration_changed", iteration)
            _emit(state, "targets_received", len(targets))

            for i, target in enumerate(targets):
                if state.stop_requested or end.check_stop():
                    break
                target = list(target)
                if len(target) != len(motors):
                    raise ValueError(f"target {target} has {len(target)} values for "
                                     f"{len(motors)} motors {fields}")
                args: list[Any] = []
                for motor, value in zip(motors, target):
                    args += [motor, float(value)]
                yield from bps.mv(*args)
                yield from bps.trigger_and_read([marana, *motors], name=IMAGE_STREAM)
                point += 1

                entry = None
                score_deadline = time.monotonic() + score_timeout
                while True:
                    entry = client.get_score(run_uid, point)
                    if entry is not None or time.monotonic() >= score_deadline:
                        break
                    yield from bps.sleep(poll_interval)
                if entry is None:
                    consecutive_timeouts += 1
                    logger.warning(
                        f"qerlin_optimize: no rixs-live score for frame {point} within "
                        f"{score_timeout:g} s ({consecutive_timeouts} in a row); "
                        f"recording score 0")
                else:
                    consecutive_timeouts = 0
                score, fwhm = score_of(entry)
                # put(), not bps.mv: a soft Signal's set() status waits for the
                # readback to equal the setpoint, which never happens for NaN.
                score_sig.put(score)
                fwhm_sig.put(fwhm)
                yield from bps.trigger_and_read([*motors, score_sig, fwhm_sig],
                                                name="primary")
                if i == len(targets) - 1:
                    _drop_resent(bridge, msg.get("targets"), pending)
                bridge.publish(f"{lightfall_prefix}.adaptive.measured", {
                    "iteration": iteration, "n_new_points": 1})
                if consecutive_timeouts >= MAX_CONSECUTIVE_TIMEOUTS:
                    text = (f"rixs-live scored none of the last {consecutive_timeouts} "
                            f"frames within {score_timeout:g} s each; it is probably not "
                            f"running or not bound (check rixs_service_status)")
                    _emit(state, "status_changed", text)
                    raise ScoreTimeoutError(text)
            deadline = time.monotonic() + timeout

        if end.reason:
            _emit(state, "status_changed", f"Experiment ended ({end.reason})")
            logger.info(f"qerlin_optimize: {end.reason}; closing the run")
        yield from bps.close_run()
    finally:
        bridge.cleanup()
