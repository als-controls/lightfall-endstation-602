"""rixs-live survives a broker restart; a CLOSED connection makes the service exit non-zero."""
import asyncio
import json
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import nats as natslib
import pytest

from rixs_live.config import AppConfig, NatsConfig
from rixs_live.contract import SUBJ_DISCOVER
from rixs_live.nats_client import NatsClient
from rixs_live.service import RixsLiveService


def _nats_binary():
    exe = Path(sys.executable).parent / (
        "nats-server.exe" if sys.platform == "win32" else "nats-server")
    if exe.exists():
        return str(exe)
    return shutil.which("nats-server")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_broker(port):
    proc = subprocess.Popen([_nats_binary(), "-a", "127.0.0.1", "-p", str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return proc
        except OSError:
            time.sleep(0.05)
    proc.kill()
    pytest.fail("nats-server did not become ready")


def _stop_broker(proc):
    proc.kill()
    proc.wait(timeout=5)


async def _discover(url, deadline_s):
    """Ask _rixs.discover until it answers or ``deadline_s`` passes; returns the reply."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + deadline_s
    last = None
    while loop.time() < deadline:
        try:
            nc = await natslib.connect(url, allow_reconnect=False, connect_timeout=1)
        except Exception as ex:  # broker not back yet
            last = ex
            await asyncio.sleep(0.2)
            continue
        try:
            msg = await nc.request(SUBJ_DISCOVER, b"{}", timeout=1)
            return json.loads(msg.data)
        except Exception as ex:  # no responders until the service has reconnected
            last = ex
            await asyncio.sleep(0.2)
        finally:
            await nc.close()
    raise AssertionError(f"_rixs.discover did not answer within {deadline_s} s: {last!r}")


@pytest.mark.skipif(_nats_binary() is None, reason="nats-server not found")
def test_discover_answers_after_broker_restart():
    port = _free_port()
    url = f"nats://127.0.0.1:{port}"

    async def scenario():
        broker = _start_broker(port)
        cfg = AppConfig()
        cfg.nats.url = url
        cfg.nats.reconnect_time_wait = 0.2
        cfg.nats.auth_timeout = 1.0
        svc = RixsLiveService(cfg)
        main = asyncio.get_running_loop().create_task(svc._main())
        try:
            assert (await _discover(url, 10.0))["app_name"] == "rixs-live"
            _stop_broker(broker)
            await asyncio.sleep(0.5)  # the service notices the disconnect
            assert not main.done()
            broker = _start_broker(port)  # same port, fresh server (no state kept)
            assert (await _discover(url, 10.0))["app_name"] == "rixs-live"
            assert not main.done()  # never exited, never needed a manual restart
        finally:
            main.cancel()
            try:
                assert await main == 0
            finally:
                _stop_broker(broker)

    asyncio.run(scenario())


class _ClosingNats:
    """Fake NATS client whose connection closes for good after the first poll."""

    def __init__(self):
        self.tiled_credentials = None
        self.on_reconnected = None
        self.polls = 0
        self.closed_calls = 0

    @property
    def is_closed(self):
        self.polls += 1
        return self.polls > 1

    async def connect(self): ...
    async def authenticate(self): return None
    async def subscribe(self, subject, cb): ...

    async def close(self):
        self.closed_calls += 1


async def test_closed_connection_exits_non_zero():
    fake = _ClosingNats()
    svc = RixsLiveService(AppConfig(), nats_client=fake)
    assert await asyncio.wait_for(svc._main(), 5) == 1
    assert fake.closed_calls == 1
    assert fake.on_reconnected == svc._on_reconnected


def test_main_raises_system_exit_on_closed(monkeypatch):
    svc = RixsLiveService(AppConfig(), nats_client=_ClosingNats())
    with pytest.raises(SystemExit) as info:
        svc.main()
    assert info.value.code == 1


async def test_nats_client_reconnects_forever_and_tracks_closed(monkeypatch):
    seen = {}

    class FakeNc:
        is_closed = False
        connected_url = None

    async def fake_connect(url, **kwargs):
        seen.update(kwargs, url=url)
        return FakeNc()

    monkeypatch.setattr(natslib, "connect", fake_connect)
    client = NatsClient(NatsConfig(url="nats://x:1"))
    await client.connect()
    assert seen["max_reconnect_attempts"] == -1
    for cb in ("disconnected_cb", "reconnected_cb", "closed_cb", "error_cb"):
        assert callable(seen[cb]), cb

    hooks = []

    async def hook():
        hooks.append(1)

    client.on_reconnected = hook
    await seen["reconnected_cb"]()
    assert hooks == [1]
    assert not client.is_closed
    await seen["closed_cb"]()
    assert client.is_closed


async def test_reconnect_reruns_auth():
    calls = []

    class Fake(_ClosingNats):
        async def authenticate(self):
            calls.append(1)

    svc = RixsLiveService(AppConfig(), nats_client=Fake())
    await svc._on_reconnected()
    await asyncio.sleep(0.05)
    assert calls == [1]
