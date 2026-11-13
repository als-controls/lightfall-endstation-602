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


def test_agent_spec_has_optimization():
    path = Path(lightfall_endstation_602.__file__).parent / "agents" / "qerlin.md"
    spec = parse_agent_file(path, "beamline")
    assert "autonomous_experiment" in spec.tools
    assert {"qerlin_optimization", "autonomous_experiment"} <= set(spec.skills)
    assert "qerlin_optimization" in spec.prompt and "qerlin_optimize" in spec.prompt


def test_optimization_skill_frontmatter_and_plugin():
    from lightfall_endstation_602.tools import QerlinOptimizationSkill

    p = QerlinOptimizationSkill()
    assert p.name == "qerlin_optimization" and p.create_tools() == []
    text = (p.get_skill_dir() / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    head = text.split("---")[1]
    assert "name: qerlin-optimization" in head and "description:" in head
    body = text.split("---", 2)[2]
    for word in ("qerlin_optimize", "rixs_score", "autonomous_experiment",
                 "qerlin_focus_scan", "rixs_service_status", "tsuchinoko_stop"):
        assert word in body, word
    assert (p.get_references_dir() / "optimization-notes.md").is_file()


def test_skill_names_resolve_to_plugins_or_builtins():
    """Each skill the agent lists is a plugin skill dir or a Lightfall builtin."""
    from lightfall.agents.skills_store import builtin_skills_dir

    from lightfall_endstation_602.tools import QerlinFocusingTools, QerlinOptimizationSkill
    path = Path(lightfall_endstation_602.__file__).parent / "agents" / "qerlin.md"
    spec = parse_agent_file(path, "beamline")
    plugin_skills = {QerlinFocusingTools().name, QerlinOptimizationSkill().name}
    builtin = {d.name for d in Path(builtin_skills_dir()).iterdir() if (d / "SKILL.md").is_file()}
    for name in spec.skills:
        assert name in plugin_skills | builtin, name
