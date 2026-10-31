"""Hidden-focus model for the simulated QERLIN detector (numpy only)."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class FocusModel:
    """Elastic-line width as a function of SM3 pitch (mrad)."""

    best_pitch: float
    sigma_min_px: float = 2.0
    k_px_per_mrad: float = 8.0
    line_x_frac: float = 0.70
    angle_deg: float = 1.5
    peak_counts: float = 2000.0
    background: float = 100.0

    def sigma_px(self, pitch: float) -> float:
        return float(np.sqrt(self.sigma_min_px ** 2
                             + (self.k_px_per_mrad * (pitch - self.best_pitch)) ** 2))

    @classmethod
    def from_seed(cls, seed: int | None) -> "FocusModel":
        if seed is None:
            return cls(best_pitch=0.0)
        rng = np.random.default_rng(seed)
        return cls(best_pitch=float(rng.uniform(-1.0, 1.0)))


def render_frame(model: FocusModel, pitch: float, exposure_s: float,
                 shape: tuple[int, int] = (2048, 2048), rng=None) -> np.ndarray:
    """Render a uint16 frame with a near-vertical Gaussian line."""
    if rng is None:
        rng = np.random.default_rng()
    h, w = shape
    sigma = model.sigma_px(pitch)
    amp = model.peak_counts * exposure_s * (model.sigma_min_px / sigma)
    a = np.deg2rad(model.angle_deg)
    x0 = model.line_x_frac * w
    yc = 0.5 * h
    y = np.arange(h, dtype=np.float32)[:, None]
    x = np.arange(w, dtype=np.float32)[None, :]
    # signed perpendicular distance from the tilted line through (x0, yc)
    d = (x - x0) * np.cos(a) - (y - yc) * np.sin(a)
    sig = amp * np.exp(-0.5 * (d / sigma) ** 2, dtype=np.float32)
    rows = np.zeros((h, 1), dtype=np.float32)
    rows[int(0.2 * h):int(0.8 * h)] = 1.0
    mean = model.background + sig * rows
    img = rng.poisson(mean)
    return np.clip(img, 0, np.iinfo(np.uint16).max).astype(np.uint16)
