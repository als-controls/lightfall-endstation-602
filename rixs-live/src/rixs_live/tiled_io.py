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

from rixs_live.contract import RixsRunInfo


def connect_tiled(url: str, api_key: str | None):
    from tiled.client import from_uri
    return from_uri(url, api_key=api_key)


class FocusRunReader:
    """Slices only the rows not yet seen from the run's primary stream."""

    def __init__(self, run_node, info: RixsRunInfo) -> None:
        self._run = run_node
        self._info = info

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
        """Return ``(seq_num, position, frame)`` for frames ``start..n-1``.

        Raises KeyError until the primary stream and its image node exist.
        ``n`` is the number of rows present in both the image and motor nodes,
        since the two may land in Tiled at slightly different times.
        """
        stream = self._run["primary"]
        image = stream[self._info.image_field]
        motor = stream[self._info.motor_field]
        n = min(self._n_frames(image), int(motor.shape[0]))
        if n <= start:
            return []
        frames = self._frames(image, start, n)
        positions = np.asarray(motor[start:n], dtype=float).ravel()
        try:
            seq = [int(s) for s in np.asarray(stream["seq_num"][start:n]).ravel()]
        except KeyError:
            seq = list(range(start + 1, n + 1))
        return [(seq[i], float(positions[i]), frames[i]) for i in range(n - start)]
