"""QERLIN Focus panel: live focus curve and zeroth-order preview from rixs-live."""
from __future__ import annotations

import base64
import binascii
import math
from typing import ClassVar

import numpy as np
from loguru import logger
from qtpy.QtCore import QTimer
from qtpy.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from lightfall.plugins.panel_plugin import PanelPlugin
from lightfall.ui.panels.base import BasePanel, PanelMetadata
from lightfall.visualization import pg


def _num(v) -> float | None:
    """JSON value -> finite float, else None."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def decode_preview(preview) -> tuple[np.ndarray, int] | None:
    """Decode a ``{b64, shape:[h, w], scale}`` preview; None if malformed."""
    if not isinstance(preview, dict):
        return None
    try:
        h, w = (int(n) for n in preview["shape"])
        raw = base64.b64decode(preview["b64"], validate=True)
        img = np.frombuffer(raw, dtype=np.uint8).reshape(h, w)
        scale = max(1, int(preview.get("scale") or 1))
    except (KeyError, TypeError, ValueError, binascii.Error) as ex:
        logger.debug(f"bad rixs preview: {ex!r}")
        return None
    return img, scale


class QerlinFocusPanel(BasePanel):
    panel_metadata: ClassVar[PanelMetadata] = PanelMetadata(
        id="lightfall_endstation_602.panels.qerlin_focus",
        name="QERLIN Focus",
        description="Live SM3 focus curve and zeroth-order preview from the rixs-live service",
        icon="mdi6.image-filter-center-focus",
        category="Analysis",
        singleton=True,
        closable=True,
        keywords=["qerlin", "rixs", "focus", "zeroth order", "6.0.2"],
        default_area="bottom",
        sidebar_group="top",
    )

    RETRY_MS = 1000

    def __init__(self, parent: QWidget | None = None, client=None, binder=None) -> None:
        # attrs before super().__init__: BasePanel.__init__ calls _setup_ui()
        if client is None or binder is None:
            from lightfall_endstation_602 import runtime
            client = client or runtime.get_client()
            binder = binder or runtime.get_binder()
        self._client = client
        self._binder = binder
        self._run_uid: str | None = None
        self._xs: list[float] = []
        self._ys: list[float] = []
        super().__init__(parent)
        self._client.frameScored.connect(self._on_frame)
        self._client.scanComplete.connect(self._on_complete)
        self._client.statusChanged.connect(self._on_status)
        self._client.errorReceived.connect(self._on_error)
        # Default on, so the demo needs no clicks. The RunEngine is created when
        # the engine thread starts, which can be after this panel is built, so a
        # failed first enable keeps the toggle armed and retries once a second.
        self._retry_timer = QTimer(self)
        self._retry_timer.setInterval(self.RETRY_MS)
        self._retry_timer.timeout.connect(self._retry_enable)
        self._enable_toggle.blockSignals(True)
        self._enable_toggle.setChecked(True)
        self._enable_toggle.blockSignals(False)
        self._retry_enable()

    # -- UI -----------------------------------------------------------------
    def _setup_ui(self) -> None:
        self._enable_toggle = self.add_title_bar_button(
            "mdi6.play-pause", "Enable rixs-live binding", checkable=True)
        self._enable_toggle.toggled.connect(self._on_enable_toggled)

        content = QWidget(self)
        outer = QVBoxLayout(content)
        row = QHBoxLayout()

        self._plot = pg.PlotWidget()
        self._plot.setLabel("bottom", "SM3 pitch", units="mrad")
        self._plot.setLabel("left", "FWHM", units="px")
        self._scatter = pg.ScatterPlotItem(size=8)
        self._fit_curve = pg.PlotDataItem([], [], pen=pg.series_pen(1))
        self._opt_line = pg.InfiniteLine(angle=90, movable=False)
        self._opt_line.setVisible(False)
        self._plot.addItem(self._scatter)
        self._plot.addItem(self._fit_curve)
        self._plot.addItem(self._opt_line)

        self._image_view = pg.PlotWidget()
        self._image_view.setAspectLocked(True)
        self._image_view.invertY(True)  # row 0 at the top, like the detector
        self._image_view.hideAxis("left")
        self._image_view.hideAxis("bottom")
        self._image = pg.ImageItem()
        self._line_curve = pg.PlotDataItem([], [], pen=pg.series_pen(2))
        self._image_view.addItem(self._image)
        self._image_view.addItem(self._line_curve)

        row.addWidget(self._plot, 1)
        row.addWidget(self._image_view, 1)
        self._status_label = QLabel("idle")
        outer.addLayout(row)
        outer.addWidget(self._status_label)
        self._layout.addWidget(content)

    def _retry_enable(self) -> None:
        """Enable the binder while the toggle is armed; retry until it works."""
        if not self._enable_toggle.isChecked() or self._binder.enabled:
            self._retry_timer.stop()
            return
        try:
            self._binder.enable()
        except Exception as ex:
            if not self._retry_timer.isActive():
                logger.debug(f"rixs binder not enabled yet, retrying: {ex!r}")
                self._status_label.setText(f"waiting for RunEngine ({ex})")
                self._retry_timer.start()
            return
        self._retry_timer.stop()
        self._status_label.setText("bound: waiting for a qerlin_focus_scan run")

    def _on_enable_toggled(self, checked: bool) -> None:
        self._retry_timer.stop()
        try:
            if checked:
                self._binder.enable()
            else:
                self._binder.disable()
        except Exception as ex:
            logger.exception(ex)
            self._status_label.setText(f"binder: {ex}")
            self._enable_toggle.blockSignals(True)
            self._enable_toggle.setChecked(bool(self._binder.enabled))
            self._enable_toggle.blockSignals(False)

    # -- events ---------------------------------------------------------------
    def _start_run(self, run_uid) -> None:
        if run_uid == self._run_uid:
            return
        self._run_uid = run_uid
        self._xs, self._ys = [], []
        self._scatter.setData([], [])
        self._fit_curve.setData([], [])
        self._opt_line.setVisible(False)

    def _on_frame(self, data: dict) -> None:
        self._start_run(data.get("run_uid"))
        pos = _num(data.get("position"))
        fwhm = _num(data.get("fwhm_px"))
        if data.get("score_valid") and pos is not None and fwhm is not None:
            self._xs.append(pos)
            self._ys.append(fwhm)
            self._scatter.setData(self._xs, self._ys)
            text = f"pitch {pos:.4g}: FWHM {fwhm:.3g} px"
            mev = _num(data.get("fwhm_mev"))
            if mev is not None:
                text += f" ({mev:.3g} meV)"
            rp = _num(data.get("resolving_power"))
            if rp is not None:
                text += f", R={rp:.0f}"
            self._status_label.setText(text)
        else:
            self._status_label.setText(f"no fit: {data.get('failure_reason') or 'unknown'}")
        self._show_preview(data)

    def _show_preview(self, data: dict) -> None:
        decoded = decode_preview(data.get("preview"))
        if decoded is None:
            return
        img, scale = decoded
        # ImageItem is column-major (x = column); transpose the row-major frame.
        self._image.setImage(img.T, autoLevels=False, levels=(0, 255))
        centroid = data.get("centroid")
        angle = _num(data.get("angle_deg"))
        if not centroid or angle is None:
            self._line_curve.setData([], [])
            return
        cx, cy = _num(centroid[0]), _num(centroid[1])
        if cx is None or cy is None:
            self._line_curve.setData([], [])
            return
        cx, cy = cx / scale, cy / scale
        half = 0.5 * max(img.shape)
        dx, dy = half * math.cos(math.radians(angle)), half * math.sin(math.radians(angle))
        self._line_curve.setData([cx - dx, cx + dx], [cy - dy, cy + dy])

    def _on_complete(self, data: dict) -> None:
        self._start_run(data.get("run_uid"))
        points = [p for p in data.get("points") or []
                  if p.get("score_valid") and _num(p.get("position")) is not None
                  and _num(p.get("fwhm_px")) is not None]
        if points:
            self._xs = [_num(p["position"]) for p in points]
            self._ys = [_num(p["fwhm_px"]) for p in points]
            self._scatter.setData(self._xs, self._ys)
        fit = data.get("fit") or {}
        opt = _num(fit.get("optimal_position"))
        coeffs = fit.get("coeffs")
        if coeffs and len(coeffs) == 3:
            xs = list(self._xs) + ([opt] if opt is not None else [])
            lo, hi = (min(xs), max(xs)) if xs else (-1.0, 1.0)
            if hi - lo < 1e-12:
                lo, hi = lo - 1.0, hi + 1.0
            pad = 0.05 * (hi - lo)
            x = np.linspace(lo - pad, hi + pad, 200)
            self._fit_curve.setData(x, np.polyval([float(c) for c in coeffs], x))
        else:
            self._fit_curve.setData([], [])
        if fit.get("ok") and opt is not None:
            self._opt_line.setValue(opt)
            self._opt_line.setVisible(True)
            self._status_label.setText(f"scan complete: optimal pitch {opt:.4g}")
        else:
            self._opt_line.setVisible(False)
            self._status_label.setText(
                f"scan complete, no fit: {fit.get('reason') or 'unknown'}")

    def _on_status(self, data: dict) -> None:
        self._status_label.setText(
            f"{data.get('state', '?')} - {data.get('frames_done', 0)}/{data.get('total', 0)}")

    def _on_error(self, data: dict) -> None:
        self._status_label.setText(f"error: {data.get('error', '')}")

    def _on_closing(self) -> None:
        # The panel is only a view of the shared binder: closing it must not unbind
        # the agent's scans. Only the title-bar toggle disables the binder.
        self._retry_timer.stop()
        super()._on_closing()


class QerlinFocusPanelPlugin(PanelPlugin):
    @property
    def name(self) -> str:
        return "qerlin_focus"

    @property
    def description(self) -> str:
        return "Live QERLIN focus-curve panel for the rixs-live analysis service"

    def get_panel_class(self):
        return QerlinFocusPanel
