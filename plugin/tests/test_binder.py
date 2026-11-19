from unittest.mock import MagicMock

from lightfall_endstation_602 import contract as c
from lightfall_endstation_602.binder import RixsRunBinder
from lightfall_endstation_602.client import RixsServiceClient


def _binder(fake_svc):
    client = RixsServiceClient(service=fake_svc)
    re = MagicMock()
    re.subscribe.return_value = 7
    b = RixsRunBinder(
        client,
        run_engine_getter=lambda: re,
        credentials_getter=lambda: ("http://t", "key", None),
    )
    return b, re


def _start(uid="runX"):
    return {"uid": uid, "plan_name": "zeroth", c.MD_KEY: {"contract_version": 1}}


def test_enable_disable(fake_svc):
    b, re = _binder(fake_svc)
    b.enable()
    b.enable()
    assert b.enabled
    re.subscribe.assert_called_once()
    b.disable()
    re.unsubscribe.assert_called_once_with(7)
    assert not b.enabled


def test_binder_bind_and_stop(fake_svc):
    b, re = _binder(fake_svc)
    b.enable()
    cb = re.subscribe.call_args[0][0]
    cb("start", _start())
    assert fake_svc.published == [(c.RPC_BIND, {
        "run_uid": "runX", "tiled_url": "http://t", "tiled_proxy": None,
        "contract_version": c.CONTRACT_VERSION})]
    assert b._client.last_bound_uid == "runX"
    cb("stop", {"run_start": "runX"})
    assert (c.RPC_STOP, {"run_uid": "runX"}) in fake_svc.published
    assert b._client.last_bound_uid == "runX"  # kept after stop: the result comes later


def test_binder_ignores_non_rixs_start(fake_svc):
    b, re = _binder(fake_svc)
    b.enable()
    cb = re.subscribe.call_args[0][0]
    cb("start", {"uid": "plain", "plan_name": "count"})
    cb("stop", {"run_start": "plain"})
    assert fake_svc.published == []
    assert b._client.last_bound_uid is None


def test_callback_exceptions_swallowed(fake_svc):
    b, re = _binder(fake_svc)
    b._get_creds = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    b.enable()
    re.subscribe.call_args[0][0]("start", _start())


def test_bind_payload_never_carries_api_key(fake_svc):
    b, re = _binder(fake_svc)
    b.enable()
    re.subscribe.call_args[0][0]("start", _start())
    assert "tiled_api_key" not in fake_svc.published[0][1]


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


def test_listeners_notified_on_enable_disable(fake_svc):
    b, re = _binder(fake_svc)
    seen = []
    b.add_listener(seen.append)
    b.enable()
    b.enable()  # no-op: no second notification
    b.disable()
    b.disable()
    assert seen == [True, False]
    b.remove_listener(seen.append)


def test_listener_exception_does_not_break_enable(fake_svc):
    b, re = _binder(fake_svc)
    b.add_listener(lambda enabled: 1 / 0)
    b.enable()
    assert b.enabled


def test_stop_forwards_exit_status_and_num_events(fake_svc):
    b, re = _binder(fake_svc)
    b.enable()
    cb = re.subscribe.call_args[0][0]
    cb("start", _start())
    cb("stop", {"run_start": "runX", "exit_status": "abort",
                "num_events": {"primary": 3}})
    assert (c.RPC_STOP, {"run_uid": "runX", "exit_status": "abort",
                          "num_events": {"primary": 3}}) in fake_svc.published


def test_bind_carries_lightfall_tiled_proxy(fake_svc):
    client = RixsServiceClient(service=fake_svc)
    re = MagicMock()
    asked = []
    b = RixsRunBinder(client, run_engine_getter=lambda: re,
                      credentials_getter=lambda: ("http://t", None, None),
                      proxy_getter=lambda url: asked.append(url) or "socks5://localhost:1080")
    b.enable()
    re.subscribe.call_args[0][0]("start", _start())
    assert asked == ["http://t"]
    assert fake_svc.published[0][1]["tiled_proxy"] == "socks5://localhost:1080"


def test_bind_proxy_lookup_failure_sends_null(fake_svc):
    client = RixsServiceClient(service=fake_svc)
    re = MagicMock()

    def boom(url):
        raise RuntimeError("no proxy settings")

    b = RixsRunBinder(client, run_engine_getter=lambda: re,
                      credentials_getter=lambda: ("http://t", None, None), proxy_getter=boom)
    b.enable()
    re.subscribe.call_args[0][0]("start", _start())
    (subject, payload), = fake_svc.published
    assert subject == c.RPC_BIND and payload["tiled_proxy"] is None


def test_default_proxy_uses_tiled_service_lookup(monkeypatch):
    import sys
    import types
    from lightfall_endstation_602 import binder as bm

    class TiledService:
        @staticmethod
        def _get_proxy_url(url):
            return "socks5://localhost:1080" if "lbl.gov" in url else None

    mod = types.ModuleType("lightfall.services.tiled_service")
    mod.TiledService = TiledService
    monkeypatch.setitem(sys.modules, "lightfall.services.tiled_service", mod)
    assert bm._default_proxy("http://bcgtiled.dhcp.lbl.gov:8000") == "socks5://localhost:1080"
    assert bm._default_proxy("http://localhost:8000") is None
    assert bm._default_proxy("") is None

    def boom(url):
        raise RuntimeError("preferences not ready")

    TiledService._get_proxy_url = staticmethod(boom)
    assert bm._default_proxy("http://x.lbl.gov") is None
    del TiledService._get_proxy_url  # older Lightfall without the helper
    assert bm._default_proxy("http://x.lbl.gov") is None
