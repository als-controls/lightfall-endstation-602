"""Versioned NATS contract shared with the rixs-live service (duplicated; keep in sync)."""

from __future__ import annotations

CONTRACT_VERSION = 1
MD_KEY = "rixs"
KIND_ZEROTH = "zeroth_order_focus"
SUBJ_BIND, SUBJ_STOP = "rixs.run.bind", "rixs.run.stop"
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


def build_rixs_md(
    image_field: str,
    motor_field: str,
    frame_shape: tuple[int, int],
    mono_energy_ev: float | None,
) -> dict:
    """Build the ``rixs`` start-document metadata block."""
    return {
        "contract_version": CONTRACT_VERSION,
        "kind": KIND_ZEROTH,
        "image_field": image_field,
        "motor_field": motor_field,
        "frame_shape": [int(frame_shape[0]), int(frame_shape[1])],
        "mono_energy_ev": mono_energy_ev,
    }
