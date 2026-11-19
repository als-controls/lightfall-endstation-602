import pytest

from lightfall_endstation_602 import contract as c
from lightfall_endstation_602.client import RixsServiceClient


def test_bind_is_a_run_bind_rpc_without_any_key(fake_svc):
    cl = RixsServiceClient(service=fake_svc)
    cl.bind_run("u1", tiled_url="http://t", tiled_proxy="socks5://p:1080")
    assert fake_svc.calls == [(c.RPC_BIND, {
        "run_uid": "u1", "tiled_url": "http://t", "tiled_proxy": "socks5://p:1080",
        "contract_version": c.CONTRACT_VERSION})]


def test_run_stop_rpc(fake_svc):
    RixsServiceClient(service=fake_svc).run_stop(
        "u1", exit_status="success", num_events={"primary": 3})
    assert fake_svc.calls == [(c.RPC_STOP, {"run_uid": "u1", "exit_status": "success",
                                            "num_events": {"primary": 3}})]


def test_watches_the_session_events(fake_svc):
    RixsServiceClient(service=fake_svc)
    assert fake_svc.watched == {c.EVT_FRAME, c.EVT_COMPLETE, c.EVT_STATUS, c.EVT_ERROR}


def test_signals_emit(fake_svc, qtbot):
    cl = RixsServiceClient(service=fake_svc)
    got = {}
    cl.frameScored.connect(lambda d: got.setdefault("frame", d))
    cl.scanComplete.connect(lambda d: got.setdefault("done", d))
    cl.statusChanged.connect(lambda d: got.setdefault("status", d))
    cl.errorReceived.connect(lambda d: got.setdefault("err", d))
    assert cl.last_complete is None
    fake_svc.emit(c.EVT_FRAME, {"seq": 1})
    fake_svc.emit(c.EVT_COMPLETE, {"run_uid": "u", "best": 3})
    fake_svc.emit(c.EVT_STATUS, {"state": "x"})
    fake_svc.emit(c.EVT_ERROR, {"error": "e"})
    fake_svc.emit("something.else", {"ignored": True})
    assert got["frame"]["seq"] == 1
    assert got["done"]["best"] == 3
    assert cl.last_complete == {"run_uid": "u", "best": 3}
    assert got["status"]["state"] == "x"
    assert got["err"]["error"] == "e"


def test_health_follows_the_heartbeat(fake_svc, qtbot):
    cl = RixsServiceClient(service=fake_svc)  # fake starts "down"
    seen = []
    cl.healthChanged.connect(seen.append)
    assert cl.service_up is False and cl.discover() is None
    fake_svc.set_health("up", {"app_name": "rixs-live", "dispersion_mev_per_px": 2.5})
    assert cl.service_up is True
    assert cl.discover() == {"app_name": "rixs-live", "app_version": "0.0",
                             "dispersion_mev_per_px": 2.5}
    fake_svc.set_health("degraded")
    fake_svc.set_health("down")
    assert seen == [True, False]
    assert cl.service_up is False and cl.service_info is None


def test_unknown_state_leaves_health_unknown():
    from conftest import FakeService

    cl = RixsServiceClient(service=FakeService(health="unknown"))
    assert cl.service_up is None
    cl.check_health_async()
    assert cl.service_up is None


def test_follows_the_manager_client_and_its_recreation(qtbot):
    from conftest import FakeService, _Sig

    class Manager:
        def __init__(self):
            self.client_created = _Sig()
            self.clients = {}

        def client(self, name):
            return self.clients.get(name)

    first = FakeService()
    first.set_health("up")
    manager = Manager()
    manager.clients[c.SERVICE_NAME] = first
    cl = RixsServiceClient(manager=manager)
    assert cl.service_up is True
    second = FakeService()  # e.g. after ServiceManager.restart("rixs-live")
    manager.clients[c.SERVICE_NAME] = second
    manager.client_created.emit("some-other-service")
    assert cl._svc is first
    manager.client_created.emit(c.SERVICE_NAME)
    assert cl._svc is second and cl.service_up is False
    cl.bind_run("u1")
    assert second.calls and not first.calls


def test_get_result_ok(up_svc):
    up_svc.replies[c.RPC_RESULT] = {"status": "ok", "result": {"run_uid": "u", "best": 2},
                                    "contract_version": "1.0"}
    assert RixsServiceClient(service=up_svc).get_result("u") == {"run_uid": "u", "best": 2}
    assert up_svc.calls == [(c.RPC_RESULT, {"run_uid": "u"})]


def test_get_result_pending_passthrough(up_svc):
    up_svc.replies[c.RPC_RESULT] = {"status": "ok", "pending": True, "run_uid": "u",
                                    "frames_done": 3, "total": 7, "contract_version": "1.0"}
    r = RixsServiceClient(service=up_svc).get_result("u")
    assert r == {"pending": True, "run_uid": "u", "frames_done": 3, "total": 7}


@pytest.mark.parametrize("reply", [
    {"status": "ok", "result": None, "contract_version": "1.0"},  # unknown run
    {"status": "error", "code": "timeout", "message": "x", "contract_version": "1.0"},
    TimeoutError("late"),
])
def test_get_result_none_for_unknown_error_or_timeout(up_svc, reply):
    up_svc.replies[c.RPC_RESULT] = reply
    assert RixsServiceClient(service=up_svc).get_result("u") is None


def test_no_service_client_is_safe(qtbot):
    cl = RixsServiceClient(manager=None)
    cl._manager = None
    seen = []
    cl.bindSent.connect(seen.append)
    cl.bind_run("u1")  # logged, never raises
    cl.run_stop("u1")
    assert seen == ["u1"]
    assert cl.get_result("u1") is None and cl.discover() is None


def test_call_raising_is_safe(fake_svc):
    fake_svc.call_exc = RuntimeError("QtServiceClient is not running")
    cl = RixsServiceClient(service=fake_svc)
    cl.bind_run("u1")
    assert cl.get_result("u1") is None


def test_failed_bind_reaches_error_signal(up_svc, qtbot):
    up_svc.replies[c.RPC_BIND] = {"status": "error", "code": "unknown",
                                  "message": "client still starting", "contract_version": "1.0"}
    cl = RixsServiceClient(service=up_svc)
    errors = []
    cl.errorReceived.connect(errors.append)
    cl.bind_run("u1")
    qtbot.waitUntil(lambda: bool(errors), timeout=2000)
    assert errors[0]["run_uid"] == "u1" and "client still starting" in errors[0]["error"]


def test_bind_emits_bind_sent(fake_svc, qtbot):
    cl = RixsServiceClient(service=fake_svc)
    seen = []
    cl.bindSent.connect(seen.append)
    cl.bind_run("u1")
    assert seen == ["u1"]


# -- per-frame score cache -----------------------------------------------------

def test_frame_scores_cached_without_preview(fake_svc, qtbot):
    cl = RixsServiceClient(service=fake_svc)
    assert cl.get_score("u", 1) is None
    fake_svc.emit(c.EVT_FRAME, {"run_uid": "u", "seq_num": 1, "fwhm_px": 6.0,
                                 "score_valid": True, "positions": {"a": 1.0},
                                 "preview": {"b64": "..."}, "centroid": [1, 2]})
    got = cl.get_score("u", 1)
    assert got == {"run_uid": "u", "seq_num": 1, "fwhm_px": 6.0, "score_valid": True,
                   "positions": {"a": 1.0}}
    got["fwhm_px"] = 0  # a copy: callers cannot corrupt the cache
    assert cl.get_score("u", 1)["fwhm_px"] == 6.0
    assert cl.get_score("u", 2) is None and cl.get_score("other", 1) is None


def test_score_cache_ignores_malformed_and_is_bounded(fake_svc, monkeypatch):
    cl = RixsServiceClient(service=fake_svc)
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


def test_wait_score_timeout_and_wakeup(fake_svc):
    import threading
    import time

    cl = RixsServiceClient(service=fake_svc)
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


def test_score_cache_thread_safety(fake_svc):
    """Concurrent writers (as from the session) and readers (as from the RunEngine) agree."""
    import threading

    cl = RixsServiceClient(service=fake_svc)
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
