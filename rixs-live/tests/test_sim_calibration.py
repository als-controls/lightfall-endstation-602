"""The sim focus model reproduces real QERLIN zeroth-order scans through score_frame.

Targets are QERLIN's own scores of the 2026-06-28 scans: SM3 Mirror Pitch scan 004202
(best ~103.7, ~5.1 px; +12 -> ~18.5 px, -6 -> ~8.5 px) and Sample X scans 004206/7
(best ~-0.818 mm; +-0.1 mm -> ~15 px). Each case renders one 2048^2 frame.
"""
import numpy as np
import pytest

from lightfall_endstation_602.sim.model import FocusModel, render_frame
from rixs_live.focus import score_frame

TOL = 0.15
MODEL = FocusModel()
BP, BX = MODEL.best_pitch, MODEL.best_sample_x

CASES = {
    "best": (BP, BX, 5.1),
    "pitch +12": (BP + 12.0, BX, 18.5),
    "pitch -6": (BP - 6.0, BX, 8.5),
    "sample_x -0.1 mm": (BP, BX - 0.1, 15.0),
    "sample_x +0.1 mm": (BP, BX + 0.1, 15.0),
}


@pytest.mark.parametrize("case", list(CASES))
def test_fwhm_matches_real_scan(case):
    pitch, sample_x, target = CASES[case]
    img = render_frame(MODEL, pitch, 1.0, shape=(2048, 2048),
                       rng=np.random.default_rng(3), sample_x=sample_x)
    s = score_frame(img, 2.5, 850.0)
    assert s.score_valid, s.failure_reason
    assert s.fwhm_px == pytest.approx(target, rel=TOL), f"{case}: {s.fwhm_px:.2f} px"


def test_pitch_curve_is_lopsided():
    """Steeper on the + side, like scan 004202."""
    assert MODEL.sigma_px(BP + 6.0, BX) > MODEL.sigma_px(BP - 6.0, BX)
