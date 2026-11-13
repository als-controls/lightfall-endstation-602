"""Lightfall PlanPlugins exposing the QERLIN focus scan and the N-D optimization."""
# PEP 563 keeps Unit()/Range() metadata introspectable (see lightfall_pystxmcontrol).
from __future__ import annotations

from collections.abc import Callable, Generator
from typing import Annotated, Any

from lightfall.acquire.plan_ui import plan_with_ui
from lightfall.plugins.plan_plugin import PlanPlugin
from lightfall.ui.annotations import DeviceDefault, DeviceFilter, Range, Unit

from .optimize import qerlin_optimize
from .plans import qerlin_focus_scan
from .sim.devices import SimMarana, SimMonoEnergy


def _cls(c: type) -> str:
    return f"{c.__module__}.{c.__name__}"


MARANA_CLASS = _cls(SimMarana)
MONO_CLASS = _cls(SimMonoEnergy)


def _qerlin_focus_scan_ui(
    marana: Annotated[Any, DeviceFilter(device_class=MARANA_CLASS)],
    # Any motor; the sim backend tags sm3_pitch and sample_x as motors.
    motor: Annotated[Any, DeviceFilter(category="motor"), DeviceDefault("sm3_pitch")],
    mono: Annotated[Any, DeviceFilter(device_class=MONO_CLASS)],
    *,
    # No Unit(): start/stop are in the chosen motor's own units.
    start: float = 98.0,
    stop: float = 116.0,
    num: Annotated[int, Range(1, 201)] = 19,
    exposure_s: Annotated[float, Unit("s")] = 1.0,
) -> Generator[Any, Any, Any]:
    """Scan one focusing motor and take one Marana frame per point (focus scan).

    UNITS: start/stop are in the scanned motor's own units: SM3 pitch
    (sm3_pitch) in its native units (limits 90..125, typical scan 98-116 step
    1), Sample X (sample_x) in mm (limits -1.5..0.5, typical span 0.12-0.15
    step 0.01). exposure_s is the Marana exposure in SECONDS. ``num`` is the
    number of points (1-201) and sets how much the scan does versus the agent:
    a large num is a dense scan that leaves little for the agent to do, a small
    num (e.g. 5-9) is a coarse scan the agent refines iteratively. num=1 with
    start == stop is a single confirming exposure. Device args are names:
    marana, motor (e.g. sm3_pitch or sample_x), mono. Each run's start
    document carries the ``rixs`` metadata that the rixs-live service binds on.
    """
    return (yield from qerlin_focus_scan(
        marana, motor, mono, start, stop, num, exposure_s=exposure_s, md=None))


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


def _adaptive_panel():
    """Lightfall's adaptive-plan UI (stop/pause buttons sharing its plan state)."""
    from lightfall.acquire.plans.adaptive import AdaptiveExperimentPanel
    return AdaptiveExperimentPanel


@plan_with_ui(_adaptive_panel())
def _qerlin_optimize_ui(
    marana: Annotated[Any, DeviceFilter(device_class=MARANA_CLASS)],
    # Any motors, in the axes order of tsuchinoko's parameter_bounds.
    motors: Annotated[list[Any], DeviceFilter(category="motor"),
                      DeviceDefault("sm3_pitch", "sample_x")],
    mono: Annotated[Any, DeviceFilter(device_class=MONO_CLASS),
                    DeviceDefault("mono_energy")] = None,
    *,
    exposure_s: Annotated[float, Unit("s")] = 1.0,
    timeout: Annotated[float, Unit("s")] = 300.0,
    score_timeout: Annotated[float, Unit("s")] = 60.0,
) -> Generator[Any, Any, Any]:
    """N-D focus optimization: tsuchinoko/gpCAM picks points, rixs-live scores frames.

    Start tsuchinoko and configure it (autonomous_experiment workflow) first; this
    plan then measures the targets it sends. ``motors`` is a list of 1 or more
    motor names (e.g. ["sm3_pitch", "sample_x"]) in the SAME order as the axes of
    tsuchinoko's parameter_bounds. Each target takes one Marana frame (the
    ``frames`` stream); rixs-live's FWHM of that frame becomes ``rixs_score`` =
    100/FWHM px (0 when unscorable), which gpCAM maximizes, with ``rixs_fwhm_px``
    alongside, in the ``primary`` stream. UNITS: exposure_s, timeout (wait for new
    targets before closing the run) and score_timeout (wait for each frame's
    score) are in SECONDS. The run fails after 3 frames in a row go unscored
    (rixs-live down). ``mono`` (optional) labels the mono energy.
    """
    return (yield from qerlin_optimize(
        marana, motors, mono, exposure_s=exposure_s, timeout=timeout,
        score_timeout=score_timeout))


class QerlinOptimizePlanPlugin(PlanPlugin):
    """Contributes qerlin_optimize (gpCAM-driven N-D focusing) to the plan registry."""

    @property
    def name(self) -> str:
        return "qerlin_optimize"

    @property
    def category(self) -> str:
        return "rixs"

    def get_plan_function(self) -> Callable[..., Generator[Any, Any, Any]]:
        return _qerlin_optimize_ui
