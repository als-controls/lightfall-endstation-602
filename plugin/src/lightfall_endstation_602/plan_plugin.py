"""Lightfall PlanPlugin exposing the QERLIN focus scan."""
# PEP 563 keeps Unit()/Range() metadata introspectable (see lightfall_pystxmcontrol).
from __future__ import annotations

from collections.abc import Callable, Generator
from typing import Annotated, Any

from lightfall.plugins.plan_plugin import PlanPlugin
from lightfall.ui.annotations import DeviceFilter, Range, Unit

from .plans import qerlin_focus_scan
from .sim.devices import SimMarana, SimMonoEnergy, SimSM3Pitch


def _cls(c: type) -> str:
    return f"{c.__module__}.{c.__name__}"


MARANA_CLASS = _cls(SimMarana)
SM3_CLASS = _cls(SimSM3Pitch)
MONO_CLASS = _cls(SimMonoEnergy)


def _qerlin_focus_scan_ui(
    marana: Annotated[Any, DeviceFilter(device_class=MARANA_CLASS)],
    sm3: Annotated[Any, DeviceFilter(device_class=SM3_CLASS)],
    mono: Annotated[Any, DeviceFilter(device_class=MONO_CLASS)],
    *,
    start: Annotated[float, Unit("mrad")] = -1.0,
    stop: Annotated[float, Unit("mrad")] = 1.0,
    num: Annotated[int, Range(1, 201)] = 21,
    exposure_s: Annotated[float, Unit("s")] = 1.0,
) -> Generator[Any, Any, Any]:
    """Scan SM3 mirror pitch and take one Marana frame per point (focus scan).

    UNITS: start/stop are SM3 pitch in milliradians (mrad, hardware limits
    +-3); exposure_s is the Marana exposure in SECONDS. ``num`` is the number
    of pitch points (1-201) and sets how much the scan does versus the agent:
    a large num is a dense scan that leaves little for the agent to do, a small
    num (e.g. 5-9) is a coarse scan the agent refines iteratively. num=1 with
    start == stop is a single confirming exposure. Device args are names:
    marana, sm3, mono. Each run's start document carries the ``rixs`` metadata
    that the rixs-live service binds on.
    """
    return (yield from qerlin_focus_scan(
        marana, sm3, mono, start, stop, num, exposure_s=exposure_s, md=None))


class QerlinFocusScanPlanPlugin(PlanPlugin):
    """Contributes qerlin_focus_scan to Lightfall's plan registry."""

    @property
    def name(self) -> str:
        return "qerlin_focus_scan"

    @property
    def category(self) -> str:
        return "rixs"

    def get_plan_function(self) -> Callable[..., Generator[Any, Any, Any]]:
        return _qerlin_focus_scan_ui
