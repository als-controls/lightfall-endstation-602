"""The real Marana writes HDF5 files under /data/<beamline>/marana01/... ."""
from __future__ import annotations

import logging

import pytest
from ophyd.sim import make_fake_device

from lightfall_endstation_602 import devices, paths
from lightfall_endstation_602.devices import Marana


@pytest.fixture(autouse=True)
def _reset_warned():
    paths._warned = False
    yield
    paths._warned = False


def _set_beamline(monkeypatch, value):
    monkeypatch.setattr(paths, "_beamline", lambda: value)


@pytest.fixture
def plain_puts(monkeypatch):
    """Fake signals carry no enum strings, so ophyd's set_and_wait readback check
    never matches there; on a real IOC it does. Replace it with a plain put."""
    monkeypatch.setattr(devices, "set_and_wait", lambda sig, val, **kw: sig.put(val))


def _marana():
    return make_fake_device(Marana)("BL602:marana01:", name="marana")


def test_template_with_beamline(monkeypatch):
    _set_beamline(monkeypatch, "6.0.2")
    assert paths.data_write_template("marana01") == "/data/6.0.2/marana01/%Y/%m/%d/"


def test_template_without_beamline_warns_once(monkeypatch, caplog):
    _set_beamline(monkeypatch, "")
    with caplog.at_level(logging.WARNING, logger=paths.logger.name):
        assert paths.data_write_template("marana01") == "/data/marana01/%Y/%m/%d/"
        paths.data_write_template("marana01")
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_marana_writes_under_the_beamline_directory(monkeypatch):
    _set_beamline(monkeypatch, "6.0.2")
    dev = _marana()
    assert dev.hdf1.write_path_template.rstrip("/") == "/data/6.0.2/marana01/%Y/%m/%d"
    assert str(dev.hdf1.reg_root).replace("\\", "/") == "/data"
    assert dev.hdf1.read_path_template.rstrip("/") == "/data/6.0.2/marana01/%Y/%m/%d"


def test_hdf1_is_read_and_armed(monkeypatch):
    """HDF1 is part of every reading and its stage_sigs arm a file per run."""
    _set_beamline(monkeypatch, "6.0.2")
    dev = _marana()
    assert "hdf1" in dev.read_attrs
    sigs = {getattr(k, "attr_name", k): v for k, v in dev.hdf1.stage_sigs.items()}
    assert sigs.get("enable") == 1
    assert sigs.get("file_write_mode") == "Stream"
    assert sigs.get("capture") == 1


def test_hdf1_stages_create_directory_for_the_whole_tail(monkeypatch):
    """<beamline>/marana01/%Y/%m/%d is 5 levels below /data: the IOC creates them."""
    _set_beamline(monkeypatch, "6.0.2")
    dev = _marana()
    sigs = {getattr(k, "attr_name", k): v for k, v in dev.hdf1.stage_sigs.items()}
    assert sigs.get("create_directory") == -5
    tail = dev.hdf1.write_path_template.strip("/").split("/")[1:]  # drop "data"
    assert len(tail) == -sigs["create_directory"]


def test_frames_per_point_follows_num_images(monkeypatch):
    _set_beamline(monkeypatch, "6.0.2")
    dev = _marana()
    dev.cam.num_images.sim_put(3)
    assert dev.hdf1.get_frames_per_point() == 3


def test_warmup_uses_andor3_fixed_mode_and_restores(monkeypatch, plain_puts):
    """ADAndor3 has no 'Single' ImageMode; warmup must use 'Fixed' and put the cam back."""
    _set_beamline(monkeypatch, "6.0.2")
    dev = _marana()
    cam = dev.cam
    cam.image_mode.sim_put("Continuous")
    cam.acquire_time.sim_put(2.5)
    seen = {}

    def fake_acquire(value, **kwargs):
        seen["image_mode"] = cam.image_mode.get()
        seen["num_images"] = cam.num_images.get()
        dev.hdf1.array_size.width.sim_put(2048)  # the plugin saw an array
        dev.hdf1.array_size.height.sim_put(2048)

    monkeypatch.setattr(cam.acquire, "put", fake_acquire)
    dev.hdf1.warmup()

    assert seen == {"image_mode": "Fixed", "num_images": 1}
    assert cam.image_mode.get() == "Continuous"
    assert cam.acquire_time.get() == 2.5


def test_warmup_times_out_loudly(monkeypatch, plain_puts):
    _set_beamline(monkeypatch, "6.0.2")
    dev = _marana()
    dev.hdf1._WARMUP_TIMEOUT_S = 0.2
    monkeypatch.setattr(dev.cam.acquire, "put", lambda value, **kw: None)
    with pytest.raises(RuntimeError, match="saw no array"):
        dev.hdf1.warmup()



def test_frame_shape_before_a_run_comes_from_the_cam(monkeypatch):
    """Before staging the real Marana describes no image key; the plans' shape comes
    from cam.array_size (height, width), not the (num_images, h, w) data key."""
    from lightfall_endstation_602.plans import frame_shape

    _set_beamline(monkeypatch, "6.0.2")
    dev = _marana()
    dev.cam.array_size.array_size_y.sim_put(2048)
    dev.cam.array_size.array_size_x.sim_put(1024)
    assert "marana_image" not in dev.describe()
    assert frame_shape(dev) == (2048, 1024)


def test_frame_shape_from_the_sim_describe():
    from lightfall_endstation_602.plans import frame_shape
    from lightfall_endstation_602.sim.devices import SimMarana

    assert frame_shape(SimMarana(name="marana", frame_shape=(2048, 2048))) == (2048, 2048)


# ----------------------------------------------------------- PVA frames for rixs-live


def test_pva1_is_enabled_blocking_and_counter_is_read(monkeypatch):
    """Pva1 posts each frame before acquisition completes; events carry the counter."""
    _set_beamline(monkeypatch, "6.0.2")
    dev = _marana()
    sigs = {getattr(k, "attr_name", k): v for k, v in dev.pva1.stage_sigs.items()}
    assert sigs.get("enable") == 1
    assert sigs.get("blocking_callbacks") == "Yes"
    assert "array_counter" in dev.read_attrs
    assert dev.array_counter.name == "marana_array_counter"
    assert Marana.array_counter.suffix == "cam1:ArrayCounter_RBV"


def test_pva_frame_source_names_the_channel_and_counter(monkeypatch):
    from lightfall_endstation_602.plans import pva_frame_source

    _set_beamline(monkeypatch, "6.0.2")
    dev = _marana()
    dev.pva1.pv_name.sim_put("BL602:marana01:Pva1:Image")
    assert pva_frame_source(dev) == {"pva_channel": "BL602:marana01:Pva1:Image",
                                     "frame_counter_field": "marana_array_counter"}
    dev.pva1.pv_name.sim_put("")  # unset readback: areaDetector's default name
    assert pva_frame_source(dev)["pva_channel"] == "BL602:marana01:Pva1:Image"


def test_sim_marana_keeps_its_frames_in_tiled():
    from lightfall_endstation_602.plans import pva_frame_source
    from lightfall_endstation_602.sim.devices import SimMarana

    assert pva_frame_source(SimMarana(name="marana", frame_shape=(8, 8))) == {}


def test_array_counter_is_read_fresh_not_from_the_monitor_cache():
    assert Marana.array_counter.kwargs.get("auto_monitor") is False


def test_every_stage_sig_resolves_to_a_component(monkeypatch):
    """Staging resolves each stage_sigs key with getattr; a missing component only
    fails at stage time on the real IOC (it did, for create_directory)."""
    _set_beamline(monkeypatch, "6.0.2")
    dev = _marana()
    for owner in (dev, dev.cam, dev.hdf1, dev.pva1, dev.stats1, dev.image1):
        for key in owner.stage_sigs:
            if isinstance(key, str):
                obj = owner
                for part in key.split("."):
                    obj = getattr(obj, part)  # raises AttributeError if missing
    assert type(dev.hdf1).create_directory.suffix == "CreateDirectory"
