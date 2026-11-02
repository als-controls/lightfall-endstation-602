"""Pin how a real TiledWriter-written focus scan reads back, and FocusRunReader over it."""
import uuid

import bluesky.plans as bp
import numpy as np
import pytest
from bluesky import RunEngine
from bluesky_tiled_plugins import TiledWriter
from lightfall_endstation_602.contract import build_rixs_md
from lightfall_endstation_602.sim.devices import SimMarana, SimSM3Pitch

from rixs_live.contract import RixsRunInfo
from rixs_live.tiled_io import FocusRunReader

SHAPE = (48, 64)  # small: these tests pin layout only, nothing is scored


def _info(uid="run1", shape=SHAPE):
    return RixsRunInfo(uid, "marana_image", "sm3_pitch", shape, 850.0)


def _scan(tiled_client, n=3, on_doc=None):
    """Run a real bp.scan through TiledWriter(batch_size=1); return the run uid."""
    tw = TiledWriter(tiled_client, batch_size=1)
    RE = RunEngine({})
    RE.subscribe(tw)
    if on_doc is not None:
        RE.subscribe(on_doc)  # after the writer: sees each doc once it is in Tiled
    sm3 = SimSM3Pitch(name="sm3_pitch")
    marana = SimMarana(name="marana", frame_shape=SHAPE)
    md = {"rixs": build_rixs_md("marana_image", "sm3_pitch", SHAPE, 850.0)}
    (uid,) = RE(bp.scan([marana], sm3, -1.0, 1.0, n, md=md))
    return uid


def test_tiledwriter_layout_is_n_h_w_and_scalar_column(tiled_client):
    uid = _scan(tiled_client, n=3)
    run = tiled_client[uid]
    assert run.metadata["start"]["rixs"]["frame_shape"] == list(SHAPE)
    primary = run["primary"]
    # Real behavior (tiled 0.2.x, bluesky-tiled-plugins 2.0.x): the image is an
    # internal zarr array node stacked (n, H, W); the motor is a 1-D (n,) column.
    # SynAxis also emits sm3_pitch_setpoint; we key on sm3_pitch exactly.
    assert {"marana_image", "sm3_pitch", "sm3_pitch_setpoint", "seq_num"} <= set(primary)
    assert primary["marana_image"].shape == (3, *SHAPE)
    assert primary["sm3_pitch"].shape == (3,)
    assert np.allclose(primary["sm3_pitch"].read(), [-1.0, 0.0, 1.0])
    assert primary["marana_image"][1:3].shape == (2, *SHAPE)  # row slicing works


def test_reader_reads_real_run(tiled_client):
    uid = _scan(tiled_client, n=3)
    run = tiled_client[uid]
    reader = FocusRunReader(run, _info(uid))
    got = reader.read_new(0)
    assert [s for s, _, _ in got] == [1, 2, 3]
    assert np.allclose([p for _, p, _ in got], [-1.0, 0.0, 1.0])
    stored = run["primary"]["marana_image"].read()
    for i, (_, _, frame) in enumerate(got):
        assert frame.shape == SHAPE
        assert np.array_equal(frame, stored[i])
    tail = reader.read_new(2)
    assert [(s, p) for s, p, _ in tail] == [(3, 1.0)]
    assert reader.read_new(3) == []


def test_reader_sees_frames_live_and_keyerror_before_stream(tiled_client):
    seen = {"before": [], "counts": []}
    state = {"reader": None, "next": 0}

    def on_doc(name, doc):
        if name == "start":
            state["reader"] = FocusRunReader(tiled_client[doc["uid"]], _info(doc["uid"]))
        if name in ("start", "descriptor"):
            try:
                seen["before"].append((name, state["reader"].read_new(0)))
            except KeyError:
                seen["before"].append((name, "KeyError"))
        if name == "event":
            new = state["reader"].read_new(state["next"])
            state["next"] += len(new)
            seen["counts"].append(len(new))

    _scan(tiled_client, n=4, on_doc=on_doc)
    # Before the first event there is no image node at all -> KeyError.
    assert seen["before"] == [("start", "KeyError"), ("descriptor", "KeyError")]
    # batch_size=1: every event is readable as soon as the writer has handled it.
    assert seen["counts"] == [1, 1, 1, 1]


def _write_manual(client, images, positions):
    run = client.create_container(str(uuid.uuid4()))
    primary = run.create_container("primary")
    primary.write_array(np.asarray(images, dtype=float), key="marana_image")
    primary.write_array(np.asarray(positions, dtype=float), key="sm3_pitch")
    return run


def test_reader_handles_flat_rows(tiled_client):
    frames = np.arange(3 * 4 * 5, dtype=float).reshape(3, 4, 5)
    run = _write_manual(tiled_client, frames.reshape(3, 20), [0.1, 0.2, 0.3])
    got = FocusRunReader(run, _info(shape=(4, 5))).read_new(1)
    assert [(s, p) for s, p, _ in got] == [(2, 0.2), (3, 0.3)]  # no seq_num column
    assert np.array_equal(got[0][2], frames[1])
    assert np.array_equal(got[1][2], frames[2])


def test_reader_handles_fully_flat_vector(tiled_client):
    frames = np.arange(2 * 4 * 5, dtype=float).reshape(2, 4, 5)
    run = _write_manual(tiled_client, frames.ravel(), [0.5, 0.6])
    got = FocusRunReader(run, _info(shape=(4, 5))).read_new(0)
    assert [s for s, _, _ in got] == [1, 2]
    assert np.array_equal(got[1][2], frames[1])


def test_reader_waits_for_lagging_motor_column(tiled_client):
    run = _write_manual(tiled_client, np.zeros((3, 4, 5)), [0.1, 0.2])
    got = FocusRunReader(run, _info(shape=(4, 5))).read_new(0)
    assert len(got) == 2  # only rows present in both columns


def test_reader_rejects_wrong_frame_size(tiled_client):
    run = _write_manual(tiled_client, np.zeros((2, 4, 6)), [0.1, 0.2])
    with pytest.raises(ValueError):
        FocusRunReader(run, _info(shape=(4, 5))).read_new(0)


def test_reader_missing_stream_raises_keyerror(tiled_client):
    run = tiled_client.create_container(str(uuid.uuid4()))
    with pytest.raises(KeyError):
        FocusRunReader(run, _info()).read_new(0)


def _write_scrambled(client, frames, seq, positions):
    run = client.create_container(str(uuid.uuid4()))
    primary = run.create_container("primary")
    primary.write_array(np.asarray(frames, dtype=float), key="marana_image")
    primary.write_array(np.asarray(positions, dtype=float), key="sm3_pitch")
    primary.write_array(np.asarray(seq), key="seq_num")
    return run


def test_reader_pairs_by_seq_num_when_table_is_scrambled(tiled_client):
    n = 6
    frames = np.stack([np.full((4, 5), float(i + 1)) for i in range(n)])  # frame i = seq i+1
    order = [2, 4, 6, 1, 3, 5]  # storage order: evens then odds
    pitch = {s: s * 0.1 for s in range(1, n + 1)}
    run = _write_scrambled(tiled_client, frames, order, [pitch[s] for s in order])
    reader = FocusRunReader(run, _info(shape=(4, 5)))
    got = reader.read_new(0)
    assert [s for s, _, _ in got] == [1, 2, 3, 4, 5, 6]
    for s, p, f in got:
        assert p == pytest.approx(pitch[s])
        assert np.all(f == s)
    tail = reader.read_new(4)
    assert [(s, round(p, 3)) for s, p, _ in tail] == [(5, 0.5), (6, 0.6)]
    assert np.all(tail[0][2] == 5)


def test_reader_withholds_frame_until_seq_in_table(tiled_client):
    frames = np.stack([np.full((4, 5), float(i + 1)) for i in range(4)])
    # frame 3 (seq 3) is in the array but its table row has not landed yet
    run = _write_scrambled(tiled_client, frames, [2, 1, 4], [0.2, 0.1, 0.4])
    got = FocusRunReader(run, _info(shape=(4, 5))).read_new(0)
    assert [(s, round(p, 1)) for s, p, _ in got] == [(1, 0.1), (2, 0.2)]  # stops at gap
    assert FocusRunReader(run, _info(shape=(4, 5))).read_new(2) == []


def test_reader_without_seq_num_column_is_positional(tiled_client):
    frames = np.stack([np.full((4, 5), float(i + 1)) for i in range(3)])
    run = _write_manual(tiled_client, frames, [0.3, 0.2, 0.1])
    got = FocusRunReader(run, _info(shape=(4, 5))).read_new(0)
    assert [(s, round(p, 1)) for s, p, _ in got] == [(1, 0.3), (2, 0.2), (3, 0.1)]
