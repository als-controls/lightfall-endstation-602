"""PVA frame source: NTNDArray decoding, the per-channel buffer and the row join."""
import time

import numpy as np
import pytest
from p4p.nt import NTNDArray

from rixs_live.contract import RixsRunInfo
from rixs_live.pva_io import (
    FrameNotReceivedError,
    PvaFrameBuffer,
    PvaRunReader,
    decode_ntndarray,
)

CHANNEL = "BL602:marana01:Pva1:Image"
COUNTER = "marana_array_counter"
SHAPE = (6, 8)  # (h, w): deliberately not square


def ntnd(frame, uid, stamp=1_700_000_000.25):
    v = NTNDArray().wrap(np.asarray(frame))
    v["uniqueId"] = uid
    v["timeStamp.secondsPastEpoch"] = int(stamp)
    v["timeStamp.nanoseconds"] = int(round((stamp % 1) * 1e9))
    return v


def frame_of(k, shape=SHAPE):
    return np.full(shape, k, dtype=np.uint16)


# ------------------------------------------------------------------------- decode


def test_decode_keeps_height_width_and_unique_id():
    frame = np.arange(SHAPE[0] * SHAPE[1], dtype=np.uint16).reshape(SHAPE)
    uid, got, stamp = decode_ntndarray(ntnd(frame, 42))
    assert uid == 42
    assert got.shape == SHAPE
    assert np.array_equal(got, frame)  # rows are rows: dimension[0] is x (width)
    assert stamp == pytest.approx(1_700_000_000.25)


def test_decode_refuses_compressed_and_colour():
    v = ntnd(frame_of(1), 1)
    v["codec.name"] = "blosc"
    with pytest.raises(ValueError, match="compress"):
        decode_ntndarray(v)
    with pytest.raises(ValueError, match="mono 2-D"):
        decode_ntndarray(ntnd(np.zeros((3, 4, 5), dtype=np.uint8), 1))


# ------------------------------------------------------------------------- buffer


class FakeSub:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeContext:
    def __init__(self):
        self.monitors = {}  # name -> callback

    def monitor(self, name, cb, notify_disconnect=False):
        assert notify_disconnect, "disconnects must reach the buffer"
        self.monitors[name] = cb
        self.sub = FakeSub()
        return self.sub

    def post(self, name, value):
        self.monitors[name](value)


def _buffer(capacity=4):
    ctx = FakeContext()
    buf = PvaFrameBuffer(CHANNEL, ctx, capacity=capacity)
    buf.start()
    return ctx, buf


def test_buffer_keeps_last_frames_by_unique_id():
    ctx, buf = _buffer(capacity=3)
    for uid in range(1, 6):
        ctx.post(CHANNEL, ntnd(frame_of(uid), uid))
    assert buf.ids() == [3, 4, 5]
    frame, _ = buf.get(4)
    assert frame[0, 0] == 4
    assert buf.get(1) is None
    assert buf.connected


def test_buffer_survives_disconnects_and_bad_frames():
    ctx, buf = _buffer()

    class Disconnected(Exception):
        pass

    ctx.post(CHANNEL, ntnd(frame_of(1), 1))
    ctx.post(CHANNEL, Disconnected("gone"))
    assert not buf.connected and "gone" in buf.last_error
    bad = ntnd(frame_of(2), 2)
    bad["codec.name"] = "lz4"
    ctx.post(CHANNEL, bad)  # logged, not raised into p4p's worker
    assert buf.ids() == [1] and "lz4" in buf.last_error
    buf.close()
    assert ctx.sub.closed


# ------------------------------------------------------------------------- reader


class Column:
    def __init__(self, values):
        self.values = np.asarray(values)
        self.shape = self.values.shape

    def __getitem__(self, sl):
        return self.values[sl]


class Stream(dict):
    """Image stream with seq_num, motor and counter columns, and no image field."""

    def __init__(self, positions, counters, order=None):
        n = len(positions)
        cols = {"seq_num": np.arange(1, n + 1), "sm3_pitch": np.asarray(positions, float),
                COUNTER: np.asarray(counters)}
        if order is not None:  # storage order (PostgreSQL), each row self-consistent
            idx = np.asarray(order) - 1
            cols = {k: v[idx] for k, v in cols.items()}
        super().__init__({k: Column(v) for k, v in cols.items()})


class Run:
    def __init__(self, stream):
        self.stream = stream

    def __getitem__(self, key):
        if key != "primary" or self.stream is None:
            raise KeyError(key)
        return self.stream


def _info(**over):
    kw = dict(run_uid="r1", image_field="marana_image", motor_field="sm3_pitch",
              frame_shape=SHAPE, mono_energy_ev=None, pva_channel=CHANNEL,
              frame_counter_field=COUNTER)
    kw.update(over)
    return RixsRunInfo(**kw)


def test_reader_joins_rows_to_frames_on_the_counter():
    ctx, buf = _buffer(capacity=16)
    # A warmup frame (uid 10) and then one frame per point; table in storage order.
    for uid in (10, 11, 12, 13):
        ctx.post(CHANNEL, ntnd(frame_of(uid), uid))
    run = Run(Stream([1.0, 2.0, 3.0], [11, 12, 13], order=[2, 3, 1]))
    reader = PvaRunReader(run, _info(), buf)
    rows = reader.read_new_rows(0)
    assert [(s, p["sm3_pitch"], int(f[0, 0])) for s, p, f in rows] == [
        (1, 1.0, 11), (2, 2.0, 12), (3, 3.0, 13)]
    assert [s for s, _, _ in reader.read_new_rows(2)] == [3]
    assert reader.read_new_rows(3) == []


def test_reader_keyerror_until_the_stream_exists():
    _, buf = _buffer()
    with pytest.raises(KeyError):
        PvaRunReader(Run(None), _info(), buf).read_new_rows(0)


def test_reader_waits_for_a_late_frame_then_raises():
    ctx, buf = _buffer()
    ctx.post(CHANNEL, ntnd(frame_of(5), 5))
    now = [100.0]
    run = Run(Stream([1.0, 2.0], [5, 6]))
    reader = PvaRunReader(run, _info(), buf, frame_timeout_s=10.0, clock=lambda: now[0])
    assert [s for s, _, _ in reader.read_new_rows(0)] == [1]  # row 2's frame not yet here
    assert reader.read_new_rows(1) == []
    now[0] = 105.0
    assert reader.read_new_rows(1) == []
    ctx.post(CHANNEL, ntnd(frame_of(6), 6))  # arrives in time
    assert [s for s, _, _ in reader.read_new_rows(1)] == [2]

    run.stream = Stream([1.0, 2.0, 3.0], [5, 6, 99])
    assert reader.read_new_rows(2) == []
    now[0] = 120.0
    with pytest.raises(FrameNotReceivedError, match=r"uniqueId 99 .*seq_num 3.*5\.\.6"):
        reader.read_new_rows(2)


def test_reader_ignores_a_stale_frame_with_a_reused_counter():
    """IOC restarted: ArrayCounter starts again and an old frame has this run's id."""
    ctx, buf = _buffer()
    ctx.post(CHANNEL, ntnd(frame_of(1), 1, stamp=1_000.0))
    reader = PvaRunReader(Run(Stream([1.0], [1])), _info(), buf,
                          run_start_time=2_000.0, stale_slack_s=30.0)
    assert reader.read_new_rows(0) == []
    ctx.post(CHANNEL, ntnd(frame_of(1), 1, stamp=2_001.0))
    assert [s for s, _, _ in reader.read_new_rows(0)] == [1]


def test_reader_rejects_a_frame_of_the_wrong_shape():
    ctx, buf = _buffer()
    ctx.post(CHANNEL, ntnd(frame_of(1, shape=(4, 4)), 1))
    with pytest.raises(ValueError, match="frame_shape"):
        PvaRunReader(Run(Stream([1.0], [1])), _info(), buf).read_new_rows(0)


# ------------------------------------------------------------------- real p4p wire


def test_real_pva_server_round_trip():
    """A p4p server posting NTNDArrays reaches the buffer through a real client Context."""
    from p4p.client.thread import Context
    from p4p.server import Server
    from p4p.server.thread import SharedPV

    pv = SharedPV(initial=ntnd(frame_of(0), 0))  # raw Values: uniqueId is ours to set
    with Server(providers=[{CHANNEL: pv}], isolate=True) as server:
        ctx = Context("pva", conf=server.conf(), useenv=False, nt=False)
        try:
            buf = PvaFrameBuffer(CHANNEL, ctx, capacity=8)
            buf.start()
            deadline = time.monotonic() + 10
            while not buf.connected and time.monotonic() < deadline:
                time.sleep(0.05)
            assert buf.connected, buf.last_error
            for uid in (7, 8):
                pv.post(ntnd(frame_of(uid), uid))
            while buf.get(8) is None and time.monotonic() < deadline:
                time.sleep(0.05)
            frame, _ = buf.get(8)
            assert frame.shape == SHAPE and frame[0, 0] == 8
            assert buf.get(7) is not None
            buf.close()
        finally:
            ctx.close()
