"""Simulated 6.0.2 ophyd devices. No lightfall imports (the service venv uses this)."""
from __future__ import annotations

import logging
import os

import numpy as np
from ophyd import Component as Cpt
from ophyd import Device, Signal
from ophyd.sim import SynAxis
from ophyd.status import Status

from .model import FocusModel, render_frame

log = logging.getLogger(__name__)


class SimState:
    """Module singleton: the hidden focus model and the registered focusing motors."""

    def __init__(self):
        seed = os.environ.get("LIGHTFALL_SIM_SEED")
        self.model: FocusModel = FocusModel.from_seed(int(seed) if seed else None)
        self.sm3: "SimSM3Pitch | None" = None
        self.sample_x: "SimSampleX | None" = None


_STATE: SimState | None = None


def get_sim_state() -> SimState:
    global _STATE
    if _STATE is None:
        _STATE = SimState()
    return _STATE


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
        get_sim_state().sm3 = self


class SimSampleX(_LimitedAxis):
    """Sample X in mm, limits -1.5..0.5."""

    _limits = (-1.5, 0.5)
    _init = -0.85
    _egu = "mm"

    def __init__(self, name, **kw):
        super().__init__(name, **kw)
        get_sim_state().sample_x = self


class SimMonoEnergy(_LimitedAxis):
    """Monochromator energy in eV."""

    _limits = (0.0, 5000.0)
    _init = 850.0
    _egu = "eV"


class SimMarana(Device):
    """Marana-like 2D detector; frame width depends on current SM3 pitch and Sample X."""

    image = Cpt(Signal, value=None, kind="normal")
    exposure_time = Cpt(Signal, value=1.0, kind="config")

    def __init__(self, name, frame_shape: tuple[int, int] = (2048, 2048), **kw):
        super().__init__(name=name, **kw)
        self.frame_shape = tuple(frame_shape)
        self._warned = False
        self.image.put(np.zeros(self.frame_shape, dtype=np.uint16))

    def describe(self):
        d = super().describe()
        d[self.image.name].update(
            shape=list(self.frame_shape), dtype="array", dtype_numpy="<u2")
        return d

    def trigger(self):
        state = get_sim_state()
        if state.sm3 is None:
            pitch = state.model.best_pitch
            if not self._warned:
                log.warning(f"SimMarana: no SimSM3Pitch registered; using pitch {pitch:g}")
                self._warned = True
        else:
            pitch = float(state.sm3.position)
        # No Sample X motor: treat Sample X as at its best for this pitch.
        sample_x = None if state.sample_x is None else float(state.sample_x.position)
        frame = render_frame(state.model, pitch, float(self.exposure_time.get()),
                             shape=self.frame_shape, sample_x=sample_x)
        self.image.put(frame)
        st = Status()
        st.set_finished()
        return st
