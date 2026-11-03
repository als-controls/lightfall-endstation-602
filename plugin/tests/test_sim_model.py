import numpy as np
import pytest

from lightfall_endstation_602.sim.model import FocusModel, render_frame

SMALL = (256, 256)


def _width(img, half=40):
    """RMS width of the line in a window around its peak column (noise kept out)."""
    h, w = img.shape
    band = img[int(0.45 * h):int(0.55 * h)].astype(float).sum(axis=0)
    band = band - np.median(band)
    c = int(np.argmax(np.convolve(band, np.ones(9) / 9, mode="same")))
    lo, hi = max(c - half, 0), min(c + half + 1, w)
    win = np.clip(band[lo:hi], 0, None)
    x = np.arange(lo, hi)
    m = (win * x).sum() / win.sum()
    return np.sqrt((win * (x - m) ** 2).sum() / win.sum())


def test_sigma_minimum_at_best():
    m = FocusModel(best_pitch=0.3)
    assert m.sigma_px(0.3) == m.sigma_min_px
    assert m.sigma_px(1.3) > m.sigma_min_px
    assert m.sigma_px(-0.7) > m.sigma_min_px


def test_sigma_lopsided_pitch_and_sample_x():
    m = FocusModel(best_pitch=104.0, best_sample_x=-0.8)
    assert m.sigma_px(110.0) > m.sigma_px(98.0)  # steeper on the + side
    assert m.sigma_px(104.0, -0.8) == m.sigma_min_px
    assert m.sigma_px(104.0, -0.9) > m.sigma_min_px
    assert m.sigma_px(104.0, -0.7) == pytest.approx(m.sigma_px(104.0, -0.9))


def test_best_sample_x_couples_to_pitch():
    m = FocusModel(best_pitch=104.0, best_sample_x=-0.8, sample_x_per_pitch_mm=0.005)
    assert m.best_sample_x_at(110.0) == -0.8 + 0.03
    # at the shifted best Sample X only the pitch term remains
    assert m.sigma_px(110.0, m.best_sample_x_at(110.0)) == m.sigma_px(110.0)
    assert m.sigma_px(110.0, -0.8) > m.sigma_px(110.0)


def test_seed_reproducible():
    a, b = FocusModel.from_seed(5), FocusModel.from_seed(5)
    assert (a.best_pitch, a.best_sample_x) == (b.best_pitch, b.best_sample_x)
    assert 100.0 <= a.best_pitch <= 114.0
    assert -0.85 <= a.best_sample_x <= -0.70
    none = FocusModel.from_seed(None)
    assert (none.best_pitch, none.best_sample_x) == (104.0, -0.80)


def test_render_shape_dtype():
    img = render_frame(FocusModel(), 104.0, 1.0, shape=(2048, 2048), rng=np.random.default_rng(0))
    assert img.shape == (2048, 2048)
    assert img.dtype == np.uint16


def test_render_width_tracks_pitch():
    m = FocusModel(best_pitch=104.0)
    at_best = render_frame(m, 104.0, 1.0, shape=SMALL, rng=np.random.default_rng(1))
    off = render_frame(m, 116.0, 1.0, shape=SMALL, rng=np.random.default_rng(1))
    assert _width(at_best) < _width(off)


def test_render_width_tracks_sample_x():
    m = FocusModel(best_pitch=104.0, best_sample_x=-0.8)
    at_best = render_frame(m, 104.0, 1.0, shape=SMALL, rng=np.random.default_rng(1),
                           sample_x=-0.8)
    off = render_frame(m, 104.0, 1.0, shape=SMALL, rng=np.random.default_rng(1),
                       sample_x=-0.95)
    assert _width(at_best) < _width(off)
