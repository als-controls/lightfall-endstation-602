"""Versioned NATS contract shared with the Lightfall plugin (duplicated; keep in sync)."""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

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


@dataclass(frozen=True)
class RixsRunInfo:
    run_uid: str
    image_field: str
    motor_field: str
    frame_shape: tuple[int, int]
    mono_energy_ev: float | None
    motor_units: str = ""  # SM3 pitch in its native units, Sample X in mm; "" if unknown
    image_stream: str = "primary"  # event stream with the image + motor rows
    motor_fields: tuple[str, ...] = ()  # () = just motor_field (a 1D run)
    pva_channel: str = ""  # "" = frames come from Tiled's image_field
    frame_counter_field: str = ""  # event field matching each frame's NTNDArray uniqueId

    @property
    def fields(self) -> tuple[str, ...]:
        """Every motor field of the run, ``motor_field`` first."""
        return self.motor_fields or (self.motor_field,)

    @property
    def uses_pva(self) -> bool:
        return bool(self.pva_channel)

    @property
    def multi_motor(self) -> bool:
        return len(self.fields) > 1


def parse_start_rixs(run_uid: str, start_doc: dict) -> RixsRunInfo | None:
    """Parse the ``rixs`` block of a start doc.

    Returns None if absent; raises ValueError on a wrong version/kind or bad frame_shape.
    """
    md = start_doc.get(MD_KEY)
    if md is None:
        logger.info("run {} has no '{}' metadata; ignoring", run_uid, MD_KEY)
        return None
    if md.get("contract_version") != CONTRACT_VERSION:
        raise ValueError(f"unsupported rixs contract_version {md.get('contract_version')!r}")
    if md.get("kind") != KIND_ZEROTH:
        raise ValueError(f"unsupported rixs kind {md.get('kind')!r}")
    shape = md.get("frame_shape")
    if not isinstance(shape, (list, tuple)) or len(shape) != 2:
        raise ValueError(f"malformed rixs frame_shape {shape!r}")
    try:
        h, w = int(shape[0]), int(shape[1])
        image_field, motor_field = md["image_field"], md["motor_field"]
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError(f"malformed rixs metadata: {exc!r}") from exc
    units = md.get("motor_units")
    stream = md.get("image_stream")
    if stream is None:
        stream = "primary"
    elif not isinstance(stream, str) or not stream:
        raise ValueError(f"malformed rixs image_stream {stream!r}")
    fields = md.get("motor_fields")
    if fields is None:
        fields = ()
    elif (not isinstance(fields, (list, tuple)) or not fields
          or not all(isinstance(f, str) and f for f in fields)):
        raise ValueError(f"malformed rixs motor_fields {fields!r}")
    else:
        fields = tuple(fields)
        if fields[0] != motor_field:
            raise ValueError(f"rixs motor_fields[0] {fields[0]!r} != motor_field "
                             f"{motor_field!r}")
        if len(set(fields)) != len(fields):
            raise ValueError(f"duplicate rixs motor_fields {list(fields)!r}")
        if len(fields) == 1:
            fields = ()  # same as a plain 1D run
    channel, counter = md.get("pva_channel"), md.get("frame_counter_field")
    if channel is None and counter is None:
        channel = counter = ""
    elif not (isinstance(channel, str) and channel and isinstance(counter, str) and counter):
        raise ValueError(f"rixs pva_channel {channel!r} and frame_counter_field {counter!r} "
                         f"must both be non-empty strings (or both absent)")
    return RixsRunInfo(run_uid, image_field, motor_field, (h, w), md.get("mono_energy_ev"),
                       units if isinstance(units, str) else "", stream, fields,
                       channel, counter)
