"""Guards the vendored copy of lightfall_utils.ipc.services (host, core, protocol)."""
import subprocess
import sys
from pathlib import Path

import pytest

from rixs_live import svc
from rixs_live.svc import protocol

UPSTREAM = (Path(__file__).resolve().parents[3] / "lightfall-utils" / "src" / "lightfall_utils"
            / "ipc")


def test_vendored_from_marker():
    assert svc.VENDORED_FROM == "lightfall_utils.ipc.services 6bec6d2"


def test_root():
    assert protocol.root("als.602", "rixs-live") == "als.602.svc.rixs-live"


def test_exports_and_no_qt():
    code = ("import sys; from rixs_live.svc.host import ServiceHost; "
            "from rixs_live.svc.core import NatsCore; "
            "assert 'PySide6' not in sys.modules and 'lightfall_utils' not in sys.modules")
    subprocess.run([sys.executable, "-c", code], check=True)


def _body(path: Path) -> list[str]:
    """Source lines without the import lines (the only lines a vendored copy may change)."""
    lines = path.read_bytes().replace(b"\r\n", b"\n").decode().splitlines()
    return [ln for ln in lines if not ln.startswith(("from ", "import "))]


@pytest.mark.skipif(not UPSTREAM.exists(), reason="lightfall-utils checkout not alongside")
@pytest.mark.parametrize("ours, theirs", [("host.py", "services/host.py"),
                                          ("core.py", "services/core.py"),
                                          ("protocol.py", "services/protocol.py"),
                                          ("base_protocol.py", "protocol.py")])
def test_matches_upstream_except_imports(ours, theirs):
    assert _body(Path(svc.__file__).parent / ours) == _body(UPSTREAM / theirs)
