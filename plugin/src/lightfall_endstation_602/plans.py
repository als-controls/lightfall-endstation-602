"""Bluesky plans for 6.0.2. Pure generators: no lightfall imports."""
from __future__ import annotations

import bluesky.plan_stubs as bps
import bluesky.plans as bp

from .contract import MD_KEY, build_rixs_md


def qerlin_focus_scan(marana, sm3, mono, start: float, stop: float, num: int,
                      exposure_s: float = 1.0, md: dict | None = None):
    """Scan SM3 pitch (mrad) from start to stop in num points, one Marana frame each.

    The start document carries the ``rixs`` contract block rixs-live binds on.
    ``num == 1`` with ``start == stop`` is a single confirming exposure.
    """
    mono_energy = yield from bps.rd(mono)
    frame_shape = tuple(marana.describe()[f"{marana.name}_image"]["shape"])
    yield from bps.mv(marana.exposure_time, exposure_s)
    _md = {MD_KEY: build_rixs_md(f"{marana.name}_image", sm3.name,
                                 frame_shape, float(mono_energy))}
    _md.update(md or {})
    return (yield from bp.scan([marana], sm3, start, stop, num, md=_md))
