import inspect

import numpy as np
import pytest
from bluesky import RunEngine

from lightfall_endstation_602.plans import qerlin_focus_scan
from lightfall_endstation_602.plans import motor_units
from lightfall_endstation_602.sim.devices import (
    AxisTracker, SimMarana, SimMonoEnergy, SimSampleX, SimSM3Pitch, get_sim_state,
)
from lightfall_endstation_602.sim.model import FocusModel


@pytest.fixture
def rig():
    state = get_sim_state()
    old = state.model, state.axes
    state.model, state.axes = FocusModel(best_pitch=104.0), AxisTracker()
    sm3 = SimSM3Pitch(name="sm3_pitch")
    mono = SimMonoEnergy(name="mono_energy")
    cam = SimMarana(name="marana", frame_shape=(2048, 2048))
    yield cam, sm3, mono
    state.model, state.axes = old


def _run(plan):
    docs = []
    RunEngine({})(plan, lambda n, d: docs.append((n, d)))
    return docs


def test_focus_scan_md_and_events(rig):
    cam, sm3, mono = rig
    docs = _run(qerlin_focus_scan(cam, sm3, mono, 100.0, 108.0, 5, exposure_s=0.5))
    start = next(d for n, d in docs if n == "start")
    rixs = start["rixs"]
    assert rixs["kind"] == "zeroth_order_focus"
    assert rixs["image_field"] == "marana_image"
    assert rixs["motor_field"] == "sm3_pitch"
    assert rixs["frame_shape"] == [2048, 2048]
    assert rixs["mono_energy_ev"] == 850.0
    assert rixs["motor_units"] == ""  # SM3 pitch: native units, no egu
    ev = [d for n, d in docs if n == "event"]
    assert len(ev) == 5
    assert [e["data"]["sm3_pitch"] for e in ev] == pytest.approx(np.linspace(100, 108, 5))
    assert cam.exposure_time.get() == 0.5


def test_focus_scan_any_motor_sample_x(rig):
    cam, _, mono = rig
    sx = SimSampleX(name="sample_x")
    docs = _run(qerlin_focus_scan(cam, sx, mono, -0.88, -0.76, 3, exposure_s=0.5))
    rixs = next(d for n, d in docs if n == "start")["rixs"]
    assert rixs["motor_field"] == "sample_x"
    assert rixs["motor_units"] == "mm"
    ev = [d for n, d in docs if n == "event"]
    assert [e["data"]["sample_x"] for e in ev] == pytest.approx([-0.88, -0.82, -0.76])


def test_motor_units_fallbacks():
    class Desc:
        name = "m"
        egu = "mm"

        def describe(self):
            return {"m": {"units": "deg"}}

    class Egu:
        name = "m"
        egu = "um"

        def describe(self):
            raise RuntimeError("no describe")

    class Bare:
        name = "m"

        def describe(self):
            return {"m": {}}

    assert motor_units(Desc()) == "deg"
    assert motor_units(Egu()) == "um"
    assert motor_units(Bare()) == ""


def test_caller_md_merged(rig):
    cam, sm3, mono = rig
    docs = _run(qerlin_focus_scan(cam, sm3, mono, 104, 105, 2, md={"note": "x"}))
    start = next(d for n, d in docs if n == "start")
    assert start["note"] == "x" and "rixs" in start


def test_single_point_confirm(rig):
    cam, sm3, mono = rig
    docs = _run(qerlin_focus_scan(cam, sm3, mono, 104.0, 104.0, 1))
    assert len([1 for n, _ in docs if n == "event"]) == 1


def test_plan_plugin_registers():
    from lightfall_endstation_602.plan_plugin import QerlinFocusScanPlanPlugin
    p = QerlinFocusScanPlanPlugin()
    assert p.name == "qerlin_focus_scan"
    params = inspect.signature(p.get_plan_function()).parameters
    for k in ("marana", "motor", "mono", "start", "stop", "num", "exposure_s"):
        assert k in params
    assert "sm3" not in params
    assert "motor" in inspect.signature(qerlin_focus_scan).parameters


def test_plan_plugin_motor_filter_is_any_motor():
    from typing import get_args, get_type_hints

    from lightfall.ui.annotations import DeviceDefault, DeviceFilter

    from lightfall_endstation_602 import plan_plugin
    hints = get_type_hints(plan_plugin._qerlin_focus_scan_ui, include_extras=True)
    meta = get_args(hints["motor"])[1:]
    assert DeviceFilter(category="motor") in meta
    assert any(isinstance(m, DeviceDefault) and m.names == ("sm3_pitch",) for m in meta)


def test_sim_motors_are_motor_category():
    import happi
    from importlib.resources import files

    from lightfall.devices.backends.happi import _guess_category
    path = files("lightfall_endstation_602.sim").joinpath("qerlin_happi.json")
    cats = {r.item.name: str(_guess_category(r.item))
            for r in happi.Client(path=str(path)).search()}
    assert cats["sm3_pitch"] == cats["sample_x"] == "motor"
