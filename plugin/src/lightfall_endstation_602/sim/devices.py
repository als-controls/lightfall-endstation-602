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
    """Module singleton: the hidden focus model and the registered SM3 motor."""

    def __init__(self):
        seed = os.environ.get("LIGHTFALL_SIM_SEED")
        self.model: FocusModel = FocusModel.from_seed(int(seed) if seed else None)
        self.sm3: "SimSM3Pitch | None" = None


_STATE: SimState | None = None


def get_sim_state() -> SimState:
    global _STATE
    if _STATE is None:
        _STATE = SimState()
    return _STATE


class _LimitedAxis(SynAxis):
    _limits = (-3.0, 3.0)
    _init = 0.0

    def __init__(self, name, **kw):
        kw.setdefault("value", self._init)
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
    """SM3 mirror pitch in mrad, limits +-3."""

    _limits = (-3.0, 3.0)
    _init = 0.0

    def __init__(self, name, **kw):
        super().__init__(name, **kw)
        get_sim_state().sm3 = self


class SimMonoEnergy(_LimitedAxis):
    """Monochromator energy in eV."""

    _limits = (0.0, 5000.0)
    _init = 850.0


class SimMarana(Device):
    """Marana-like 2D detector; frame width depends on current SM3 pitch."""

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
            if not self._warned:
                log.warning("SimMarana: no SimSM3Pitch registered; using pitch 0.0")
                self._warned = True
            pitch = 0.0
        else:
            pitch = float(state.sm3.position)
        frame = render_frame(state.model, pitch, float(self.exposure_time.get()),
                             shape=self.frame_shape)
        self.image.put(frame)
        st = Status()
        st.set_finished()
        return st
