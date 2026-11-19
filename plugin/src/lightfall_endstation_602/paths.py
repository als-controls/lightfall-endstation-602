"""Detector write-path templates keyed on the beamline id.

The data-movement service (shussebora) copies files relative to ``/data``, so
writing to ``/data/<beamline>/<detector>/%Y/%m/%d/`` gives every site distinct,
identical paths on all destinations (``bcgtiled:/data/6.0.2/marana01/...``).

The IOC must find ``/data/<beamline>/<detector>/`` already in place: the HDF5
plugin stages with ``create_directory = -3``, so the IOC creates only the
``%Y/%m/%d`` levels, and a missing parent fails staging with
``Path ... does not exist on IOC``. On 602marana ``/data/6.0.2/marana01`` exists,
owned by ``epics`` (the user ``softioc-602marana`` runs as).

Same pattern as ``lightfall_endstation_7011.devices.paths``; a shared copy belongs
in lightfall-utils once a third endstation needs it.
"""

from __future__ import annotations

import logging

from ophyd.areadetector.base import ADComponent

logger = logging.getLogger(__name__)

_warned = False


def _beamline() -> str:
    """The ``tiled_beamline`` preference (e.g. ``"6.0.2"``), or ``""``."""
    try:
        from lightfall.ui.preferences.manager import PreferencesManager

        return str(PreferencesManager.get_instance().get("tiled_beamline", "") or "")
    except Exception:  # noqa: BLE001
        logger.debug("tiled_beamline preference unavailable", exc_info=True)
        return ""


def data_write_template(detector: str, root: str = "/data/") -> str:
    """Return ``<root><beamline>/<detector>/%Y/%m/%d/``.

    Falls back to ``<root><detector>/%Y/%m/%d/`` (warning once) when the
    ``tiled_beamline`` preference is unset.
    """
    global _warned
    beamline = _beamline()
    if not beamline:
        if not _warned:
            _warned = True
            logger.warning(
                "tiled_beamline preference is unset; %s files will be written "
                "without a beamline directory under %s",
                detector,
                root,
            )
        return f"{root}{detector}/%Y/%m/%d/"
    return f"{root}{beamline}/{detector}/%Y/%m/%d/"


class BeamlineDataComponent(ADComponent):
    """HDF5-plugin component whose write path carries the beamline id.

    The template is applied when the (lazy) plugin is instantiated, so the
    ``tiled_beamline`` preference is read at use rather than at import, and
    constructing the device does not eagerly instantiate the plugin. The
    ``write_path_template`` passed to the component is only the construction-time
    default; it is replaced here.
    """

    def __init__(self, cls, suffix=None, *, detector: str, **kwargs):
        super().__init__(cls, suffix, **kwargs)
        self._detector = detector

    def create_component(self, instance):
        plugin = super().create_component(instance)
        plugin.write_path_template = data_write_template(self._detector)
        return plugin
