from lightfall_endstation_602 import contract as c
from lightfall_endstation_602.client import RixsServiceClient


def test_bind_payload(fake_ipc):
    cl = RixsServiceClient(ipc=fake_ipc)
    cl.bind_run("u1", tiled_url="http://t", lightfall_prefix="als.602")
    assert fake_ipc.published == [(c.SUBJ_BIND, {
        "run_uid": "u1", "tiled_url": "http://t", "tiled_proxy": None,
        "lightfall_prefix": "als.602", "contract_version": c.CONTRACT_VERSION})]


def test_run_stop(fake_ipc):
    RixsServiceClient(ipc=fake_ipc).run_stop("u1")
    assert fake_ipc.published == [(c.SUBJ_STOP, {"run_uid": "u1"})]


def test_signals_emit(fake_ipc, qtbot):
    cl = RixsServiceClient(ipc=fake_ipc)
    got = {}
    cl.frameScored.connect(lambda d: got.setdefault("frame", d))
    cl.scanComplete.connect(lambda d: got.setdefault("done", d))
    cl.statusChanged.connect(lambda d: got.setdefault("status", d))
    cl.errorReceived.connect(lambda d: got.setdefault("err", d))
    assert cl.last_complete is None
    fake_ipc.emit(c.SUBJ_FRAME, {"seq": 1})
    fake_ipc.emit(c.SUBJ_COMPLETE, {"run_uid": "u", "best": 3})
    fake_ipc.emit(c.SUBJ_STATUS, {"state": "x"})
    fake_ipc.emit(c.SUBJ_ERROR, {"error": "e"})
    assert got["frame"]["seq"] == 1
    assert got["done"]["best"] == 3
    assert cl.last_complete == {"run_uid": "u", "best": 3}
    assert got["status"]["state"] == "x"
    assert got["err"]["error"] == "e"


def test_discover(fake_ipc):
    fake_ipc.replies[c.SUBJ_DISCOVER] = {"app_name": "rixs-live"}
    assert RixsServiceClient(ipc=fake_ipc).discover()["app_name"] == "rixs-live"


def test_discover_exception_returns_none(fake_ipc):
    fake_ipc.request_exc = RuntimeError("x")
    assert RixsServiceClient(ipc=fake_ipc).discover() is None


def test_get_result_ok(fake_ipc):
    fake_ipc.replies[c.SUBJ_RESULT] = {"run_uid": "u", "best": 2}
    r = RixsServiceClient(ipc=fake_ipc).get_result("u")
    assert r == {"run_uid": "u", "best": 2}
    assert fake_ipc.requests == [(c.SUBJ_RESULT, {"run_uid": "u"})]


def test_get_result_pending_passthrough(fake_ipc):
    pending = {"pending": True, "run_uid": "u", "frames_done": 3, "total": 7}
    fake_ipc.replies[c.SUBJ_RESULT] = pending
    cl = RixsServiceClient(ipc=fake_ipc)
    assert cl.last_bound_uid is None
    r = cl.get_result("u")
    assert r == pending and r.get("pending") is True


def test_get_result_timeout_returns_none(fake_ipc):
    fake_ipc.request_exc = TimeoutError()
    assert RixsServiceClient(ipc=fake_ipc).get_result("u") is None


def test_get_result_error_reply_returns_none(fake_ipc):
    fake_ipc.replies[c.SUBJ_RESULT] = {"error": "unknown run"}
    assert RixsServiceClient(ipc=fake_ipc).get_result("u") is None


def test_no_ipc_is_safe():
    cl = RixsServiceClient(ipc=None)
    cl._ipc = None
    cl.bind_run("u")
    cl.run_stop("u")
    assert cl.discover() is None
    assert cl.get_result("u") is None


def test_record_health_emits_only_on_change(fake_ipc, qtbot):
    cl = RixsServiceClient(ipc=fake_ipc)
    seen = []
    cl.healthChanged.connect(seen.append)
    assert cl.service_up is None
    cl.record_health({"app_name": "rixs-live"})
    cl.record_health({"app_name": "rixs-live"})
    cl.record_health(None)
    assert seen == [True, False]
    assert cl.service_up is False and cl.service_info is None
    assert cl.health_checked_at is not None


def test_check_health_async_runs_discover_off_gui_thread(fake_ipc, qtbot):
    import threading
    threads = []

    class IPC(type(fake_ipc)):
        def request(self, subject, data, timeout_ms=1000):
            threads.append(threading.get_ident())
            return {"app_name": "rixs-live"}

    cl = RixsServiceClient(ipc=IPC())
    cl.start_health_monitor(interval_ms=60_000)
    cl.start_health_monitor()  # idempotent
    qtbot.waitUntil(lambda: cl.service_up is True, timeout=3000)
    assert threads and threads[0] != threading.get_ident()
    assert cl.service_info == {"app_name": "rixs-live"}
    cl.stop_health_monitor()


def test_bind_emits_bind_sent(fake_ipc, qtbot):
    cl = RixsServiceClient(ipc=fake_ipc)
    seen = []
    cl.bindSent.connect(seen.append)
    cl.bind_run("u1")
    assert seen == ["u1"]


# -- per-frame score cache -----------------------------------------------------

def test_frame_scores_cached_without_preview(fake_ipc, qtbot):
    cl = RixsServiceClient(ipc=fake_ipc)
    assert cl.get_score("u", 1) is None
    fake_ipc.emit(c.SUBJ_FRAME, {"run_uid": "u", "seq_num": 1, "fwhm_px": 6.0,
                                 "score_valid": True, "positions": {"a": 1.0},
                                 "preview": {"b64": "..."}, "centroid": [1, 2]})
    got = cl.get_score("u", 1)
    assert got == {"run_uid": "u", "seq_num": 1, "fwhm_px": 6.0, "score_valid": True,
                   "positions": {"a": 1.0}}
    got["fwhm_px"] = 0  # a copy: callers cannot corrupt the cache
    assert cl.get_score("u", 1)["fwhm_px"] == 6.0
    assert cl.get_score("u", 2) is None and cl.get_score("other", 1) is None


def test_score_cache_ignores_malformed_and_is_bounded(fake_ipc, monkeypatch):
    cl = RixsServiceClient(ipc=fake_ipc)
    for bad in (None, {}, {"run_uid": "u"}, {"run_uid": "", "seq_num": 1},
                {"run_uid": "u", "seq_num": True}, {"run_uid": "u", "seq_num": "x"}):
        cl.record_score(bad)
    assert not cl._scores
    monkeypatch.setattr(RixsServiceClient, "SCORE_CACHE_MAX", 3)
    for i in range(1, 6):
        cl.record_score({"run_uid": "u", "seq_num": i})
    assert [k[1] for k in cl._scores] == [3, 4, 5]
    cl.record_score({"run_uid": "v", "seq_num": 1})
    cl.clear_scores("u")
    assert list(cl._scores) == [("v", 1)]
    cl.clear_scores()
    assert not cl._scores


def test_wait_score_timeout_and_wakeup(fake_ipc):
    import threading
    import time

    cl = RixsServiceClient(ipc=fake_ipc)
    t0 = time.monotonic()
    assert cl.wait_score("u", 1, 0.05) is None
    assert time.monotonic() - t0 >= 0.04
    timer = threading.Timer(0.05, cl.record_score,
                            args=({"run_uid": "u", "seq_num": 1, "fwhm_px": 5.0},))
    timer.start()
    got = cl.wait_score("u", 1, 5.0)
    timer.join()
    assert got["fwhm_px"] == 5.0
    assert cl.wait_score("u", 1, 0.0)["fwhm_px"] == 5.0  # already there: no wait


def test_score_cache_thread_safety(fake_ipc):
    """Concurrent writers (as from IPC) and readers (as from the RunEngine) agree."""
    import threading

    cl = RixsServiceClient(ipc=fake_ipc)
    n_writers, per = 4, 300
    errors = []

    def writer(w):
        for i in range(per):
            cl.record_score({"run_uid": f"r{w}", "seq_num": i + 1, "fwhm_px": float(i)})

    def reader(w):
        try:
            for i in range(per):
                got = cl.wait_score(f"r{w}", i + 1, 10.0)
                assert got is not None and got["fwhm_px"] == float(i)
                for _ in range(3):
                    cl.get_score(f"r{w}", 1)
        except Exception as ex:  # noqa: BLE001
            errors.append(ex)

    threads = [threading.Thread(target=reader, args=(w,)) for w in range(n_writers)]
    threads += [threading.Thread(target=writer, args=(w,)) for w in range(n_writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors and not any(t.is_alive() for t in threads)
    assert len(cl._scores) == n_writers * per
