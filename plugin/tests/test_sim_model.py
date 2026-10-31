import numpy as np

from lightfall_endstation_602.sim.model import FocusModel, render_frame

SMALL = (256, 256)


def _width(img):
    h, w = img.shape
    band = img[int(0.45 * h):int(0.55 * h)].astype(float).sum(axis=0)
    band = band - np.median(band)
    band = np.clip(band, 0, None)
    x = np.arange(w)
    m = (band * x).sum() / band.sum()
    return np.sqrt((band * (x - m) ** 2).sum() / band.sum())


def test_sigma_minimum_at_best():
    m = FocusModel(best_pitch=0.3)
    assert m.sigma_px(0.3) == m.sigma_min_px
    assert m.sigma_px(1.3) > m.sigma_min_px
    assert m.sigma_px(-0.7) > m.sigma_min_px


def test_seed_reproducible():
    assert FocusModel.from_seed(5).best_pitch == FocusModel.from_seed(5).best_pitch
    assert -1.0 <= FocusModel.from_seed(5).best_pitch <= 1.0
    assert FocusModel.from_seed(None).best_pitch == 0.0


def test_render_shape_dtype():
    img = render_frame(FocusModel(0.0), 0.0, 1.0, shape=(2048, 2048), rng=np.random.default_rng(0))
    assert img.shape == (2048, 2048)
    assert img.dtype == np.uint16


def test_render_width_tracks_pitch():
    m = FocusModel(best_pitch=0.2)
    at_best = render_frame(m, 0.2, 1.0, shape=SMALL, rng=np.random.default_rng(1))
    off = render_frame(m, 1.7, 1.0, shape=SMALL, rng=np.random.default_rng(1))
    assert _width(at_best) < _width(off)
