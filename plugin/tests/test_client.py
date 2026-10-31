from lightfall_endstation_602 import contract as c
from lightfall_endstation_602.client import RixsServiceClient


def test_bind_payload(fake_ipc):
    cl = RixsServiceClient(ipc=fake_ipc)
    cl.bind_run("u1", tiled_url="http://t", tiled_api_key="k", lightfall_prefix="als.602")
    assert fake_ipc.published == [(c.SUBJ_BIND, {
        "run_uid": "u1", "tiled_url": "http://t", "tiled_api_key": "k",
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
