import asyncio
import json
from pathlib import Path

import pytest

from lightfall_endstation_602 import runtime, tools
from lightfall_endstation_602.tools import QerlinFocusingTools


class FakeClient:
    def __init__(self, discover=None, results=None, last_complete=None):
        self._discover = discover
        self._results = results or {}
        self.last_complete = last_complete
        self.asked = []

    def discover(self, timeout_ms=2000):
        return self._discover

    def get_result(self, run_uid, timeout_ms=5000):
        self.asked.append(run_uid)
        return self._results.get(run_uid)


@pytest.fixture
def plugin(monkeypatch):
    monkeypatch.setattr(tools, "_REGISTERED", True)  # skip registry side effects
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
