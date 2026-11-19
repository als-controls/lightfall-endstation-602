"""Real (EPICS) device classes for the 6.0.2 endstation.

Simulated counterparts live in :mod:`lightfall_endstation_602.sim.devices`.
"""

from __future__ import annotations

import logging
import time
from pathlib import PurePosixPath

import numpy as np
from ophyd import (
    Andor3Detector,
    EpicsSignalRO,
    HDF5Plugin,
    ImagePlugin,
    Kind,
    SingleTrigger,
    StatsPlugin,
)
from ophyd.areadetector.base import ADComponent as C
from ophyd.areadetector.base import EpicsSignalWithRBV
from ophyd.areadetector.cam import Andor3DetectorCam
from ophyd.areadetector.plugins import PluginBase
from ophyd.areadetector.filestore_mixins import (
    FileStoreHDF5IterativeWrite,
    FileStorePluginBase,
    resource_factory,
)
from ophyd.utils import set_and_wait

from .paths import BeamlineDataComponent

logger = logging.getLogger(__name__)


class MaranaCam(Andor3DetectorCam):
    """ADAndor3 (Andor SDK3) cam1.

    ophyd's ``Andor3DetectorCam`` matches ``andor3.template``; every one of its PVs
    was checked against ``BL602:marana01:cam1:``. The SDK2 ``Andor*`` PVs
    (AndorTempStatus_RBV, AndorShutterMode, PALFilePath, ...) don't exist on ADAndor3.
    The intensifier-only records (GateMode, InsertionDelay, MCP*) read "N.A." on a
    Marana and are left out. Overrides here make status/info read-only strings.
    """

    temp_status = C(EpicsSignalRO, "TempStatus_RBV", string=True)
    temperature_actual = C(EpicsSignalRO, "TemperatureActual")
    full_aoi_control = C(EpicsSignalRO, "FullAOIControl", string=True)
    readout_time = C(EpicsSignalRO, "ReadoutTime")
    transfer_rate = C(EpicsSignalRO, "TransferRate")
    serial_number = C(EpicsSignalRO, "SerialNumber", string=True)
    firmware_version = C(EpicsSignalRO, "FirmwareVersion", string=True)
    software_version = C(EpicsSignalRO, "SoftwareVersion", string=True)
    controller_id = C(EpicsSignalRO, "ControllerID", string=True)


class MaranaHDF5Plugin(HDF5Plugin, FileStoreHDF5IterativeWrite):
    """``HDF1:`` writing one HDF5 file per staging, registered as an external resource.

    * ``frames_per_point`` follows ``cam.num_images`` (one Fixed-mode frame per trigger).
    * POSIX path semantics throughout (``reg_root``, ``read_path_template`` and the
      resource path), so the IOC-side ``/data/...`` paths stay ``/``-separated in the
      documents when the client runs on Windows; upstream ophyd uses the OS path type
      there. Same overrides as lightfall-endstation-7011's PIMTE3 plugin.
    * Warmup: the HDF5 plugin must see one array before it can capture (its dimensions
      come from that array). ADAndor3's ImageMode is ``Fixed``/``Continuous``, not the
      generic ``Single``, so ophyd's stock warmup would fail here.
    * Directories: staging sets ``CreateDirectory`` so the IOC creates the missing tail
      of the write path itself. ``<beamline>/marana01/%Y/%m/%d`` is 5 levels below
      ``/data``, so a new day, or a new beamline directory, needs no manual mkdir and
      doesn't depend on the IOC's own (non-autosaved) CreateDirectory value.
    """

    _WARMUP_TIMEOUT_S = 30.0
    # Negative N: the IOC may create up to |N| missing trailing directories.
    CREATE_DIRECTORY_DEPTH = -5
    path_semantics = "posix"

    # ophyd declares CreateDirectory only on its versioned FilePlugin_V22+ classes,
    # not on the plain HDF5Plugin this subclasses; the IOC (ADCore 3.14) has it.
    create_directory = C(EpicsSignalWithRBV, "CreateDirectory", kind="config")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stage_sigs["create_directory"] = self.CREATE_DIRECTORY_DEPTH

    @FileStorePluginBase.reg_root.setter
    def reg_root(self, val):
        if val is None:
            val = "/"
        root = PurePosixPath(val)
        if not root.is_absolute():
            raise ValueError(f"The root part of the path must be absolute not {root=}.")
        self._root = root

    @property
    def read_path_template(self):
        if self._read_path_template is None:
            ret = PurePosixPath(self.write_path_template)
        else:
            ret = PurePosixPath(self._read_path_template)
        ret = self._ensure_absolute_under_root(ret)
        return str(ret) + "/"

    @read_path_template.setter
    def read_path_template(self, val):
        if val is not None:
            val = str(PurePosixPath(val)) + "/"
        self._read_path_template = val

    def _generate_resource(self, resource_kwargs):
        fn = PurePosixPath(self._fn).relative_to(self.reg_root)
        resource, self._datum_factory = resource_factory(
            spec=self.filestore_spec,
            root=str(self.reg_root),
            resource_path=str(fn),
            resource_kwargs=resource_kwargs,
            path_semantics=self.path_semantics,
        )
        if self._reg is not None:
            self._resource_uid = self._reg.register_resource(
                rpath=resource["resource_path"],
                rkwargs=resource["resource_kwargs"],
                root=resource["root"],
                spec=resource["spec"],
                path_semantics=resource["path_semantics"],
            )
            resource["uid"] = self._resource_uid
        self._resource_uid = resource["uid"]
        self._asset_docs_cache.append(("resource", resource))

    def get_frames_per_point(self):
        return self.parent.cam.num_images.get()

    @property
    def _warmed_up(self) -> bool:
        return bool(np.array(self.array_size.get()).sum() > 0)

    def stage(self):
        if not self._warmed_up:
            self.warmup()
        return super().stage()

    def warmup(self):
        """Prime the plugin with one short Fixed-mode acquisition, then restore the cam."""
        cam = self.parent.cam
        set_and_wait(self.enable, 1)
        sigs = [
            (cam.array_callbacks, 1),
            (cam.image_mode, "Fixed"),
            (cam.num_images, 1),
            (cam.trigger_mode, "Internal"),
            (cam.acquire_time, 0.01),
        ]
        original = [(sig, sig.get()) for sig, _ in sigs]
        try:
            for sig, val in sigs:
                set_and_wait(sig, val)
            cam.acquire.put(1)  # don't wait for completion: the array check below does
            deadline = time.monotonic() + self._WARMUP_TIMEOUT_S
            while not self._warmed_up and time.monotonic() < deadline:
                time.sleep(0.1)
        finally:
            for sig, val in reversed(original):
                set_and_wait(sig, val)
        if not self._warmed_up:
            raise RuntimeError(f"{self.name}: HDF5 plugin saw no array within "
                               f"{self._WARMUP_TIMEOUT_S:.0f} s of a warmup frame")


class MaranaPvaPlugin(PluginBase):
    """``Pva1:``: serves each NDArray as an NTNDArray on ``PvName_RBV``.

    rixs-live reads the frames from here while a scan runs (the HDF5 file reaches
    Tiled only after capture ends). Staging enables it with blocking callbacks
    (PluginBase), so each frame is posted before the acquisition completes.
    ``PvName_RBV`` is a char waveform, hence ``string=True`` (ophyd's PvaPlugin
    reads it as numbers).
    """

    _plugin_type = "NDPluginPva"
    pv_name = C(EpicsSignalRO, "PvName_RBV", string=True)


class Marana(SingleTrigger, Andor3Detector):
    """Andor Marana sCMOS on the ADAndor3 IOC (e.g. prefix ``BL602:marana01:``).

    Plugins used: ``cam1:``, ``image1:`` (live view), ``Stats1:`` (frame
    total/statistics, read per event), ``Pva1:`` (frames for rixs-live, matched to
    events by ``array_counter``, the cam's ``ArrayCounter_RBV``, which equals each
    NDArray's ``uniqueId``) and ``HDF1:``, which writes one HDF5 file per
    run under ``/data/<tiled_beamline>/marana01/%Y/%m/%d/`` (e.g.
    ``/data/6.0.2/marana01/...``) and adds the ``<name>_image`` field as an external
    resource. shussebora on 602marana copies each file to bcgtiled when capture ends
    and prunes the local copy once it is there.
    """

    _default_read_attrs = ("hdf1", "stats1", "array_counter")

    cam = C(MaranaCam, "cam1:")
    image1 = C(ImagePlugin, "image1:")
    stats1 = C(StatsPlugin, "Stats1:")
    pva1 = C(MaranaPvaPlugin, "Pva1:")
    # auto_monitor=False: every read is a fresh CA get, made after the trigger has
    # completed, so the counter can never be a cached value from the previous frame
    # (which would pair this point with the wrong PVA frame, silently).
    array_counter = C(EpicsSignalRO, "cam1:ArrayCounter_RBV", auto_monitor=False)
    # The template given here is only the construction-time default:
    # BeamlineDataComponent replaces it from the tiled_beamline preference.
    hdf1 = BeamlineDataComponent(
        MaranaHDF5Plugin,
        "HDF1:",
        detector="marana01",
        write_path_template="/data/marana01/%Y/%m/%d/",
        root="/data/",
        reg=None,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # One frame per trigger. ADAndor3 ImageMode is ('Fixed', 'Continuous'),
        # not the generic AD ('Single', 'Multiple', 'Continuous').
        self.stage_sigs.update({"cam.image_mode": "Fixed", "cam.num_images": 1})
        self.stats1.total.kind = Kind.hinted
        # Record acquisition geometry/exposure in the configuration documents.
        for attr in ("acquire_time", "acquire_period", "min_x", "min_y", "bin_x", "bin_y"):
            self.cam._set_kind(attr, Kind.config)

    def stop(self, *, success=False):
        self._acquisition_signal.put(0)
        logger.info("%s: stopping acquisition", self.name)
        return super().stop(success=success)
