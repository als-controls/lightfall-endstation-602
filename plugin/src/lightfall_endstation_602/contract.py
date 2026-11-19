"""Versioned NATS contract shared with the rixs-live service (duplicated; keep in sync)."""

from __future__ import annotations

CONTRACT_VERSION = 2  # 2: rixs.run.bind carries tiled_proxy (str | None)
MD_KEY = "rixs"
KIND_ZEROTH = "zeroth_order_focus"
SUBJ_BIND, SUBJ_STOP = "rixs.run.bind", "rixs.run.stop"
# bind: {run_uid, tiled_url, tiled_proxy, lightfall_prefix, contract_version};
# tiled_proxy is the proxy Lightfall itself uses for tiled_url (e.g.
# "socks5://localhost:1080") or None. Never an API key: the subject is public.
SUBJ_STATUS, SUBJ_ERROR = "rixs.status", "rixs.error"
SUBJ_FRAME, SUBJ_COMPLETE = "rixs.zeroth.frame", "rixs.zeroth.complete"
# request/reply: {"run_uid"} -> complete payload | {"error"}
SUBJ_RESULT = "rixs.zeroth.result"
SUBJ_DISCOVER = "_rixs.discover"
# Frame payload geometry (SUBJ_FRAME): ``centroid`` is [x, y] in full-resolution
# detector pixels with x = column and y = row (row 0 = first row of the frame).
# ``angle_deg`` is the line direction measured from +x toward +y in those image
# coordinates, i.e. the direction vector is (cos, sin) in (column, row); a
# near-vertical line reads ~ +/-90. ``preview`` is row-major uint8, shape [h, w],
# block-downsampled by ``scale`` (full-res coord / scale = preview coord).
# The start doc's optional ``motor_units`` (added within v2; older senders omit it,
# read as "") is echoed as ``motor_units`` in frame and complete payloads.
# Also optional within v2 (older senders omit them):
# - ``image_stream`` (default "primary"): the event stream holding the image and
#   motor rows (``qerlin_optimize`` uses "frames"; seq_num counts within it).
# - ``motor_fields`` (default [motor_field]): every motor of an N-D run, first ==
#   ``motor_field``. Frame payloads always carry ``positions: {field: value}``
#   (``position`` stays the first motor). A run with more than one motor gets no
#   1D fit (``fit.ok`` false, reason "multi-motor run: no 1D fit"); its complete
#   payload's points carry ``positions`` and ``best_measured`` is the lowest-FWHM
#   (highest-score) point with all its positions.
# - ``pva_channel`` + ``frame_counter_field`` (both or neither): read each frame's
#   pixels from that areaDetector PVA channel (NTNDArray) instead of Tiled. A real
#   Marana writes its image to an external HDF5 file that Tiled can only serve once
#   it has been copied off the IOC host, at the end of capture. Event rows (seq_num,
#   motors) still come from Tiled; row -> frame is the NTNDArray ``uniqueId`` equal to
#   the row's ``frame_counter_field`` value (the cam's ArrayCounter_RBV).


def build_rixs_md(
    image_field: str,
    motor_field: str,
    frame_shape: tuple[int, int],
    mono_energy_ev: float | None,
    motor_units: str = "",
    image_stream: str | None = None,
    motor_fields: list[str] | None = None,
    pva_channel: str | None = None,
    frame_counter_field: str | None = None,
) -> dict:
    """Build the ``rixs`` start-document metadata block.

    ``motor_units`` (optional, "" if unknown) labels ``motor_field``: SM3 pitch in its
    native units, Sample X in mm. ``image_stream`` and ``motor_fields`` are written
    only when given (an N-D optimization run); ``motor_fields[0]`` must be
    ``motor_field``. ``pva_channel`` and ``frame_counter_field`` (both or neither)
    send rixs-live to the detector's PVA plugin for the pixels (a real Marana).
    """
    md = {
        "contract_version": CONTRACT_VERSION,
        "kind": KIND_ZEROTH,
        "image_field": image_field,
        "motor_field": motor_field,
        "frame_shape": [int(frame_shape[0]), int(frame_shape[1])],
        "mono_energy_ev": mono_energy_ev,
        "motor_units": motor_units or "",
    }
    if bool(pva_channel) != bool(frame_counter_field):
        raise ValueError("pva_channel and frame_counter_field go together")
    if pva_channel:
        md["pva_channel"] = str(pva_channel)
        md["frame_counter_field"] = str(frame_counter_field)
    if image_stream:
        md["image_stream"] = image_stream
    if motor_fields:
        fields = [str(f) for f in motor_fields]
        if fields[0] != motor_field:
            raise ValueError(f"motor_fields[0] {fields[0]!r} != motor_field {motor_field!r}")
        md["motor_fields"] = fields
    return md
