import numpy as np
import pytest
from ophyd.status import Status

from lightfall_endstation_602.sim.devices import (
    SimMarana, SimMonoEnergy, SimSM3Pitch, get_sim_state,
)
from lightfall_endstation_602.sim.model import FocusModel
from test_sim_model import _width


@pytest.fixture
def rig():
    state = get_sim_state()
    old = state.model, state.sm3
    state.model = FocusModel(best_pitch=0.4)
    sm3 = SimSM3Pitch(name="sm3_pitch")
    cam = SimMarana(name="marana", frame_shape=(256, 256))
    yield state, sm3, cam
    state.model, state.sm3 = old


def test_marana_trigger_reads_current_pitch(rig):
    state, sm3, cam = rig
    sm3.set(state.model.best_pitch).wait(2)
    st = cam.trigger()
    assert isinstance(st, Status) and st.done
    w_best = _width(cam.image.get())
    sm3.set(state.model.best_pitch + 1.5).wait(2)
    cam.trigger().wait(2)
    assert _width(cam.image.get()) > w_best


def test_describe_shape(rig):
    _, _, cam = rig
    d = cam.describe()
    assert d["marana_image"]["shape"] == [256, 256]


def test_motors(rig):
    _, sm3, _ = rig
    assert sm3.position == 0.0
    assert sm3.read()["sm3_pitch"]["value"] == 0.0
    sm3.set(1.0).wait(2)
    assert sm3.position == 1.0
    assert SimMonoEnergy(name="mono_energy").position == 850.0


def test_scan_with_bluesky(rig):
    from bluesky import RunEngine
    import bluesky.plans as bp
    state, sm3, cam = rig
    docs = []
    RE = RunEngine({})
    RE(bp.scan([cam], sm3, -1, 1, 3), lambda n, d: docs.append((n, d)))
    ev = [d for n, d in docs if n == "event"]
    assert len(ev) == 3
    assert np.asarray(ev[0]["data"]["marana_image"]).shape == (256, 256)
    assert [e["data"]["sm3_pitch"] for e in ev] == [-1, 0, 1]


def test_backend_resource_and_classes():
    import importlib
    import importlib.resources as res
    import json
    from lightfall_endstation_602.backend import QerlinSimBackendPlugin
    pkg, fn = QerlinSimBackendPlugin.database_resource
    db = json.loads(res.files(pkg).joinpath(fn).read_text())
    assert set(db) == {"sm3_pitch", "mono_energy", "marana"}
    for e in db.values():
        mod, cls = e["device_class"].rsplit(".", 1)
        assert hasattr(importlib.import_module(mod), cls)
    assert QerlinSimBackendPlugin().name == "qerlin_sim"
