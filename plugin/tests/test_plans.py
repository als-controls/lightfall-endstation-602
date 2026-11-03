import inspect

import numpy as np
import pytest
from bluesky import RunEngine

from lightfall_endstation_602.plans import qerlin_focus_scan
from lightfall_endstation_602.sim.devices import (
    SimMarana, SimMonoEnergy, SimSM3Pitch, get_sim_state,
)
from lightfall_endstation_602.sim.model import FocusModel


@pytest.fixture
def rig():
    state = get_sim_state()
    old = state.model, state.sm3
    state.model = FocusModel(best_pitch=104.0)
    sm3 = SimSM3Pitch(name="sm3_pitch")
    mono = SimMonoEnergy(name="mono_energy")
    cam = SimMarana(name="marana", frame_shape=(2048, 2048))
    yield cam, sm3, mono
    state.model, state.sm3 = old


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
    ev = [d for n, d in docs if n == "event"]
    assert len(ev) == 5
    assert [e["data"]["sm3_pitch"] for e in ev] == pytest.approx(np.linspace(100, 108, 5))
    assert cam.exposure_time.get() == 0.5


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
    for k in ("marana", "sm3", "mono", "start", "stop", "num", "exposure_s"):
        assert k in params
