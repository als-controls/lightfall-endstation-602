import importlib
import subprocess
import sys
import textwrap

from lightfall_endstation_602.manifest import manifest

EXPECTED = {
    ("device_backend", "qerlin_sim"): "lightfall_endstation_602.backend:QerlinSimBackendPlugin",
    ("plan", "qerlin_focus_scan"): "lightfall_endstation_602.plan_plugin:QerlinFocusScanPlanPlugin",
    ("panel", "qerlin_focus"): "lightfall_endstation_602.panel:QerlinFocusPanelPlugin",
    ("tool", "qerlin_focusing"): "lightfall_endstation_602.tools:QerlinFocusingTools",
}


def test_manifest_entries():
    got = {(e.type_name, e.name): e.import_path for e in manifest.plugins}
    assert got == EXPECTED
    (panel,) = [e for e in manifest.plugins if e.type_name == "panel"]
    assert panel.preload is True


def test_manifest_types():
    for e in manifest.plugins:
        assert e.type_name in {"device_backend", "plan", "panel", "tool"}
        mod, _, cls = e.import_path.partition(":")
        assert isinstance(getattr(importlib.import_module(mod), cls), type)


def test_no_heavy_imports():
    code = textwrap.dedent("""
        import sys
        import lightfall_endstation_602.manifest, lightfall_endstation_602.panel
        import lightfall_endstation_602.tools
        heavy = ["cv2", "scipy", "skimage", "pandas", "tifffile", "rixs_app"]
        bad = [m for m in heavy if m in sys.modules]
        print("HEAVY:", bad)
        sys.exit(1 if bad else 0)
    """)
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
