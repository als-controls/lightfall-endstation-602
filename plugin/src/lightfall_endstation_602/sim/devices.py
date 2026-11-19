"""Simulated 6.0.2 ophyd devices. No lightfall imports (the service venv uses this)."""
from __future__ import annotations

import gc
import itertools
import logging
import os
import threading
import weakref

import numpy as np
from ophyd import Component as Cpt
from ophyd import Device, Signal
from ophyd.ophydobj import OphydObject
from ophyd.sim import SynAxis
from ophyd.status import Status

from .model import FocusModel, render_frame

log = logging.getLogger(__name__)


SM3_NAME = "sim_sm3_pitch"
SAMPLE_X_NAME = "sim_sample_x"
TRACKED_NAMES = (SM3_NAME, SAMPLE_X_NAME)


def _position(obj) -> float | None:
    try:
        return float(obj.position)
    except Exception:  # noqa: BLE001 - not a positioner, or not readable yet
        return None


class AxisTracker:
    """Every live top-level ophyd object named like a focusing motor; picks the moved one.

    SimMarana must follow whichever ``sim_sm3_pitch`` / ``sim_sample_x`` the user
    actually moves, and that need not be a device this module built: in Lightfall,
    two backends can offer the same name (only one wins the catalog name; the other
    can still be built by happi's reconnect pass). The ``sim_`` prefix keeps the sim
    devices clear of real and mock names, but the tracker stays defensive. So the tracker keeps every candidate by name
    (an ophyd instantiation callback, plus one ``gc`` sweep for objects built
    before this module was imported) and, at each frame, uses the candidate whose
    position changed most recently; until one moves, the newest Sim* instance.
    """

    def __init__(self):
        self._lock = threading.Lock()
        # name -> id(obj) -> [weakref, first position, last position, order]
        self._entries: dict[str, dict[int, list]] = {n: {} for n in TRACKED_NAMES}
        self._active: dict[str, int] = {}
        self._order = itertools.count()

    def watch(self, obj) -> None:
        """Track ``obj`` if it is a top-level object with a tracked name."""
        name = getattr(obj, "_name", None)
        if name not in self._entries or getattr(obj, "_parent", None) is not None:
            return
        with self._lock:
            entries = self._entries[name]
            if id(obj) not in entries or entries[id(obj)][0]() is not obj:
                entries[id(obj)] = [weakref.ref(obj), None, None, next(self._order)]

    def observe(self, obj) -> None:
        """Record ``obj``'s current position as its baseline (it has not moved)."""
        self.watch(obj)
        pos = _position(obj)
        with self._lock:
            e = self._entries.get(obj.name, {}).get(id(obj))
            if e is not None and pos is not None:
                if e[1] is None:
                    e[1] = pos
                e[2] = pos

    def sweep(self) -> None:
        """Pick up candidates that already existed (built before the callback)."""
        for obj in gc.get_objects():
            if isinstance(obj, OphydObject) and getattr(obj, "_name", None) in self._entries:
                self.watch(obj)
                if getattr(obj, "_parent", None) is None:
                    self.observe(obj)

    def resolve(self, name: str, prefer: type):
        """The ``name`` object to use for this frame, or None if none is alive."""
        with self._lock:
            entries = self._entries[name]
            live = {}
            for key, e in list(entries.items()):
                obj = e[0]()
                if obj is None:
                    del entries[key]
                    continue
                live[key] = obj
            if not live:
                self._active.pop(name, None)
                return None
            changed = []
            for key, obj in live.items():
                e = entries[key]
                pos = _position(obj)
                if pos is None:
                    continue
                if e[1] is None:
                    e[1] = pos
                elif e[2] is not None and pos != e[2]:
                    changed.append(key)
                e[2] = pos
            active = self._active.get(name)
            if changed:
                if active not in changed:
                    active = max(changed, key=lambda k: entries[k][3])
            elif active not in live:
                moved = [k for k in live if entries[k][2] is not None
                         and entries[k][2] != entries[k][1]]
                sims = [k for k in live if isinstance(live[k], prefer)]
                pool = moved or sims or list(live)
                active = max(pool, key=lambda k: entries[k][3])
            self._active[name] = active
            return live[active]


class SimState:
    """Module singleton: the hidden focus model and the focusing-motor tracker."""

    def __init__(self):
        seed = os.environ.get("LIGHTFALL_SIM_SEED")
        self.model: FocusModel = FocusModel.from_seed(int(seed) if seed else None)
        self.axes = AxisTracker()


_STATE: SimState | None = None
_SWEPT = False


def get_sim_state() -> SimState:
    global _STATE
    if _STATE is None:
        _STATE = SimState()
    return _STATE


def _on_new_ophyd_object(obj) -> None:
    get_sim_state().axes.watch(obj)


def _sweep_once() -> None:
    """One gc sweep per process, for candidates built before this module."""
    global _SWEPT
    if not _SWEPT:
        _SWEPT = True
        get_sim_state().axes.sweep()


OphydObject.add_instantiation_callback(_on_new_ophyd_object)


class _LimitedAxis(SynAxis):
    _limits = (-3.0, 3.0)
    _init = 0.0
    _egu = ""

    def __init__(self, name, **kw):
        kw.setdefault("value", self._init)
        kw.setdefault("egu", self._egu)  # SynAxis would default to "mm"
        super().__init__(name=name, **kw)

    @property
    def limits(self):
        return self._limits

    def set(self, value):
        lo, hi = self._limits
        if not lo <= value <= hi:
            raise ValueError(f"{self.name}: {value} outside limits {self._limits}")
        return super().set(value)


class SimSM3Pitch(_LimitedAxis):
    """SM3 mirror pitch in its native (logged) units, limits 90..125."""

    _limits = (90.0, 125.0)
    _init = 105.0

    def __init__(self, name, **kw):
        super().__init__(name, **kw)
        get_sim_state().axes.observe(self)


class SimSampleX(_LimitedAxis):
    """Sample X in mm, limits -1.5..0.5."""

    _limits = (-1.5, 0.5)
    _init = -0.85
    _egu = "mm"

    def __init__(self, name, **kw):
        super().__init__(name, **kw)
        get_sim_state().axes.observe(self)


class SimMonoEnergy(_LimitedAxis):
    """Monochromator energy in eV."""

    _limits = (0.0, 5000.0)
    _init = 850.0
    _egu = "eV"


class SimMaranaCam(Device):
    """The slice of the real Marana's areaDetector cam (``cam1:``) the plans use."""

    acquire_time = Cpt(Signal, value=1.0, kind="config")  # exposure, seconds


class SimMarana(Device):
    """Marana-like 2D detector; frame width depends on current SM3 pitch and Sample X.

    The motors are found by name (``sim_sm3_pitch``, ``sim_sample_x``) at each trigger,
    through :class:`AxisTracker`, not handed in. Exposure is ``cam.acquire_time``,
    as on the real Marana, so the same plans drive both.
    """

    image = Cpt(Signal, value=None, kind="normal")
    cam = Cpt(SimMaranaCam, "", kind="config")

    def __init__(self, name, frame_shape: tuple[int, int] = (2048, 2048), **kw):
        super().__init__(name=name, **kw)
        self.frame_shape = tuple(frame_shape)
        self._warned = False
        _sweep_once()
        self.image.put(np.zeros(self.frame_shape, dtype=np.uint16))

    def describe(self):
        d = super().describe()
        d[self.image.name].update(
            shape=list(self.frame_shape), dtype="array", dtype_numpy="<u2")
        return d

    def trigger(self):
        state = get_sim_state()
        sm3 = state.axes.resolve(SM3_NAME, SimSM3Pitch)
        if sm3 is None:
            pitch = state.model.best_pitch
            if not self._warned:
                log.warning(f"SimMarana: no {SM3_NAME} motor found; using pitch {pitch:g}")
                self._warned = True
        else:
            pitch = float(sm3.position)
        # No Sample X motor: treat Sample X as at its best for this pitch.
        sx = state.axes.resolve(SAMPLE_X_NAME, SimSampleX)
        sample_x = None if sx is None else float(sx.position)
        frame = render_frame(state.model, pitch, float(self.cam.acquire_time.get()),
                             shape=self.frame_shape, sample_x=sample_x)
        self.image.put(frame)
        st = Status()
        st.set_finished()
        return st
