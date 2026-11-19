import numpy as np
import pytest
from ophyd.status import Status

from lightfall_endstation_602.sim.devices import (
    AxisTracker, SimMarana, SimMonoEnergy, SimSampleX, SimSM3Pitch, get_sim_state,
)
from lightfall_endstation_602.sim.model import FocusModel
from test_sim_model import _width


@pytest.fixture
def rig():
    state = get_sim_state()
    old = state.model, state.axes
    state.model = FocusModel(best_pitch=104.0, best_sample_x=-0.8)
    state.axes = AxisTracker()
    sm3 = SimSM3Pitch(name="sim_sm3_pitch")
    cam = SimMarana(name="sim_marana", frame_shape=(256, 256))
    yield state, sm3, cam
    state.model, state.axes = old


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
    sx = SimSampleX(name="sim_sample_x")
    assert state.axes.resolve("sim_sample_x", SimSampleX) is sx
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
    assert d["sim_marana_image"]["shape"] == [256, 256]


def test_motors(rig):
    state, sm3, _ = rig
    assert sm3.position == 105.0
    assert sm3.read()["sim_sm3_pitch"]["value"] == 105.0
    assert sm3.limits == (90.0, 125.0) and sm3.egu == ""
    sm3.set(110.0).wait(2)
    assert sm3.position == 110.0
    with pytest.raises(ValueError):
        sm3.set(0.0)
    sx = SimSampleX(name="sim_sample_x")
    assert sx.position == -0.85 and sx.limits == (-1.5, 0.5) and sx.egu == "mm"
    assert sx.read()["sim_sample_x"]["value"] == -0.85
    with pytest.raises(ValueError):
        sx.set(1.0)
    mono = SimMonoEnergy(name="sim_mono_energy")
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
    assert np.asarray(ev[0]["data"]["sim_marana_image"]).shape == (256, 256)
    assert [e["data"]["sim_sm3_pitch"] for e in ev] == [100, 104, 108]


def test_backend_resource_and_classes():
    import importlib
    import importlib.resources as res
    import json
    from lightfall_endstation_602.backend import QerlinSimBackendPlugin
    pkg, fn = QerlinSimBackendPlugin.database_resource
    db = json.loads(res.files(pkg).joinpath(fn).read_text())
    assert set(db) == {"sim_sm3_pitch", "sim_sample_x", "sim_mono_energy", "sim_marana"}
    for e in db.values():
        mod, cls = e["device_class"].rsplit(".", 1)
        assert hasattr(importlib.import_module(mod), cls)
    assert QerlinSimBackendPlugin().name == "qerlin_sim"


def test_describe_declares_uint16(rig):
    _, _, cam = rig
    d = cam.describe()["sim_marana_image"]
    assert d["dtype"] == "array"
    assert d["dtype_numpy"] == "<u2"
    assert d["shape"] == [256, 256]
    assert cam.image.get().dtype == np.uint16
    cam.trigger().wait(2)
    assert cam.image.get().dtype == np.uint16


def _lightfall_load():
    """Load the QERLIN happi DB the way Lightfall does, with the mock backend enabled.

    DeviceCatalog registers the mock backend first and skips any later device whose
    name is taken. The mock has its own ``sample_x`` and ``mono_energy``; before the
    ``sim_`` prefix those won the names and the sim Marana had to follow a mock
    SynAxis. Now nothing collides and every sim device is built directly.
    """
    from lightfall.devices.backends.mock import MockBackend
    from lightfall_endstation_602.backend import QerlinSimBackendPlugin

    mock = MockBackend()
    mock.connect()
    taken = {d.name for d in mock.get_all_devices()}
    happi = QerlinSimBackendPlugin().create_backend()
    happi.connect()
    infos = happi.load_metadata()
    assert not taken & {info.name for info in infos}  # no name clashes with the mock
    built = {info.name: happi.instantiate(info) for info in infos}
    for info in infos:
        info._ophyd_device = built[info.name]
    connected, _ = happi.reconnect_failed_devices(timeout=1.0)
    assert connected == 0  # nothing was skipped, so nothing is built late
    return built


def test_marana_follows_sim_sample_x_under_lightfall_loading(rig):
    """Regression (live run b69213c0): the Marana must follow the catalog's Sample X."""
    state, _, _ = rig
    devices = _lightfall_load()
    sx, sm3, cam = devices["sim_sample_x"], devices["sim_sm3_pitch"], devices["sim_marana"]
    assert type(sx) is SimSampleX  # the catalog's Sample X is the sim one
    cam.frame_shape = (256, 256)
    sm3.set(98.0).wait(2)
    widths = {}
    for x in (-0.83, -0.65, -0.95):
        sx.set(x).wait(2)
        cam.trigger().wait(2)
        widths[x] = _width(cam.image.get())
    assert widths[-0.65] > 2 * widths[-0.83]
    assert widths[-0.95] > 1.5 * widths[-0.83]


def test_tracker_sweep_finds_motors_built_before_it():
    """Lightfall builds the mock backend before this plugin is imported."""
    from ophyd.sim import SynAxis
    early = SynAxis(name="sim_sample_x")
    tracker = AxisTracker()
    assert tracker.resolve("sim_sample_x", SimSampleX) is None
    tracker.sweep()
    assert tracker.resolve("sim_sample_x", SimSampleX) is not None
    early.set(-0.7).wait(2)
    assert tracker.resolve("sim_sample_x", SimSampleX) is early


def test_tracker_prefers_the_moved_motor_then_sim_instance(rig):
    from ophyd.sim import SynAxis
    state, _, _ = rig
    other = SynAxis(name="sim_sample_x")
    sim = SimSampleX(name="sim_sample_x")
    assert state.axes.resolve("sim_sample_x", SimSampleX) is sim  # nothing moved yet
    other.set(-0.7).wait(2)
    assert state.axes.resolve("sim_sample_x", SimSampleX) is other
    assert state.axes.resolve("sim_sample_x", SimSampleX) is other  # sticky until another moves
    sim.set(-0.9).wait(2)
    assert state.axes.resolve("sim_sample_x", SimSampleX) is sim
