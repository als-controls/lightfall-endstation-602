import base64
from unittest.mock import MagicMock

import numpy as np
import pyqtgraph as pg
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
    fake_ipc.replies[c.SUBJ_DISCOVER] = {"app_name": "rixs-live"}  # healthy service
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
    # row-major ImageItem: data passed through as-is (x = column, y = row)
    assert np.array_equal(np.asarray(shown), img)
    lx, ly = panel._line_curve.getData()
    assert len(lx) == 2
    # centroid (8,6) full-res -> (2,1.5) preview px; angle 0 -> horizontal line
    assert ly[0] == pytest.approx(1.5) and ly[1] == pytest.approx(1.5)
    assert min(lx) < 2.0 < max(lx)


@pytest.fixture(params=["col-major", "row-major"])
def global_axis_order(request):
    # Lightfall sets this globally to row-major; the panel must not depend on it.
    old = pg.getConfigOption("imageAxisOrder")
    pg.setConfigOptions(imageAxisOrder=request.param)
    yield request.param
    pg.setConfigOptions(imageAxisOrder=old)


@pytest.mark.parametrize("angle_deg", [88.5, 60.0, -30.0])
def test_preview_line_overlays_known_angle_streak(qtbot, fake_ipc, global_axis_order, angle_deg):
    """Line drawn from (centroid, angle_deg) must sit on the streak as displayed."""
    client = RixsServiceClient(ipc=fake_ipc)
    binder = MagicMock()
    binder.enabled = False
    p = QerlinFocusPanel(client=client, binder=binder)
    qtbot.addWidget(p)

    scale, h, w = 4, 64, 96  # preview geometry; full-res frame is 256 x 384
    cx_full, cy_full = 200.0, 120.0
    a = np.radians(angle_deg)
    rows = np.arange(h, dtype=float)[:, None] + 0.5  # preview pixel centres
    cols = np.arange(w, dtype=float)[None, :] + 0.5
    # perpendicular distance from the line through the centroid, direction (cos a, sin a)
    d = -(cols - cx_full / scale) * np.sin(a) + (rows - cy_full / scale) * np.cos(a)
    img = np.where(np.abs(d) < 1.0, 255, 0).astype(np.uint8)
    preview = {"b64": base64.b64encode(img.tobytes()).decode("ascii"),
               "shape": [h, w], "scale": scale}
    fake_ipc.emit(c.SUBJ_FRAME, _frame(0.0, 4.0, preview=preview,
                                       centroid=[cx_full, cy_full], angle=angle_deg))

    item = p._image
    item.render()
    qimg = item.qimage
    assert (qimg.width(), qimg.height()) == (w, h)  # displayed as w cols x h rows
    (x0, x1), (y0, y1) = p._line_curve.getData()
    hits = total = 0
    for f in np.linspace(0.0, 1.0, 201):
        x, y = x0 + f * (x1 - x0), y0 + f * (y1 - y0)
        if 0 <= x < w and 0 <= y < h:
            total += 1
            hits += qimg.pixelColor(int(x), int(y)).value() > 128
    assert total > 20
    assert hits / total > 0.9, f"line misses streak ({hits}/{total})"


def test_preview_bad_payload_is_ignored(panel):
    panel.test_ipc.emit(c.SUBJ_FRAME, _frame(0.0, 4.0, preview={"b64": "!!", "shape": [3, 4],
                                                                 "scale": 1}))
    xs, _ = panel._scatter.getData()
    assert list(xs) == [0.0]


def test_status_and_error(panel):
    panel.test_ipc.emit(c.SUBJ_STATUS, {"run_uid": "u", "state": "scoring",
                                        "frames_done": 2, "total": 5})
    assert panel._state_label.text() == "service: scoring 2/5"
    panel.test_ipc.emit(c.SUBJ_STATUS, {"run_uid": "u", "state": "binding",
                                        "frames_done": 0, "total": None})
    assert panel._state_label.text() == "service: binding 0/?"
    panel.test_ipc.emit(c.SUBJ_ERROR, {"run_uid": "u", "error": "boom"})
    assert "boom" in panel._status_label.text()


def test_status_does_not_overwrite_frame_or_fit_text(panel):
    panel.test_ipc.emit(c.SUBJ_FRAME, _frame(0.5, None, valid=False, reason="no peak found"))
    panel.test_ipc.emit(c.SUBJ_STATUS, {"run_uid": "u", "state": "scoring",
                                        "frames_done": 1, "total": 7})
    assert "no fit: no peak found" in panel._status_label.text()
    assert "scoring 1/7" in panel._state_label.text()
    panel.test_ipc.emit(c.SUBJ_COMPLETE, {
        "run_uid": "u", "motor_field": "sm3_pitch", "points": [],
        "fit": {"ok": False, "optimal_position": None, "coeffs": None,
                "reason": "fewer than 3 valid points"},
        "best_measured": None})
    panel.test_ipc.emit(c.SUBJ_STATUS, {"run_uid": "u", "state": "idle",
                                        "frames_done": 1, "total": 7})
    assert "fewer than 3 valid points" in panel._status_label.text()
    assert panel._state_label.text() == "service: idle"


def test_axis_label_follows_payload_motor_and_units(panel):
    axis = panel._plot.getPlotItem().getAxis("bottom")
    assert axis.labelText == "position"  # nothing scanned yet
    f = _frame(-0.82, 5.0)
    f.update(motor_field="sample_x", motor_units="mm")
    panel.test_ipc.emit(c.SUBJ_FRAME, f)
    assert axis.labelText == "sample_x (mm)"
    assert "mmm" not in axis.labelString()  # no SI auto-prefix
    assert "sample_x -0.82" in panel._status_label.text()
    panel.test_ipc.emit(c.SUBJ_COMPLETE, {
        "run_uid": "u2", "motor_field": "sm3_pitch", "motor_units": "", "points": [],
        "fit": {"ok": True, "optimal_position": 104.0, "coeffs": [1.0, -208.0, 10821.0],
                "reason": None},
        "best_measured": None})
    assert axis.labelText == "sm3_pitch"
    assert "optimal sm3_pitch 104" in panel._status_label.text()


def test_axis_label_kept_for_payload_without_motor(panel):
    f = _frame(0.0, 4.0)
    f.update(motor_field="sample_x", motor_units="mm")
    panel.test_ipc.emit(c.SUBJ_FRAME, f)
    panel.test_ipc.emit(c.SUBJ_FRAME, _frame(1.0, 5.0))  # older service: no motor keys
    assert panel._plot.getPlotItem().getAxis("bottom").labelText == "sample_x (mm)"


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


def test_toggle_follows_binder_changes_without_retriggering(qtbot, fake_ipc):
    from lightfall_endstation_602.binder import RixsRunBinder
    re = MagicMock()
    binder = RixsRunBinder(RixsServiceClient(ipc=fake_ipc), run_engine_getter=lambda: re)
    p = QerlinFocusPanel(client=RixsServiceClient(ipc=fake_ipc), binder=binder)
    qtbot.addWidget(p)
    assert binder.enabled and p._enable_toggle.isChecked()
    binder.disable()  # e.g. disabled elsewhere: the toggle must follow
    qtbot.waitUntil(lambda: not p._enable_toggle.isChecked(), timeout=2000)
    assert re.subscribe.call_count == 1  # toggling the button did not re-enable
    binder.enable()  # the agent tools re-enable the binder
    qtbot.waitUntil(lambda: p._enable_toggle.isChecked(), timeout=2000)
    assert binder.enabled and re.subscribe.call_count == 2 and re.unsubscribe.call_count == 1


def test_toggle_update_from_worker_thread(qtbot, fake_ipc):
    import threading
    from lightfall_endstation_602.binder import RixsRunBinder
    binder = RixsRunBinder(RixsServiceClient(ipc=fake_ipc), run_engine_getter=MagicMock)
    p = QerlinFocusPanel(client=RixsServiceClient(ipc=fake_ipc), binder=binder)
    qtbot.addWidget(p)
    t = threading.Thread(target=binder.disable)
    t.start()
    t.join()
    qtbot.waitUntil(lambda: not p._enable_toggle.isChecked(), timeout=2000)


# ------------------------------------------------------------------ health


def test_health_down_then_up(qtbot, fake_ipc):
    client = RixsServiceClient(ipc=fake_ipc)  # no discover reply: service down
    binder = MagicMock()
    binder.enabled = True
    p = QerlinFocusPanel(client=client, binder=binder)
    qtbot.addWidget(p)
    qtbot.waitUntil(lambda: p._state_label.text() == "service: not responding", timeout=3000)
    assert client.service_up is False
    assert "rixs-live not responding" in p._status_label.text()
    fake_ipc.replies[c.SUBJ_DISCOVER] = {"app_name": "rixs-live"}
    client.check_health_async()
    qtbot.waitUntil(lambda: client.service_up is True, timeout=3000)
    assert p._state_label.text() == "service: idle"
    assert p._status_label.text() == "rixs-live responding again"


def test_bind_while_down_says_not_analysed(qtbot, panel):
    client = panel._client
    qtbot.waitUntil(lambda: client.service_up is True, timeout=3000)
    client.record_health(None)
    assert panel._state_label.text() == "service: not responding"
    client.bind_run("abcdef1234-run", tiled_url="http://t")
    qtbot.waitUntil(lambda: "not analysed" in panel._status_label.text(), timeout=2000)
    assert panel._status_label.text() == (
        "run abcdef12 not analysed: rixs-live not responding")


def test_bind_from_run_engine_thread_reaches_panel(qtbot, fake_ipc):
    import threading
    client = RixsServiceClient(ipc=fake_ipc)  # down
    binder = MagicMock()
    binder.enabled = True
    p = QerlinFocusPanel(client=client, binder=binder)
    qtbot.addWidget(p)
    qtbot.waitUntil(lambda: client.service_up is False, timeout=3000)
    t = threading.Thread(target=client.bind_run, args=("worker-run-uid",))
    t.start()
    t.join()
    qtbot.waitUntil(lambda: "run worker-r not analysed" in p._status_label.text(),
                    timeout=2000)


def test_complete_clears_pending_bind(qtbot, panel):
    panel._client.bind_run("u")
    assert panel._pending_uid == "u"
    panel.test_ipc.emit(c.SUBJ_COMPLETE, {"run_uid": "u", "points": [], "fit": {}})
    assert panel._pending_uid is None
    panel._client.record_health(None)
    assert "rixs-live not responding (start it" in panel._status_label.text()


def test_health_check_does_not_block_gui_thread(qtbot, fake_ipc):
    import time

    class SlowIPC(type(fake_ipc)):
        def request(self, subject, data, timeout_ms=1000):
            time.sleep(1.0)
            raise TimeoutError()

    client = RixsServiceClient(ipc=SlowIPC())
    binder = MagicMock()
    binder.enabled = True
    t0 = time.monotonic()
    p = QerlinFocusPanel(client=client, binder=binder)
    qtbot.addWidget(p)
    assert time.monotonic() - t0 < 0.5
    qtbot.waitUntil(lambda: p._state_label.text() == "service: not responding",
                    timeout=4000)


def test_tiled_connect_error_shown_in_status_line(panel):
    err = ("cannot connect to Tiled at http://bcgtiled.dhcp.lbl.gov:8000 using no proxy: "
           "ConnectTimeout: timed out. Check the proxy")
    panel.test_ipc.emit(c.SUBJ_ERROR, {"run_uid": "u", "error": err})
    assert panel._status_label.text() == f"error: {err}"
