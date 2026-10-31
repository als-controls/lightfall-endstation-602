import asyncio
import json
from pathlib import Path

import pytest

from lightfall_endstation_602 import runtime, tools
from lightfall_endstation_602.tools import QerlinFocusingTools


class FakeClient:
    def __init__(self, discover=None, results=None, last_complete=None, last_bound_uid=None):
        self._discover = discover
        self._results = results or {}
        self.last_complete = last_complete
        self.last_bound_uid = last_bound_uid
        self.asked = []

    def discover(self, timeout_ms=2000):
        return self._discover

    def get_result(self, run_uid, timeout_ms=5000):
        self.asked.append(run_uid)
        r = self._results.get(run_uid)
        if isinstance(r, list):  # a sequence of replies, last one repeats
            return r.pop(0) if len(r) > 1 else r[0]
        return r


@pytest.fixture
def plugin(monkeypatch):
    monkeypatch.setattr(tools, "_REGISTERED", True)  # skip registry side effects
    monkeypatch.setattr(runtime, "ensure_binder_enabled", lambda: True)
    return QerlinFocusingTools()


def _tool(plugin, name):
    (spec,) = [t for t in plugin.create_tools() if t.name == name]
    return spec.handler


def _payload(result):
    return json.loads(result["content"][0]["text"])


def test_status_service_down(plugin, monkeypatch):
    monkeypatch.setattr(runtime, "get_client", lambda: FakeClient(discover=None))
    monkeypatch.setattr(runtime, "ensure_binder_enabled", lambda: False)
    out = _payload(asyncio.run(_tool(plugin, "rixs_service_status")({})))
    assert out["service_up"] is False
    assert "rixs-live run" in out["hint"]


def test_status_service_up(plugin, monkeypatch):
    reply = {"app_name": "rixs-live", "app_version": "1.2.3", "actions": [], "events": [],
             "dispersion_mev_per_px": 0.5}
    monkeypatch.setattr(runtime, "get_client", lambda: FakeClient(discover=reply))
    monkeypatch.setattr(runtime, "ensure_binder_enabled", lambda: True)
    out = _payload(asyncio.run(_tool(plugin, "rixs_service_status")({})))
    assert out == {"service_up": True, "app_version": "1.2.3",
                   "dispersion_mev_per_px": 0.5, "binder_enabled": True}


def test_get_focus_result_defaults_to_last(plugin, monkeypatch):
    payload = {"run_uid": "u1", "fit": {"ok": True, "optimal_position": 0.2}}
    client = FakeClient(results={"u1": payload}, last_complete={"run_uid": "u1"})
    monkeypatch.setattr(runtime, "get_client", lambda: client)
    out = _payload(asyncio.run(_tool(plugin, "get_focus_result")({})))
    assert out == payload
    assert client.asked == ["u1"]


def test_get_focus_result_missing_errors(plugin, monkeypatch):
    monkeypatch.setattr(runtime, "get_client", lambda: FakeClient())
    res = asyncio.run(_tool(plugin, "get_focus_result")({"run_uid": "nope"}))
    assert res.get("is_error") is True
    assert "no focus result for nope" in res["content"][0]["text"]


def test_get_focus_result_no_uid_at_all(plugin, monkeypatch):
    monkeypatch.setattr(runtime, "get_client", lambda: FakeClient())
    res = asyncio.run(_tool(plugin, "get_focus_result")({}))
    assert res.get("is_error") is True


def test_register_scope_dir_once(monkeypatch):
    calls = []

    class Reg:
        def register_scope_dir(self, scope, path):
            calls.append((scope, path))

    from lightfall.agents.registry import AgentSpecRegistry

    monkeypatch.setattr(AgentSpecRegistry, "get_instance", classmethod(lambda cls: Reg()))
    monkeypatch.setattr(tools, "_REGISTERED", False)
    QerlinFocusingTools()
    QerlinFocusingTools()
    assert len(calls) == 1
    assert calls[0][0] == "beamline"
    assert Path(calls[0][1]).name == "agents"


def test_skill_frontmatter(plugin):
    text = (plugin.get_skill_dir() / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    head = text.split("---")[1]
    assert "name: qerlin-focusing" in head
    assert "description:" in head
    assert (plugin.get_references_dir() / "rixs-terminology.md").is_file()


def test_singletons_resolved_on_main_thread(plugin, monkeypatch):
    """Client/binder access goes through run_on_main_thread; only the request is threaded."""
    import threading

    import lightfall.claude._internal.threading as lf_threading

    state = {"on_main": False, "get_client_on_main": [], "discover_thread": None}
    main_ident = threading.get_ident()

    def fake_run_on_main(func, *args):
        state["on_main"] = True
        try:
            return func(*args)
        finally:
            state["on_main"] = False

    class Client(FakeClient):
        def discover(self, timeout_ms=2000):
            state["discover_thread"] = threading.get_ident()
            return {"app_version": "1", "dispersion_mev_per_px": 1.0}

        def get_result(self, run_uid, timeout_ms=5000):
            state["discover_thread"] = threading.get_ident()
            return {"run_uid": run_uid}

    client = Client(last_complete={"run_uid": "u9"})

    def get_client():
        state["get_client_on_main"].append(state["on_main"])
        return client

    def ensure():
        assert state["on_main"]
        return True

    monkeypatch.setattr(lf_threading, "run_on_main_thread", fake_run_on_main)
    monkeypatch.setattr(runtime, "get_client", get_client)
    monkeypatch.setattr(runtime, "ensure_binder_enabled", ensure)

    out = _payload(asyncio.run(_tool(plugin, "rixs_service_status")({})))
    assert out["service_up"] is True
    assert state["discover_thread"] != main_ident
    out = _payload(asyncio.run(_tool(plugin, "get_focus_result")({})))
    assert out == {"run_uid": "u9"}
    assert state["discover_thread"] != main_ident
    assert state["get_client_on_main"] and all(state["get_client_on_main"])


def test_get_focus_result_rearms_binder(plugin, monkeypatch):
    calls = []
    client = FakeClient(results={"u1": {"run_uid": "u1"}}, last_complete={"run_uid": "u1"})
    monkeypatch.setattr(runtime, "get_client", lambda: client)
    monkeypatch.setattr(runtime, "ensure_binder_enabled", lambda: calls.append(1) or True)
    out = _payload(asyncio.run(_tool(plugin, "get_focus_result")({})))
    assert out == {"run_uid": "u1"}
    assert calls == [1]


def test_get_focus_result_missing_mentions_unbound_binder(plugin, monkeypatch):
    monkeypatch.setattr(runtime, "get_client", lambda: FakeClient())
    monkeypatch.setattr(runtime, "ensure_binder_enabled", lambda: False)
    res = asyncio.run(_tool(plugin, "get_focus_result")({"run_uid": "nope"}))
    assert res.get("is_error") is True
    assert "binder" in res["content"][0]["text"]


PENDING = {"pending": True, "run_uid": "new", "frames_done": 3, "total": 7}


def test_get_focus_result_defaults_to_last_bound_not_last_complete(plugin, monkeypatch):
    """The fit for the scan just run lands after idle; never hand back the previous one."""
    monkeypatch.setattr(tools, "POLL_INTERVAL_S", 0.001)
    new = {"run_uid": "new", "fit": {"ok": True}}
    client = FakeClient(results={"old": {"run_uid": "old"}, "new": new},
                        last_complete={"run_uid": "old"}, last_bound_uid="new")
    monkeypatch.setattr(runtime, "get_client", lambda: client)
    out = _payload(asyncio.run(_tool(plugin, "get_focus_result")({})))
    assert out == new
    assert client.asked == ["new"]


def test_get_focus_result_polls_pending_until_complete(plugin, monkeypatch):
    monkeypatch.setattr(tools, "POLL_INTERVAL_S", 0.001)
    done = {"run_uid": "new", "fit": {"ok": True, "optimal_position": 0.1}}
    client = FakeClient(results={"new": [dict(PENDING), dict(PENDING), done]},
                        last_complete={"run_uid": "old"}, last_bound_uid="new")
    monkeypatch.setattr(runtime, "get_client", lambda: client)
    out = _payload(asyncio.run(_tool(plugin, "get_focus_result")({"wait_s": 5})))
    assert out == done
    assert client.asked == ["new", "new", "new"]


def test_get_focus_result_pending_timeout_errors(plugin, monkeypatch):
    monkeypatch.setattr(tools, "POLL_INTERVAL_S", 0.01)
    client = FakeClient(results={"new": [dict(PENDING, total=None)]}, last_bound_uid="new")
    monkeypatch.setattr(runtime, "get_client", lambda: client)
    res = asyncio.run(_tool(plugin, "get_focus_result")({"wait_s": 0.05}))
    assert res.get("is_error") is True
    text = res["content"][0]["text"]
    assert "still running" in text and "3/? frames" in text
    assert len(client.asked) >= 2


def test_wait_s_clamped():
    assert tools._wait_s(None) == tools.WAIT_DEFAULT_S
    assert tools._wait_s("x") == tools.WAIT_DEFAULT_S
    assert tools._wait_s(-5) == 0.0
    assert tools._wait_s(10_000) == tools.WAIT_MAX_S
    assert tools._wait_s(float("nan")) == tools.WAIT_DEFAULT_S
