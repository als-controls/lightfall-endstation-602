from unittest.mock import MagicMock

from lightfall_endstation_602 import contract as c
from lightfall_endstation_602.binder import RixsRunBinder
from lightfall_endstation_602.client import RixsServiceClient


def _binder(fake_ipc):
    client = RixsServiceClient(ipc=fake_ipc)
    re = MagicMock()
    re.subscribe.return_value = 7
    b = RixsRunBinder(
        client,
        run_engine_getter=lambda: re,
        credentials_getter=lambda: ("http://t", "key", None),
        prefix_getter=lambda: "als.602",
    )
    return b, re


def _start(uid="runX"):
    return {"uid": uid, "plan_name": "zeroth", c.MD_KEY: {"contract_version": 1}}


def test_enable_disable(fake_ipc):
    b, re = _binder(fake_ipc)
    b.enable()
    b.enable()
    assert b.enabled
    re.subscribe.assert_called_once()
    b.disable()
    re.unsubscribe.assert_called_once_with(7)
    assert not b.enabled


def test_binder_bind_and_stop(fake_ipc):
    b, re = _binder(fake_ipc)
    b.enable()
    cb = re.subscribe.call_args[0][0]
    cb("start", _start())
    assert fake_ipc.published == [(c.SUBJ_BIND, {
        "run_uid": "runX", "tiled_url": "http://t",
        "lightfall_prefix": "als.602", "contract_version": c.CONTRACT_VERSION})]
    assert b._client.last_bound_uid == "runX"
    cb("stop", {"run_start": "runX"})
    assert (c.SUBJ_STOP, {"run_uid": "runX"}) in fake_ipc.published
    assert b._client.last_bound_uid == "runX"  # kept after stop: the result comes later


def test_binder_ignores_non_rixs_start(fake_ipc):
    b, re = _binder(fake_ipc)
    b.enable()
    cb = re.subscribe.call_args[0][0]
    cb("start", {"uid": "plain", "plan_name": "count"})
    cb("stop", {"run_start": "plain"})
    assert fake_ipc.published == []
    assert b._client.last_bound_uid is None


def test_callback_exceptions_swallowed(fake_ipc):
    b, re = _binder(fake_ipc)
    b._get_creds = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    b.enable()
    re.subscribe.call_args[0][0]("start", _start())


def test_bind_payload_never_carries_api_key(fake_ipc):
    b, re = _binder(fake_ipc)
    b.enable()
    re.subscribe.call_args[0][0]("start", _start())
    assert "tiled_api_key" not in fake_ipc.published[0][1]


def test_default_credentials_never_read_session_key(monkeypatch):
    import sys
    import types
    from lightfall_endstation_602 import binder as bm
    boom = MagicMock(side_effect=AssertionError("key must not be read"))
    mod = types.ModuleType("lightfall.auth.session")
    mod.SessionManager = types.SimpleNamespace(get_instance=boom)
    monkeypatch.setitem(sys.modules, "lightfall.auth.session", mod)
    url, key, _ = bm._default_credentials()
    assert key is None
    boom.assert_not_called()


def test_listeners_notified_on_enable_disable(fake_ipc):
    b, re = _binder(fake_ipc)
    seen = []
    b.add_listener(seen.append)
    b.enable()
    b.enable()  # no-op: no second notification
    b.disable()
    b.disable()
    assert seen == [True, False]
    b.remove_listener(seen.append)


def test_listener_exception_does_not_break_enable(fake_ipc):
    b, re = _binder(fake_ipc)
    b.add_listener(lambda enabled: 1 / 0)
    b.enable()
    assert b.enabled
