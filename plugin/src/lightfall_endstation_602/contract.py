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


def build_rixs_md(
    image_field: str,
    motor_field: str,
    frame_shape: tuple[int, int],
    mono_energy_ev: float | None,
    motor_units: str = "",
) -> dict:
    """Build the ``rixs`` start-document metadata block.

    ``motor_units`` (optional, "" if unknown) labels ``motor_field``: SM3 pitch in its
    native units, Sample X in mm.
    """
    return {
        "contract_version": CONTRACT_VERSION,
        "kind": KIND_ZEROTH,
        "image_field": image_field,
        "motor_field": motor_field,
        "frame_shape": [int(frame_shape[0]), int(frame_shape[1])],
        "mono_energy_ev": mono_energy_ev,
        "motor_units": motor_units or "",
    }
