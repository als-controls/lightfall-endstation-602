from pathlib import Path

import lightfall_endstation_602
from lightfall.agents.spec import parse_agent_file


def test_agent_spec_parses():
    path = Path(lightfall_endstation_602.__file__).parent / "agents" / "qerlin.md"
    spec = parse_agent_file(path, "beamline")
    assert spec.name == "qerlin"
    assert "qerlin_focusing" in spec.tools
    assert "qerlin_focusing" in spec.skills
    assert "plan_review" in spec.skills
