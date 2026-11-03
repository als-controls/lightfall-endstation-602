import numpy as np
import pytest
from ophyd.status import Status

from lightfall_endstation_602.sim.devices import (
    SimMarana, SimMonoEnergy, SimSampleX, SimSM3Pitch, get_sim_state,
)
from lightfall_endstation_602.sim.model import FocusModel
from test_sim_model import _width


@pytest.fixture
def rig():
    state = get_sim_state()
    old = state.model, state.sm3, state.sample_x
    state.model = FocusModel(best_pitch=104.0, best_sample_x=-0.8)
    state.sample_x = None
    sm3 = SimSM3Pitch(name="sm3_pitch")
    cam = SimMarana(name="marana", frame_shape=(256, 256))
    yield state, sm3, cam
    state.model, state.sm3, state.sample_x = old


def test_marana_trigger_reads_current_pitch(rig):
    state, sm3, cam = rig
    sm3.set(state.model.best_pitch).wait(2)
    st = cam.trigger()
    assert isinstance(st, Status) and st.done
    w_best = _width(cam.image.get())
    sm3.set(state.model.best_pitch + 12.0).wait(2)
    cam.trigger().wait(2)
    assert _width(cam.image.get()) > w_best


def test_marana_trigger_reads_current_sample_x(rig):
    state, sm3, cam = rig
    sx = SimSampleX(name="sample_x")
    assert state.sample_x is sx
    sm3.set(state.model.best_pitch).wait(2)
    sx.set(state.model.best_sample_x).wait(2)
    cam.trigger().wait(2)
    w_best = _width(cam.image.get())
    sx.set(state.model.best_sample_x - 0.15).wait(2)
    cam.trigger().wait(2)
    assert _width(cam.image.get()) > w_best


def test_describe_shape(rig):
    _, _, cam = rig
    d = cam.describe()
    assert d["marana_image"]["shape"] == [256, 256]


def test_motors(rig):
    state, sm3, _ = rig
    assert sm3.position == 105.0
    assert sm3.read()["sm3_pitch"]["value"] == 105.0
    assert sm3.limits == (90.0, 125.0) and sm3.egu == ""
    sm3.set(110.0).wait(2)
    assert sm3.position == 110.0
    with pytest.raises(ValueError):
        sm3.set(0.0)
    sx = SimSampleX(name="sample_x")
    assert sx.position == -0.85 and sx.limits == (-1.5, 0.5) and sx.egu == "mm"
    assert sx.read()["sample_x"]["value"] == -0.85
    with pytest.raises(ValueError):
        sx.set(1.0)
    mono = SimMonoEnergy(name="mono_energy")
    assert mono.position == 850.0 and mono.egu == "eV"


def test_scan_with_bluesky(rig):
    from bluesky import RunEngine
    import bluesky.plans as bp
    state, sm3, cam = rig
    docs = []
    RE = RunEngine({})
    RE(bp.scan([cam], sm3, 100, 108, 3), lambda n, d: docs.append((n, d)))
    ev = [d for n, d in docs if n == "event"]
    assert len(ev) == 3
    assert np.asarray(ev[0]["data"]["marana_image"]).shape == (256, 256)
    assert [e["data"]["sm3_pitch"] for e in ev] == [100, 104, 108]


def test_backend_resource_and_classes():
    import importlib
    import importlib.resources as res
    import json
    from lightfall_endstation_602.backend import QerlinSimBackendPlugin
    pkg, fn = QerlinSimBackendPlugin.database_resource
    db = json.loads(res.files(pkg).joinpath(fn).read_text())
    assert set(db) == {"sm3_pitch", "sample_x", "mono_energy", "marana"}
    for e in db.values():
        mod, cls = e["device_class"].rsplit(".", 1)
        assert hasattr(importlib.import_module(mod), cls)
    assert QerlinSimBackendPlugin().name == "qerlin_sim"


def test_describe_declares_uint16(rig):
    _, _, cam = rig
    d = cam.describe()["marana_image"]
    assert d["dtype"] == "array"
    assert d["dtype_numpy"] == "<u2"
    assert d["shape"] == [256, 256]
    assert cam.image.get().dtype == np.uint16
    cam.trigger().wait(2)
    assert cam.image.get().dtype == np.uint16
