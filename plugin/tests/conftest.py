import pytest


class FakeIPC:
    """Duck-type of lightfall.ipc.service.IPCService for unit tests."""

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
