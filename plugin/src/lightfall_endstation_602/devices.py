"""Real (EPICS) device classes for the 6.0.2 endstation.

Simulated counterparts live in :mod:`lightfall_endstation_602.sim.devices`.
"""

from __future__ import annotations

import logging

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
from ophyd.areadetector.cam import Andor3DetectorCam

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


class Marana(SingleTrigger, Andor3Detector):
    """Andor Marana sCMOS on the ADAndor3 IOC (e.g. prefix ``BL602:marana01:``).

    Plugins used: ``cam1:``, ``image1:`` (live view), ``Stats1:`` (frame
    total/statistics, read per event) and ``HDF1:`` (not read or armed; file
    writing isn't configured yet, so images stay in the IOC).
    """

    _default_read_attrs = ("stats1",)

    cam = C(MaranaCam, "cam1:")
    image1 = C(ImagePlugin, "image1:")
    stats1 = C(StatsPlugin, "Stats1:")
    # Plain plugin for now: no filestore mixin, write path or Capture arming.
    # File writing gets configured once data transfer (shussebora) is set up.
    hdf1 = C(HDF5Plugin, "HDF1:")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # One frame per trigger. ADAndor3 ImageMode is ('Fixed', 'Continuous'),
        # not the generic AD ('Single', 'Multiple', 'Continuous').
        self.stage_sigs.update({"cam.image_mode": "Fixed", "cam.num_images": 1})
        self.stats1.total.kind = Kind.hinted
        # Staging the detector stages its plugins too; until file writing is set
        # up, leave HDF1 untouched (no enable/BlockingCallbacks changes per scan).
        self.hdf1.stage_sigs.clear()
        # Record acquisition geometry/exposure in the configuration documents.
        for attr in ("acquire_time", "acquire_period", "min_x", "min_y", "bin_x", "bin_y"):
            self.cam._set_kind(attr, Kind.config)

    def stop(self, *, success=False):
        self._acquisition_signal.put(0)
        logger.info("%s: stopping acquisition", self.name)
        return super().stop(success=success)
