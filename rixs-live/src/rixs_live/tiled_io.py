"""Tiled I/O: connect, and read new (seq_num, position, frame) rows of a focus scan.

Pinned in tests/test_tiled_io.py against a run written by a real TiledWriter
(tiled 0.2.x, bluesky-tiled-plugins 2.0.x): the primary stream is a container
of array nodes; the image is stacked ``(n, H, W)`` and each scalar (motor,
``seq_num``) is a 1-D ``(n,)`` array node. Other writers may store frames
flattened, ``(n, H*W)`` or ``(n*H*W,)``; both are accepted.
"""
from __future__ import annotations

import math

import numpy as np
from loguru import logger

from rixs_live.contract import RixsRunInfo


def connect_tiled(url: str, api_key: str | None):
    from tiled.client import from_uri
    return from_uri(url, api_key=api_key)


class FocusRunReader:
    """Slices only the rows not yet seen from the run's primary stream."""

    def __init__(self, run_node, info: RixsRunInfo) -> None:
        self._run = run_node
        self._info = info
        self._warned: set = set()

    def _n_frames(self, image_node) -> int:
        shape = tuple(image_node.shape)
        h, w = self._info.frame_shape
        if len(shape) == 1:
            return shape[0] // (h * w)
        return shape[0]

    def _frames(self, image_node, start: int, stop: int) -> np.ndarray:
        h, w = self._info.frame_shape
        shape = tuple(image_node.shape)
        if len(shape) == 1:
            raw = image_node[start * h * w: stop * h * w]
        else:
            raw = image_node[start:stop]
        raw = np.asarray(raw)
        per = math.prod(raw.shape[1:]) if raw.ndim > 1 else h * w
        if per != h * w:
            raise ValueError(
                f"{self._info.image_field}: {raw.shape[1:]} frames do not match "
                f"frame_shape {self._info.frame_shape}")
        return raw.reshape(stop - start, h, w)

    def read_new(self, start: int) -> list[tuple[int, float, np.ndarray]]:
        """Return ``(seq_num, position, frame)`` for frames ``start..``.

        Raises KeyError until the primary stream and its image node exist.

        The image array is stored in acquisition order (row i is the frame with
        ``seq_num == i + 1``), but the table columns (``seq_num``, the motor) can
        come back in physical storage order (PostgreSQL), each row self-consistent.
        So the position of frame i is looked up by ``seq_num`` rather than by
        index. Frames whose seq_num is not in the table yet are not returned
        (the caller polls again from the count it has consumed); the first such
        gap ends the batch. Without a ``seq_num`` column rows are paired by
        position, limited to rows present in both image and motor nodes.
        """
        stream = self._run["primary"]
        image = stream[self._info.image_field]
        motor = stream[self._info.motor_field]
        n_img = self._n_frames(image)
        try:
            seq_node = stream["seq_num"]
        except KeyError:
            seq_node = None
        if seq_node is None:
            n = min(n_img, int(motor.shape[0]))
            if n <= start:
                return []
            frames = self._frames(image, start, n)
            positions = np.asarray(motor[start:n], dtype=float).ravel()
            return [(start + i + 1, float(positions[i]), frames[i])
                    for i in range(n - start)]

        # Bounded slices (not [:]): a full read of a column that is still growing
        # trips the server's expected_shape check (HTTP 500) in the queued-writer case.
        m = min(int(seq_node.shape[0]), int(motor.shape[0]))
        if m == 0:
            return []
        seqs = np.asarray(seq_node[0:m]).ravel()
        positions = np.asarray(motor[0:m], dtype=float).ravel()
        pos_of: dict[int, int] = {}
        for idx, sq in enumerate(seqs[: len(positions)]):
            sq = int(sq)
            if sq in pos_of:
                self._warn_once(("dup", sq), f"duplicate seq_num {sq} in table; keeping first")
                continue
            pos_of[sq] = idx
        stop = start
        while stop < n_img and (stop + 1) in pos_of:
            stop += 1
        if stop <= start:
            return []
        frames = self._frames(image, start, stop)
        return [(i + 1, float(positions[pos_of[i + 1]]), frames[i - start])
                for i in range(start, stop)]

    def _warn_once(self, key, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            logger.warning(msg)
