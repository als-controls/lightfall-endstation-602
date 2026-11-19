"""The Tiled proxy from rixs.run.bind reaches the Tiled client; failures name URL and proxy."""
import socket
import threading

import pytest
import tiled.client.context as context_mod

from rixs_live.config import AppConfig
from rixs_live.contract import CONTRACT_VERSION, EVT_ERROR
from rixs_live.service import RixsLiveService
from rixs_live.tiled_io import connect_tiled, redact_proxy

TARGET = "http://tiled.invalid:8000"


class RecordingProxy:
    """Minimal HTTP proxy stand-in: records each request line and answers 404."""

    def __init__(self):
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.url = f"http://127.0.0.1:{self._sock.getsockname()[1]}"
        self.request_lines: list[str] = []
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with conn:
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                if data:
                    self.request_lines.append(data.split(b"\r\n", 1)[0].decode())
                conn.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n"
                             b"Connection: close\r\n\r\n")

    def close(self):
        self._sock.close()


@pytest.fixture
def proxy_server():
    p = RecordingProxy()
    yield p
    p.close()


@pytest.fixture
def no_env_proxy(monkeypatch):
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)


def test_explicit_proxy_routes_every_request(proxy_server, no_env_proxy):
    original = (context_mod.Transport, context_mod.httpx)
    with pytest.raises(Exception):
        connect_tiled(TARGET, None, proxy_server.url)  # 404 from the "proxy"
    assert proxy_server.request_lines, "nothing went through the proxy"
    # Absolute-form request lines: the proxy was asked for the Tiled host.
    assert all(f"GET {TARGET}/" in line for line in proxy_server.request_lines)
    assert (context_mod.Transport, context_mod.httpx) == original  # patch undone


def test_env_proxy_still_honoured_without_bind_proxy(proxy_server, no_env_proxy,
                                                     monkeypatch):
    monkeypatch.setenv("ALL_PROXY", proxy_server.url)
    with pytest.raises(Exception):
        connect_tiled(TARGET, None, None)
    assert proxy_server.request_lines
    assert all(f"GET {TARGET}/" in line for line in proxy_server.request_lines)


def test_explicit_proxy_beats_env(proxy_server, no_env_proxy, monkeypatch):
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")  # nothing listens there
    with pytest.raises(Exception):
        connect_tiled(TARGET, None, proxy_server.url)
    assert proxy_server.request_lines


def test_redact_proxy():
    assert redact_proxy("socks5://localhost:1080") == "socks5://localhost:1080"
    assert redact_proxy("http://u:pw@proxy:3128") == "http://proxy:3128"


class FakeNats:
    def __init__(self):
        self.published = []
        self.tiled_credentials = None

    async def emit(self, session, name, payload):
        self.published.append((name, payload))


def _bind(**over):
    return {"run_uid": "r1", "tiled_url": TARGET, "contract_version": CONTRACT_VERSION,
            **over}


def _service(connect):
    fake = FakeNats()
    return RixsLiveService(AppConfig(bind_lookup_timeout_s=0.1), nats_client=fake,
                           emit=fake.emit, tiled_connect=connect)


async def test_bind_passes_tiled_proxy_to_client():
    seen = []

    def connect(url, key, proxy=None):
        seen.append((url, proxy))
        return {}  # empty "container": the run lookup then fails, which is fine here

    svc = _service(connect)
    await svc._handle_bind(_bind(tiled_proxy="socks5://localhost:1080"), None)
    await svc._handle_bind(_bind(tiled_proxy=None), None)
    await svc._handle_bind(_bind(), None)  # field absent
    assert seen == [(TARGET, "socks5://localhost:1080"), (TARGET, None), (TARGET, None)]


@pytest.mark.parametrize("proxy, env, expect", [
    ("socks5://localhost:1080", None, "proxy socks5://localhost:1080 (from Lightfall)"),
    (None, "socks5://envproxy:1080", "proxy socks5://envproxy:1080 (from the service"),
    (None, None, "using no proxy"),
])
async def test_connect_failure_error_names_url_and_proxy(no_env_proxy, monkeypatch,
                                                         proxy, env, expect):
    if env:
        monkeypatch.setenv("ALL_PROXY", env)

    def connect(url, key, proxy=None):
        raise TimeoutError("timed out")

    svc = _service(connect)
    await svc._handle_bind(_bind(tiled_proxy=proxy), None)
    ((subject, payload),) = svc._nats.published
    assert subject == EVT_ERROR and payload["run_uid"] == "r1"
    err = payload["error"]
    assert f"Tiled at {TARGET}" in err and expect in err
    assert "TimeoutError" in err and "Check the proxy" in err
