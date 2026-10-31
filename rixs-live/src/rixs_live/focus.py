"""Zeroth-order (spectrometer focus) scoring, focus-curve fit and frame preview.

Pure functions over numpy arrays; every value returned is a plain JSON-able
Python scalar/list so it can go straight into a NATS message.
"""
from __future__ import annotations

import base64
import math
from dataclasses import asdict, dataclass

import numpy as np

from rixs_app.core.zeroth_order import run_zeroth_order_pipeline

try:
    from rixs_app.core.cli_utils import fit_focus_curve as _qerlin_fit
except ImportError:  # older rixs_app without the extracted helper
    def _qerlin_fit(x, fwhm):
        # Same degree-2 fit as rixs_app.core.cli_utils.export_focus_curve.
        if len(x) < 3:
            return None, None
        coeffs = np.polyfit(x, fwhm, 2)
        if coeffs[0] <= 1e-12:  # degenerate / concave-down parabola
            return None, [float(c) for c in coeffs]
        return float(-coeffs[1] / (2 * coeffs[0])), [float(c) for c in coeffs]


def _f(v) -> float | None:
    """numpy/py scalar -> finite float, else None."""
    if v is None:
        return None
    v = float(v)
    return v if math.isfinite(v) else None


@dataclass
class FrameScore:
    fwhm_px: float | None
    fwhm_mev: float | None
    resolving_power: float | None
    score_valid: bool
    failure_reason: str | None
    centroid: tuple[float, float] | None
    angle_deg: float | None

    def to_dict(self) -> dict:
        d = asdict(self)
        if d["centroid"] is not None:
            d["centroid"] = list(d["centroid"])
        return d


def score_frame(img: np.ndarray, dispersion_mev_per_px: float,
                mono_energy_ev: float | None) -> FrameScore:
    """Score one detector frame with QERLIN's zeroth-order pipeline.

    Valid only when the evaluator ran and reported ``score_valid``; the
    pipeline's ``score`` key is never used (it falls back to a different
    unit when the evaluator is invalid).
    """
    disp = float(dispersion_mev_per_px or 0.0)
    try:
        res = run_zeroth_order_pipeline(np.asarray(img, dtype=np.float32),
                                        energy_dispersion=disp)
    except Exception as exc:  # noqa: BLE001 - any pipeline failure is a bad frame
        return FrameScore(None, None, None, False,
                          f"{type(exc).__name__}: {exc}", None, None)

    ev = res.get("evaluator_result")
    fit_ok = bool(res.get("fit_ok"))
    centroid = None
    angle = None
    if fit_ok:
        c = res.get("centroid")
        if c is not None and len(c) == 2:
            cx, cy = _f(c[0]), _f(c[1])
            centroid = (cx, cy) if cx is not None and cy is not None else None
        angle = _f(res.get("angle_deg"))

    fwhm_px = _f(ev.fwhm_px) if ev is not None and ev.score_valid else None
    if fwhm_px is None or fwhm_px <= 0:
        if ev is not None and ev.failure_reason:
            reason = str(ev.failure_reason)
        elif res.get("failure_reason"):
            reason = str(res["failure_reason"])
        elif ev is None:
            reason = "line not found"
        else:
            reason = "evaluator returned no valid FWHM"
        return FrameScore(None, None, None, False, reason, centroid, angle)

    fwhm_mev = fwhm_px * disp if disp > 0 else None
    rp = None
    if fwhm_mev is not None and mono_energy_ev is not None and mono_energy_ev > 0:
        rp = float(mono_energy_ev) / (fwhm_mev * 1e-3)
    return FrameScore(fwhm_px, fwhm_mev, rp, True, None, centroid, angle)


def fit_focus_curve(positions: list[float], fwhm_px: list[float]) -> dict:
    """Parabolic fit of FWHM vs. motor position -> ``{ok, optimal_position, coeffs, reason}``.

    Points whose position or width is None/NaN (invalid frames) are dropped.
    """
    pts = [(_f(x), _f(y)) for x, y in zip(positions, fwhm_px)]
    pts = [(x, y) for x, y in pts if x is not None and y is not None]
    if len(pts) < 3 or len({x for x, _ in pts}) < 3:
        return {"ok": False, "optimal_position": None, "coeffs": None,
                "reason": "fewer than 3 valid points"}
    xs = [x for x, _ in pts]
    ys = [y for _, y in pts]
    opt, coeffs = _qerlin_fit(xs, ys)
    coeffs = None if coeffs is None else [float(c) for c in coeffs]
    if opt is None:
        return {"ok": False, "optimal_position": None, "coeffs": coeffs,
                "reason": "curve not concave-up (no minimum)"}
    opt = float(opt)
    if not (min(xs) <= opt <= max(xs)):
        return {"ok": False, "optimal_position": opt, "coeffs": coeffs,
                "reason": "optimum outside scanned range"}
    return {"ok": True, "optimal_position": opt, "coeffs": coeffs, "reason": None}


def make_preview(img: np.ndarray, max_side: int = 256) -> dict:
    """Block-mean downsample to <= ``max_side`` and pack as base64 uint8.

    Returns ``{b64, shape: [h, w], scale}``; the bytes are row-major uint8,
    normalized to the 0.5-99.5 percentile range. Edge rows/columns that do
    not fill a whole block are dropped.
    """
    a = np.asarray(img, dtype=np.float32)
    h, w = a.shape
    scale = max(1, math.ceil(max(h, w) / max_side))
    hh, ww = h // scale, w // scale
    a = a[:hh * scale, :ww * scale].reshape(hh, scale, ww, scale).mean(axis=(1, 3))
    lo, hi = np.percentile(a, [0.5, 99.5])
    if hi > lo:
        u8 = np.clip((a - lo) / (hi - lo) * 255.0, 0, 255).astype(np.uint8)
    else:
        u8 = np.zeros(a.shape, dtype=np.uint8)
    return {"b64": base64.b64encode(np.ascontiguousarray(u8).tobytes()).decode("ascii"),
            "shape": [int(hh), int(ww)], "scale": int(scale)}
