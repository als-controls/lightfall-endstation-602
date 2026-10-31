import base64
from unittest.mock import MagicMock

import numpy as np
import pytest

from lightfall_endstation_602 import contract as c
from lightfall_endstation_602.client import RixsServiceClient
from lightfall_endstation_602.panel import QerlinFocusPanel, QerlinFocusPanelPlugin


def _frame(pos, fwhm, valid=True, reason=None, preview=None, centroid=None, angle=None):
    return {"run_uid": "u", "seq_num": 1, "position": pos, "fwhm_px": fwhm,
            "fwhm_mev": None, "resolving_power": None, "score_valid": valid,
            "failure_reason": reason, "centroid": centroid, "angle_deg": angle,
            "preview": preview}


@pytest.fixture
def panel(qtbot, fake_ipc):
    client = RixsServiceClient(ipc=fake_ipc)
    binder = MagicMock()
    binder.enabled = False
    p = QerlinFocusPanel(client=client, binder=binder)
    qtbot.addWidget(p)
    p.test_ipc = fake_ipc
    p.test_binder = binder
    return p


def test_metadata():
    md = QerlinFocusPanel.panel_metadata
    assert md.id == "lightfall_endstation_602.panels.qerlin_focus"
    assert md.name == "QERLIN Focus"
    assert md.category == "Analysis"
    assert md.singleton is True


def test_toggle_checked_and_binder_enabled_on_construction(panel):
    assert panel._enable_toggle.isChecked() is True
    panel.test_binder.enable.assert_called_once()
    panel._enable_toggle.setChecked(False)
    panel.test_binder.disable.assert_called_once()


def test_construction_retries_until_run_engine_exists(qtbot, fake_ipc):
    binder = MagicMock()
    binder.enabled = False
    binder.enable.side_effect = [RuntimeError("no RE"), RuntimeError("no RE"), None]
    p = QerlinFocusPanel(client=RixsServiceClient(ipc=fake_ipc), binder=binder)
    qtbot.addWidget(p)
    assert p._enable_toggle.isChecked() is True  # stays armed
    assert "waiting for RunEngine" in p._status_label.text()
    assert p._retry_timer.isActive()
    p._retry_enable()
    assert p._retry_timer.isActive()
    p._retry_enable()
    assert binder.enable.call_count == 3
    assert not p._retry_timer.isActive()
    assert p._enable_toggle.isChecked() is True


def test_unchecking_stops_retry(qtbot, fake_ipc):
    binder = MagicMock()
    binder.enabled = False
    binder.enable.side_effect = RuntimeError("no RE")
    p = QerlinFocusPanel(client=RixsServiceClient(ipc=fake_ipc), binder=binder)
    qtbot.addWidget(p)
    p._enable_toggle.setChecked(False)
    assert not p._retry_timer.isActive()


def test_toggle_rollback_on_error(panel):
    panel._enable_toggle.setChecked(False)
    panel.test_binder.enable.side_effect = RuntimeError("no ipc")
    panel._enable_toggle.setChecked(True)
    assert panel._enable_toggle.isChecked() is False
    assert "no ipc" in panel._status_label.text()


def test_frame_events_add_points(panel):
    for pos, fw in [(-1.0, 9.0), (0.0, 4.0), (1.0, 8.0)]:
        panel.test_ipc.emit(c.SUBJ_FRAME, _frame(pos, fw))
    panel.test_ipc.emit(c.SUBJ_FRAME, _frame(0.5, None, valid=False, reason="no peak found"))
    xs, ys = panel._scatter.getData()
    assert list(xs) == [-1.0, 0.0, 1.0]
    assert list(ys) == [9.0, 4.0, 8.0]
    assert "no fit: no peak found" in panel._status_label.text()


def test_new_run_clears_points(panel):
    panel.test_ipc.emit(c.SUBJ_FRAME, _frame(0.0, 4.0))
    f = _frame(1.0, 5.0)
    f["run_uid"] = "other"
    panel.test_ipc.emit(c.SUBJ_FRAME, f)
    xs, _ = panel._scatter.getData()
    assert list(xs) == [1.0]


def test_complete_draws_fit_line(panel):
    panel.test_ipc.emit(c.SUBJ_FRAME, _frame(0.0, 4.0))
    panel.test_ipc.emit(c.SUBJ_COMPLETE, {
        "run_uid": "u", "motor_field": "sm3_pitch", "points": [],
        "fit": {"ok": True, "optimal_position": 0.25, "coeffs": [2.0, -1.0, 4.0],
                "reason": None},
        "best_measured": {"position": 0.0, "fwhm_px": 4.0}})
    assert panel._opt_line.isVisible()
    assert panel._opt_line.value() == pytest.approx(0.25)
    x, y = panel._fit_curve.getData()
    assert len(x) > 10
    i = int(np.argmin(np.abs(x - 0.25)))
    assert y[i] == pytest.approx(2.0 * x[i] ** 2 - x[i] + 4.0)
    assert "0.25" in panel._status_label.text()


def test_complete_without_fit_hides_line(panel):
    panel.test_ipc.emit(c.SUBJ_COMPLETE, {
        "run_uid": "u", "motor_field": "sm3_pitch", "points": [],
        "fit": {"ok": False, "optimal_position": None, "coeffs": None,
                "reason": "too few points"},
        "best_measured": None})
    assert not panel._opt_line.isVisible()
    assert "too few points" in panel._status_label.text()


def test_preview_decodes(panel):
    img = (np.arange(12, dtype=np.uint8) * 20).reshape(3, 4)
    preview = {"b64": base64.b64encode(img.tobytes()).decode("ascii"),
               "shape": [3, 4], "scale": 4}
    panel.test_ipc.emit(c.SUBJ_FRAME, _frame(0.0, 4.0, preview=preview,
                                              centroid=[8.0, 6.0], angle=0.0))
    shown = panel._image.image
    assert shown is not None
    # image is displayed transposed (pyqtgraph x = column)
    assert np.array_equal(np.asarray(shown).T, img)
    lx, ly = panel._line_curve.getData()
    assert len(lx) == 2
    # centroid (8,6) full-res -> (2,1.5) preview px; angle 0 -> horizontal line
    assert ly[0] == pytest.approx(1.5) and ly[1] == pytest.approx(1.5)
    assert min(lx) < 2.0 < max(lx)


def test_preview_bad_payload_is_ignored(panel):
    panel.test_ipc.emit(c.SUBJ_FRAME, _frame(0.0, 4.0, preview={"b64": "!!", "shape": [3, 4],
                                                                 "scale": 1}))
    xs, _ = panel._scatter.getData()
    assert list(xs) == [0.0]


def test_status_and_error(panel):
    panel.test_ipc.emit(c.SUBJ_STATUS, {"run_uid": "u", "state": "scoring",
                                        "frames_done": 2, "total": 5})
    assert "scoring" in panel._status_label.text() and "2/5" in panel._status_label.text()
    panel.test_ipc.emit(c.SUBJ_ERROR, {"run_uid": "u", "error": "boom"})
    assert "boom" in panel._status_label.text()


def test_close_keeps_binder_enabled(panel):
    """The panel is only a view: closing it must not unbind the agent's scans."""
    panel.test_binder.disable.reset_mock()
    panel._on_closing()
    panel.test_binder.disable.assert_not_called()
    assert not panel._retry_timer.isActive()


def test_plugin_panel_class():
    plugin = QerlinFocusPanelPlugin()
    assert plugin.name == "qerlin_focus"
    assert plugin.get_panel_class() is QerlinFocusPanel
