"""PVA frame source: detector frames from an areaDetector PVA plugin (NTNDArray).

A real Marana writes its image to an external HDF5 file. Tiled can only serve that
file once it has been copied off the IOC host, at the end of capture. So for a run
whose start doc names a ``pva_channel`` (the IOC's ``Pva1`` plugin), rixs-live
takes the pixels from that channel instead. The event rows (``seq_num`` and the
motors) still come from Tiled, where scalars land as each event is written.

Row -> frame: each row carries ``frame_counter_field`` (the cam's
``ArrayCounter_RBV`` at read time). The driver stamps each NDArray with
``uniqueId`` = that counter (ADDriver convention, ADAndor3 included), and the PVA
plugin posts it with blocking callbacks before acquisition completes. So a row's
frame is the buffered NTNDArray whose ``uniqueId`` equals the row's counter.

Monitors are kept per channel for the life of the service (``PvaConfig.channels``
start them at service start), so frames that arrive while a bind is still looking
the run up in Tiled are already buffered.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Callable

import numpy as np
from loguru import logger

from rixs_live.contract import RixsRunInfo


def decode_ntndarray(value) -> tuple[int, np.ndarray, float]:
    """``(uniqueId, frame (h, w), epoch seconds)`` of a mono 2-D NTNDArray ``Value``.

    ``dimension[0]`` is the fastest-varying axis (x, the width), so the frame is
    ``(dimension[1].size, dimension[0].size)``. Compressed arrays and colour frames
    are refused (the Marana is mono; set the Pva1 plugin's compression off).
    """
    try:
        codec = value["codec"]["name"]
    except (KeyError, TypeError):
        codec = ""
    if codec:
        raise ValueError(f"compressed NTNDArray (codec {codec!r}) is not supported; "
                         f"turn the PVA plugin's compression off")
    dims = [int(d["size"]) for d in value["dimension"]]
    if len(dims) != 2:
        raise ValueError(f"expected a mono 2-D NTNDArray, got dimensions {dims}")
    data = np.asarray(value["value"])
    if data.size != dims[0] * dims[1]:
        raise ValueError(f"NTNDArray has {data.size} values for dimensions {dims}")
    frame = data.reshape(dims[1], dims[0])
    ts = value["timeStamp"]
    stamp = float(ts["secondsPastEpoch"]) + float(ts["nanoseconds"]) * 1e-9
    return int(value["uniqueId"]), frame, stamp


def make_pva_context(addr_list: str = "", auto_addr_list: bool | None = None):
    """A p4p client Context; ``addr_list``/``auto_addr_list`` override the environment.

    ``nt=False``: callbacks get the raw NTNDArray ``Value`` (p4p's default unwrapping
    drops ``uniqueId``, which the row join needs).
    """
    from p4p.client.thread import Context

    conf = {}
    if addr_list:
        conf["EPICS_PVA_ADDR_LIST"] = addr_list
    if auto_addr_list is not None:
        conf["EPICS_PVA_AUTO_ADDR_LIST"] = "YES" if auto_addr_list else "NO"
    return Context("pva", conf=conf or None, useenv=True, nt=False)


class PvaFrameBuffer:
    """The last ``capacity`` frames of one NTNDArray channel, keyed by ``uniqueId``.

    ``context`` is anything with ``monitor(name, cb, notify_disconnect=True)``
    returning a subscription with ``close()`` (p4p's thread Context). The callback
    runs on p4p's worker thread; reads are lock-protected.
    """

    def __init__(self, channel: str, context, *, capacity: int = 16,
                 decode: Callable[[Any], tuple[int, np.ndarray, float]] = decode_ntndarray):
        self.channel = channel
        self._ctx = context
        self._capacity = max(int(capacity), 1)
        self._decode = decode
        self._frames: OrderedDict[int, tuple[np.ndarray, float]] = OrderedDict()
        self._lock = threading.Lock()
        self._sub = None
        self.connected = False
        self.last_error: str | None = None

    def start(self) -> None:
        if self._sub is None:
            self._sub = self._ctx.monitor(self.channel, self._on_update,
                                          notify_disconnect=True)
            logger.info(f"monitoring PVA frames on {self.channel}")

    def close(self) -> None:
        if self._sub is not None:
            self._sub.close()
            self._sub = None

    def _on_update(self, value) -> None:
        if isinstance(value, Exception):  # p4p Disconnected / RemoteError / Cancelled
            if self.connected:
                logger.warning(f"PVA {self.channel}: {type(value).__name__} {value}")
            self.connected = False
            self.last_error = f"{type(value).__name__}: {value}"
            return
        if not self.connected:
            logger.info(f"PVA {self.channel} connected")
        self.connected = True
        try:
            uid, frame, stamp = self._decode(value)
        except Exception as ex:  # noqa: BLE001 - one bad update must not kill the monitor
            msg = f"{type(ex).__name__}: {ex}"
            if msg != self.last_error:
                logger.warning(f"PVA {self.channel}: undecodable frame ({msg})")
            self.last_error = msg
            return
        with self._lock:
            self._frames[uid] = (frame, stamp)
            self._frames.move_to_end(uid)
            while len(self._frames) > self._capacity:
                self._frames.popitem(last=False)

    def get(self, uid: int) -> tuple[np.ndarray, float] | None:
        """``(frame, epoch seconds)`` for ``uniqueId == uid``, or None if not buffered."""
        with self._lock:
            return self._frames.get(int(uid))

    def ids(self) -> list[int]:
        with self._lock:
            return list(self._frames)


class FrameNotReceivedError(RuntimeError):
    """A run's event row names a frame the PVA channel never delivered."""


class PvaRunReader:
    """``read_new_rows`` like ``FocusRunReader``, with pixels from a ``PvaFrameBuffer``.

    Rows come from the run's ``info.image_stream`` in Tiled: ``seq_num``, every motor
    of ``info.fields`` and ``info.frame_counter_field``; the image field itself is
    never read. Rows are joined on ``seq_num`` (table columns may come back in
    storage order, not acquisition order). A row whose frame is not buffered yet
    ends the batch; once it has waited ``frame_timeout_s`` the read raises
    ``FrameNotReceivedError``. A buffered frame stamped more than ``stale_slack_s``
    before the run started is from an earlier run (the array counter was reset) and
    counts as not received.
    """

    def __init__(self, run_node, info: RixsRunInfo, buffer: PvaFrameBuffer, *,
                 run_start_time: float | None = None, frame_timeout_s: float = 20.0,
                 stale_slack_s: float = 30.0, clock: Callable[[], float] = time.monotonic):
        self._run = run_node
        self._info = info
        self._buffer = buffer
        self._run_start = run_start_time
        self._timeout = frame_timeout_s
        self._slack = stale_slack_s
        self._clock = clock
        self._waiting: tuple[int, float] | None = None  # (seq_num, since)

    def _frame(self, uid: int) -> np.ndarray | None:
        got = self._buffer.get(uid)
        if got is None:
            return None
        frame, stamp = got
        if self._run_start is not None and stamp < self._run_start - self._slack:
            return None  # same uniqueId as a frame of this run, but from before it
        h, w = self._info.frame_shape
        if frame.shape != (h, w):
            raise ValueError(f"{self._buffer.channel}: frame {uid} is {frame.shape}, "
                             f"run expects frame_shape {self._info.frame_shape}")
        return frame

    def _missing(self, seq: int, uid: int) -> None:
        now = self._clock()
        if self._waiting is None or self._waiting[0] != seq:
            self._waiting = (seq, now)
            return
        if now - self._waiting[1] >= self._timeout:
            ids = self._buffer.ids()
            have = f"buffered uniqueIds {ids[0]}..{ids[-1]}" if ids else "no frames buffered"
            state = "connected" if self._buffer.connected else (
                f"not connected ({self._buffer.last_error or 'never connected'})")
            raise FrameNotReceivedError(
                f"no frame with uniqueId {uid} (event seq_num {seq}, "
                f"{self._info.frame_counter_field}) on PVA {self._buffer.channel} after "
                f"{self._timeout:.0f} s; channel {state}, {have}")

    def read_new_rows(self, start: int) -> list[tuple[int, dict[str, float], np.ndarray]]:
        """Return ``(seq_num, {motor field: position}, frame)`` for frames ``start..``.

        Raises KeyError until the stream and its columns exist.
        """
        stream = self._run[self._info.image_stream]
        fields = self._info.fields
        nodes = [stream[f] for f in fields] + [stream[self._info.frame_counter_field]]
        try:
            seq_node = stream["seq_num"]
        except KeyError:
            seq_node = None
        m = min(int(n.shape[0]) for n in nodes)
        if seq_node is not None:
            m = min(m, int(seq_node.shape[0]))
        if m <= 0:
            return []
        # Bounded slices: a full read of a still-growing column can trip the server.
        cols = [np.asarray(n[0:m], dtype=float).ravel() for n in nodes]
        if seq_node is None:
            seqs = np.arange(1, m + 1)
        else:
            seqs = np.asarray(seq_node[0:m]).ravel()
        n_rows = min([len(seqs)] + [len(c) for c in cols])
        pos_of: dict[int, int] = {}
        for idx, sq in enumerate(seqs[:n_rows]):
            pos_of.setdefault(int(sq), idx)
        counters = cols[-1]
        out = []
        seq = start + 1
        while seq in pos_of:
            idx = pos_of[seq]
            uid = int(counters[idx])
            frame = self._frame(uid)
            if frame is None:
                self._missing(seq, uid)
                break
            out.append((seq, {f: float(c[idx]) for f, c in zip(fields, cols)}, frame))
            seq += 1
        return out
