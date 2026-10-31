from pathlib import Path

import pytest

import rixs_live.nats_capability as cap
from rixs_live.nats_capability import VENDORED_FROM, derive_prefix

STXM_COPY = (Path(__file__).resolve().parents[3]
             / "stxm-live" / "src" / "stxm_live" / "nats_capability.py")


def test_vendored_marker():
    assert VENDORED_FROM == "lightfall_utils.ipc.capability v2"


def test_derive_prefix_vectors():
    assert derive_prefix("test-secret-0001") == "r5itqnjn2uhkfjylxqpf7fwjv7"
    assert derive_prefix("A" * 43) == "3dcidrblqoesmah3mp7u5jx4cx"


@pytest.mark.skipif(not STXM_COPY.exists(), reason="stxm-live checkout not alongside")
def test_identical_to_stxm_live_copy():
    # Compare with line endings normalized: a Windows checkout may hold CRLF.
    ours = Path(cap.__file__).read_bytes().replace(b"\r\n", b"\n")
    theirs = STXM_COPY.read_bytes().replace(b"\r\n", b"\n")
    assert ours == theirs
