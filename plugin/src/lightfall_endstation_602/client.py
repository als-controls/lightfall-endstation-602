"""Qt-side client for the external rixs-live analysis service."""

from __future__ import annotations

from loguru import logger
from qtpy.QtCore import QObject, Signal

from lightfall_endstation_602 import contract as c


class RixsServiceClient(QObject):
    frameScored = Signal(dict)
    scanComplete = Signal(dict)
    statusChanged = Signal(dict)
    errorReceived = Signal(dict)

    def __init__(self, ipc=None, parent=None) -> None:
        super().__init__(parent)
        if ipc is None:
            try:
                from lightfall.ipc.service import get_ipc_service

                ipc = get_ipc_service()
            except Exception:
                ipc = None
        self._ipc = ipc
        self.last_complete: dict | None = None
        if self._ipc is not None:
            self._ipc.subscribe(c.SUBJ_FRAME, self._on_frame)
            self._ipc.subscribe(c.SUBJ_COMPLETE, self._on_complete)
            self._ipc.subscribe(c.SUBJ_STATUS, self._on_status)
            self._ipc.subscribe(c.SUBJ_ERROR, self._on_error)

    # IPCService marshals callbacks to the Qt main thread (main_thread=True default)
    def _on_frame(self, subject, data, reply):
        self.frameScored.emit(data)

    def _on_complete(self, subject, data, reply):
        self.last_complete = data
        self.scanComplete.emit(data)

    def _on_status(self, subject, data, reply):
        self.statusChanged.emit(data)

    def _on_error(self, subject, data, reply):
        self.errorReceived.emit(data)

    def _request(self, subject: str, payload: dict, timeout_ms: int):
        """Blocking request; returns None on no IPC, timeout or any exception."""
        if self._ipc is None:
            return None
        try:
            return self._ipc.request(subject, payload, timeout_ms=timeout_ms)
        except Exception as ex:
            logger.debug(f"rixs request {subject} failed: {ex!r}")
            return None

    def discover(self, timeout_ms: int = 2000) -> dict | None:
        return self._request(c.SUBJ_DISCOVER, {}, timeout_ms)

    def get_result(self, run_uid: str, timeout_ms: int = 5000) -> dict | None:
        reply = self._request(c.SUBJ_RESULT, {"run_uid": run_uid}, timeout_ms)
        if isinstance(reply, dict) and "error" in reply:
            logger.debug(f"rixs result for {run_uid}: {reply['error']}")
            return None
        return reply

    def bind_run(self, run_uid: str, tiled_url: str = "", tiled_api_key=None,
                 lightfall_prefix: str = "") -> None:
        if self._ipc is None:
            return
        self._ipc.publish(c.SUBJ_BIND, {
            "run_uid": run_uid,
            "tiled_url": tiled_url,
            "tiled_api_key": tiled_api_key,
            "lightfall_prefix": lightfall_prefix,
            "contract_version": c.CONTRACT_VERSION,
        })

    def run_stop(self, run_uid: str) -> None:
        if self._ipc is None:
            return
        self._ipc.publish(c.SUBJ_STOP, {"run_uid": run_uid})
