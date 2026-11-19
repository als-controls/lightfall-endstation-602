from lightfall_endstation_602 import runtime


def test_singletons_and_ensure(monkeypatch):
    monkeypatch.setattr(runtime, "_client", None)
    monkeypatch.setattr(runtime, "_binder", None)
    from conftest import FakeService
    from lightfall_endstation_602.client import RixsServiceClient
    monkeypatch.setattr(runtime, "RixsServiceClient", lambda: RixsServiceClient(service=FakeService()))
    cl = runtime.get_client()
    assert runtime.get_client() is cl
    b = runtime.get_binder()
    assert runtime.get_binder() is b

    def boom():
        raise RuntimeError("no RE")
    b._get_re = boom
    assert runtime.ensure_binder_enabled() is False
    class RE:
        def subscribe(self, f): return 1
    b._get_re = lambda: RE()
    assert runtime.ensure_binder_enabled() is True
    assert runtime.ensure_binder_enabled() is True
