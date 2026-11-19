"""Bluesky plans for 6.0.2. Pure generators: no lightfall imports."""
from __future__ import annotations

import bluesky.plan_stubs as bps
import bluesky.plans as bp

from .contract import MD_KEY, build_rixs_md


def motor_units(motor) -> str:
    """Engineering units of ``motor``: its describe() ``units``, else ``egu``, else ""."""
    try:
        units = motor.describe().get(motor.name, {}).get("units")
    except Exception:  # noqa: BLE001 - units are informational only
        units = None
    if not units:
        units = getattr(motor, "egu", None)
    return units if isinstance(units, str) else ""


def frame_shape(marana) -> tuple[int, int]:
    """``(height, width)`` of one Marana frame, before the run starts.

    The simulated Marana describes ``<name>_image`` with shape ``(h, w)`` up front. The
    real one (file-store HDF1) only adds that key once it has been staged and
    triggered, and then as ``(num_images, h, w)``, so before a run its cam's
    ``array_size`` is the answer. Either way only the last two dimensions are a frame.
    """
    key = f"{marana.name}_image"
    desc = marana.describe()
    if key in desc and desc[key].get("shape"):
        h, w = tuple(desc[key]["shape"])[-2:]
        return int(h), int(w)
    size = marana.cam.array_size
    return int(size.array_size_y.get()), int(size.array_size_x.get())


def qerlin_focus_scan(marana, motor, mono, start: float, stop: float, num: int,
                      exposure_s: float = 1.0, md: dict | None = None):
    """Scan one focusing motor from start to stop in num points, one Marana frame each.

    ``motor`` is any positioner (usually SM3 pitch, in its native units, or Sample X,
    in mm); start/stop are in that motor's units. The start document carries the
    ``rixs`` contract block rixs-live binds on. ``num == 1`` with ``start == stop``
    is a single confirming exposure.
    """
    mono_energy = yield from bps.rd(mono)
    shape = frame_shape(marana)
    yield from bps.mv(marana.exposure_time, exposure_s)
    _md = {MD_KEY: build_rixs_md(f"{marana.name}_image", motor.name,
                                 shape, float(mono_energy),
                                 motor_units=motor_units(motor))}
    _md.update(md or {})
    return (yield from bp.scan([marana], motor, start, stop, num, md=_md))
