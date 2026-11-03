"""Hidden-focus model for the simulated QERLIN detector (numpy only).

Calibrated against real QERLIN zeroth-order scans (2026-06-28, scans 004202-004209)
scored with rixs-live's ``score_frame``: about 5.1 px FWHM at best focus; SM3 pitch
+12 units -> ~18.5 px and -6 units -> ~8.5 px (steeper on the + side); Sample X
+-0.1 mm -> ~15 px. ``score_frame`` reports FWHM^2 ~= 5.56 sigma^2 + 13.5 px^2 on
these frames, so the constants below are line sigmas, not FWHMs.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# Seeded ranges for the hidden optimum (LIGHTFALL_SIM_SEED); None seed -> defaults.
BEST_PITCH_RANGE = (100.0, 114.0)
BEST_SAMPLE_X_RANGE = (-0.85, -0.70)
DEFAULT_BEST_PITCH = 104.0
DEFAULT_BEST_SAMPLE_X = -0.80


@dataclass
class FocusModel:
    """Elastic-line width as a function of SM3 pitch (native units) and Sample X (mm)."""

    best_pitch: float = DEFAULT_BEST_PITCH
    best_sample_x: float = DEFAULT_BEST_SAMPLE_X
    sigma_min_px: float = 1.5
    # Line-sigma growth per pitch unit below / above the best pitch (lopsided, like 004202).
    k_pitch_minus_px: float = 0.376
    k_pitch_plus_px: float = 0.553
    # Line-sigma growth per mm away from the best Sample X (symmetric).
    k_sample_x_px_per_mm: float = 59.9
    # Controller's guess, not measured: the best Sample X moves linearly with pitch,
    # by this many mm per pitch unit away from best_pitch.
    sample_x_per_pitch_mm: float = 0.005
    line_x_frac: float = 0.70
    angle_deg: float = 1.5
    peak_counts: float = 2000.0
    background: float = 100.0

    def best_sample_x_at(self, pitch: float) -> float:
        """Best Sample X (mm) at this pitch, including the pitch coupling."""
        return self.best_sample_x + self.sample_x_per_pitch_mm * (pitch - self.best_pitch)

    def sigma_px(self, pitch: float, sample_x: float | None = None) -> float:
        """Line sigma in px; ``sample_x=None`` means Sample X is at its best for ``pitch``."""
        dp = pitch - self.best_pitch
        kp = self.k_pitch_plus_px if dp > 0 else self.k_pitch_minus_px
        dx = 0.0 if sample_x is None else sample_x - self.best_sample_x_at(pitch)
        return float(np.sqrt(self.sigma_min_px ** 2 + (kp * dp) ** 2
                             + (self.k_sample_x_px_per_mm * dx) ** 2))

    @classmethod
    def from_seed(cls, seed: int | None) -> "FocusModel":
        if seed is None:
            return cls()
        rng = np.random.default_rng(seed)
        return cls(best_pitch=float(rng.uniform(*BEST_PITCH_RANGE)),
                   best_sample_x=float(rng.uniform(*BEST_SAMPLE_X_RANGE)))


def render_frame(model: FocusModel, pitch: float, exposure_s: float,
                 shape: tuple[int, int] = (2048, 2048), rng=None,
                 sample_x: float | None = None) -> np.ndarray:
    """Render a uint16 frame with a near-vertical Gaussian line."""
    if rng is None:
        rng = np.random.default_rng()
    h, w = shape
    sigma = model.sigma_px(pitch, sample_x)
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
