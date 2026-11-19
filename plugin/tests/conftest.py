import concurrent.futures

import pytest

from lightfall_endstation_602 import contract as c


class _Sig:
    """Synchronous stand-in for a Qt signal."""

    def __init__(self):
        self._slots = []

    def connect(self, fn):
        self._slots.append(fn)

    def emit(self, *args):
        for fn in list(self._slots):
            fn(*args)


class FakeService:
    """Duck-type of lightfall_utils' QtServiceClient (the rixs-live session) for unit tests.

    ``calls`` records every rpc; ``replies[action]`` is its reply (default: accepted);
    ``call_exc`` makes ``call`` raise; ``emit(name, payload)`` delivers a session event;
    ``set_health(state, summary)`` mimics a heartbeat-driven health change.
    """

    def __init__(self, health="down"):
        self.calls = []
        self.replies = {}
        self.call_exc = None
        self.watched = set()
        self.health = health
        self.info = None
        self.event = _Sig()
        self.state_changed = _Sig()
        self.info_changed = _Sig()
        self.restarted = _Sig()

    # what the tests read
    @property
    def published(self):
        return list(self.calls)

    requests = published

    def watch_event(self, name):
        self.watched.add(name)

    def call(self, action, payload, timeout=2.0):
        self.calls.append((action, payload))
        if self.call_exc is not None:
            raise self.call_exc
        fut = concurrent.futures.Future()
        reply = self.replies.get(action, {"status": "ok", "accepted": True})
        if isinstance(reply, BaseException):
            fut.set_exception(reply)
        else:
            fut.set_result(reply)
        return fut

    def emit(self, name, payload):
        self.event.emit(name, payload)

    def set_health(self, state, summary=None):
        self.health = state
        if state in ("up", "degraded"):
            self.info = {"name": c.SERVICE_NAME, "version": "0.0", "state": "ready",
                         "summary": summary or {"app_name": c.SERVICE_NAME}}
            self.info_changed.emit(self.info)
        self.state_changed.emit(state)


class FakeIPC:
    """Duck-type of lightfall.ipc.service.IPCService (Lightfall's own bus: tsuchinoko)."""

    def __init__(self):
        self.published = []
        self.requests = []
        self.replies = {}
        self.subscriptions = {}
        self.request_exc = None

    def publish(self, subject, data):
        self.published.append((subject, data))

    def request(self, subject, data, timeout_ms=1000):
        self.requests.append((subject, data))
        if self.request_exc is not None:
            raise self.request_exc
        return self.replies.get(subject)

    def subscribe(self, subject, callback, *, main_thread=True):
        self.subscriptions[subject] = callback

    def emit(self, subject, data):
        self.subscriptions[subject](subject, data, None)


@pytest.fixture
def fake_ipc():
    return FakeIPC()


@pytest.fixture
def fake_svc():
    return FakeService()


@pytest.fixture
def up_svc():
    svc = FakeService()
    svc.set_health("up")
    return svc
