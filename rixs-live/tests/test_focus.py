import base64
import json
import math

import numpy as np
import pytest

from lightfall_endstation_602.sim.model import FocusModel, render_frame
from rixs_live.focus import FrameScore, fit_focus_curve, make_preview, score_frame

BEST = 0.3
SHAPE = (2048, 2048)  # real Marana geometry; the finder rejects broad lines on small frames


def _frame(pitch, model=None, seed=1):
    model = model or FocusModel(best_pitch=BEST)
    return render_frame(model, pitch, 1.0, shape=SHAPE, rng=np.random.default_rng(seed))


@pytest.fixture(scope="module")
def best_frame():
    return _frame(BEST)


@pytest.fixture(scope="module")
def scores(best_frame):
    return {
        0.0: score_frame(best_frame, 0.0, None),
        1.0: score_frame(_frame(BEST + 1.0), 0.0, None),
        2.0: score_frame(_frame(BEST + 2.0), 0.0, None),
    }


def test_score_valid_on_sim_frame(scores):
    s = scores[0.0]
    assert isinstance(s, FrameScore)
    assert s.score_valid is True
    assert 2 < s.fwhm_px < 20
    assert s.failure_reason is None
    # Everything goes into a NATS message: must be plain JSON
    d = s.to_dict()
    json.dumps(d)
    assert type(d["fwhm_px"]) is float
    assert type(d["angle_deg"]) is float
    assert all(type(c) is float for c in d["centroid"])


def test_fwhm_grows_away_from_best(scores):
    assert all(scores[k].score_valid for k in scores)
    assert scores[0.0].fwhm_px < scores[1.0].fwhm_px < scores[2.0].fwhm_px


def test_invalid_frame_excluded():
    img = _frame(BEST, model=FocusModel(best_pitch=BEST, peak_counts=0.0))
    s = score_frame(img, 2.5, 850.0)
    assert s.score_valid is False
    assert s.fwhm_px is None
    assert s.fwhm_mev is None
    assert s.resolving_power is None
    assert s.failure_reason
    json.dumps(s.to_dict())


def test_pipeline_exception_caught():
    s = score_frame(np.zeros((3, 4, 5)), 2.5, 850.0)
    assert s.score_valid is False
    assert s.fwhm_px is None
    assert "2D" in s.failure_reason


def test_resolving_power_requires_both(best_frame):
    s0 = score_frame(best_frame, 0.0, 850.0)
    assert s0.score_valid
    assert s0.resolving_power is None
    assert s0.fwhm_mev is None
    s_nomono = score_frame(best_frame, 2.5, None)
    assert s_nomono.resolving_power is None
    assert s_nomono.fwhm_mev == pytest.approx(s_nomono.fwhm_px * 2.5)
    s = score_frame(best_frame, 2.5, 850.0)
    assert s.score_valid
    assert s.resolving_power == pytest.approx(850 / (s.fwhm_px * 2.5e-3))


def _model_fwhm(pitch):
    return 2.3548 * FocusModel(best_pitch=BEST).sigma_px(pitch)


def test_fit_recovers_best():
    xs = list(np.linspace(BEST - 1.5, BEST + 1.5, 11))
    rng = np.random.default_rng(0)
    ys = [_model_fwhm(x) * (1 + 0.03 * rng.standard_normal()) for x in xs]
    r = fit_focus_curve(xs, ys)
    assert r["ok"] is True
    assert r["reason"] is None
    assert abs(r["optimal_position"] - BEST) < 0.15
    assert len(r["coeffs"]) == 3
    json.dumps(r)


def test_fit_short_and_concave():
    r = fit_focus_curve([0.0, 1.0], [5.0, 6.0])
    assert r == {"ok": False, "optimal_position": None, "coeffs": None,
                 "reason": "fewer than 3 valid points"}
    # NaN/None widths (invalid frames) don't count as valid points
    r = fit_focus_curve([0.0, 1.0, 2.0], [5.0, float("nan"), 6.0])
    assert r["ok"] is False and r["reason"] == "fewer than 3 valid points"
    r = fit_focus_curve([0.0, 1.0, 2.0], [5.0, 8.0, 5.0])
    assert r["ok"] is False
    assert r["optimal_position"] is None
    assert r["reason"] == "curve not concave-up (no minimum)"
    xs = [0.0, 0.5, 1.0, 1.5]
    ys = [(x - 3.0) ** 2 + 2 for x in xs]
    r = fit_focus_curve(xs, ys)
    assert r["ok"] is False
    assert r["reason"] == "optimum outside scanned range"
    assert r["optimal_position"] == pytest.approx(3.0)


def test_preview_bounds(best_frame):
    p = make_preview(best_frame)
    assert max(p["shape"]) <= 256
    raw = base64.b64decode(p["b64"])
    assert len(raw) == math.prod(p["shape"])
    assert p["scale"] == 8
    arr = np.frombuffer(raw, dtype=np.uint8)
    assert arr.max() > arr.min()
    json.dumps(p)


def test_preview_non_divisible_shape():
    img = np.arange(300 * 500, dtype=np.float32).reshape(300, 500)
    p = make_preview(img, max_side=256)
    assert max(p["shape"]) <= 256
    assert len(base64.b64decode(p["b64"])) == math.prod(p["shape"])


@pytest.mark.parametrize("tilt_deg", [1.5, 6.0, -6.0])
def test_centroid_and_angle_follow_contract(tilt_deg):
    """Known-angle frame: centroid is (col, row) on the line; angle is from +x toward +y."""
    model = FocusModel(best_pitch=BEST, angle_deg=tilt_deg)
    s = score_frame(_frame(BEST, model), 0.0, None)
    assert s.score_valid and s.centroid is not None and s.angle_deg is not None
    h, w = SHAPE
    # sim line passes through (x0, yc) with direction (sin t, cos t) in (col, row)
    x0, yc = model.line_x_frac * w, 0.5 * h
    t = math.radians(tilt_deg)
    cx, cy = s.centroid
    perp = (cx - x0) * math.cos(t) - (cy - yc) * math.sin(t)
    assert abs(perp) < 5.0, f"centroid {s.centroid} is {perp:.1f}px off the line"
    a = math.radians(s.angle_deg)
    # direction parallel to (sin t, cos t), up to sign
    assert abs(math.cos(a) * math.sin(t) + math.sin(a) * math.cos(t)) > math.cos(math.radians(1.0))
