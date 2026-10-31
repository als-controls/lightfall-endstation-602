"""Versioned NATS contract shared with the Lightfall plugin (duplicated; keep in sync)."""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

CONTRACT_VERSION = 1
MD_KEY = "rixs"
KIND_ZEROTH = "zeroth_order_focus"
SUBJ_BIND, SUBJ_STOP = "rixs.run.bind", "rixs.run.stop"
SUBJ_STATUS, SUBJ_ERROR = "rixs.status", "rixs.error"
SUBJ_FRAME, SUBJ_COMPLETE = "rixs.zeroth.frame", "rixs.zeroth.complete"
# request/reply: {"run_uid"} -> complete payload | {"error"}
SUBJ_RESULT = "rixs.zeroth.result"
SUBJ_DISCOVER = "_rixs.discover"


@dataclass(frozen=True)
class RixsRunInfo:
    run_uid: str
    image_field: str
    motor_field: str
    frame_shape: tuple[int, int]
    mono_energy_ev: float | None


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
    return RixsRunInfo(run_uid, image_field, motor_field, (h, w), md.get("mono_energy_ev"))
