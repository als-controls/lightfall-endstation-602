"""Qt-side client for the external rixs-live analysis service."""

from __future__ import annotations

import time

from loguru import logger
from qtpy.QtCore import QObject, QTimer, Signal

from lightfall_endstation_602 import contract as c


class RixsServiceClient(QObject):
    frameScored = Signal(dict)
    scanComplete = Signal(dict)
    statusChanged = Signal(dict)
    errorReceived = Signal(dict)
    # True/False when the periodic discover() check flips the service state.
    healthChanged = Signal(bool)
    # Run uid of every rixs.run.bind sent; may be emitted from the RunEngine thread.
    bindSent = Signal(str)

    HEALTH_INTERVAL_MS = 10_000
    HEALTH_TIMEOUT_MS = 2000

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
        # Run uid of the most recent rixs.run.bind (set by RixsRunBinder).
        self.last_bound_uid: str | None = None
        # Health of rixs-live, the one place the panel and the agent tools read it:
        # None until the first check, then the result of the latest discover().
        self.service_up: bool | None = None
        self.service_info: dict | None = None
        self.health_checked_at: float | None = None
        self._health_timer: QTimer | None = None
        self._health_pool = None
        self._health_inflight = False
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

    # -- health ---------------------------------------------------------------
    def record_health(self, reply: dict | None) -> None:
        """GUI thread: store a discover() result; emits healthChanged when it flips."""
        up = bool(reply)
        changed = up != self.service_up
        self.service_up = up
        self.service_info = reply if up else None
        self.health_checked_at = time.time()
        if changed:
            if not up:
                logger.warning("rixs-live is not responding to discovery")
            self.healthChanged.emit(up)

    def start_health_monitor(self, interval_ms: int | None = None) -> None:
        """GUI thread: check health now and every ``interval_ms`` (idempotent)."""
        if self._health_timer is not None:
            return
        self._health_timer = QTimer(self)
        self._health_timer.setInterval(interval_ms or self.HEALTH_INTERVAL_MS)
        self._health_timer.timeout.connect(self.check_health_async)
        self._health_timer.start()
        self.check_health_async()

    def stop_health_monitor(self) -> None:
        if self._health_timer is not None:
            self._health_timer.stop()
            self._health_timer = None

    def check_health_async(self) -> None:
        """GUI thread: run discover() in a worker; the result is applied on the GUI thread.

        discover() blocks for up to HEALTH_TIMEOUT_MS, so it must never run here.
        """
        if self._health_inflight:
            return
        from lightfall.utils.threads import ManagedThreadPool
        if self._health_pool is None:
            self._health_pool = ManagedThreadPool(max_workers=1, name="rixs-health")
        self._health_inflight = True
        try:
            self._health_pool.submit(self._health_worker)
        except RuntimeError:  # pool shut down (application quitting)
            self._health_inflight = False

    def _health_worker(self) -> None:
        from lightfall.utils.threads import invoke_in_main_thread
        reply = None
        try:
            reply = self.discover(self.HEALTH_TIMEOUT_MS)
        finally:
            invoke_in_main_thread(self._apply_health, reply)

    def _apply_health(self, reply: dict | None) -> None:
        self._health_inflight = False
        self.record_health(reply)

    def get_result(self, run_uid: str, timeout_ms: int = 5000) -> dict | None:
        """The stored focus result for ``run_uid``.

        Returns the complete payload, or the service's ``{"pending": True, run_uid,
        frames_done, total}`` reply while the run is still bound or being drained
        (callers check ``.get("pending")``). None for no reply, timeout or an
        ``{"error": ...}`` reply (unknown run).
        """
        reply = self._request(c.SUBJ_RESULT, {"run_uid": run_uid}, timeout_ms)
        if isinstance(reply, dict) and "error" in reply:
            logger.debug(f"rixs result for {run_uid}: {reply['error']}")
            return None
        return reply

    def bind_run(self, run_uid: str, tiled_url: str = "",
                 lightfall_prefix: str = "", tiled_proxy: str | None = None) -> None:
        """Publish rixs.run.bind. Never carries a Tiled API key (public subject).

        ``tiled_proxy`` is the proxy Lightfall uses for ``tiled_url`` (or None), so
        the service reaches the same Tiled without configuring the proxy twice.
        """
        if self._ipc is None:
            return
        self._ipc.publish(c.SUBJ_BIND, {
            "run_uid": run_uid,
            "tiled_url": tiled_url,
            "tiled_proxy": tiled_proxy or None,
            "lightfall_prefix": lightfall_prefix,
            "contract_version": c.CONTRACT_VERSION,
        })
        # A publish has no ack: listeners compare against service_up to report a
        # bind that went to a service known to be down.
        self.bindSent.emit(run_uid)

    def run_stop(self, run_uid: str, exit_status: str | None = None,
                 num_events: dict | None = None) -> None:
        """Publish rixs.run.stop; ``exit_status``/``num_events`` come from the stop doc."""
        if self._ipc is None:
            return
        payload: dict = {"run_uid": run_uid}
        if exit_status is not None:
            payload["exit_status"] = exit_status
        if isinstance(num_events, dict):
            payload["num_events"] = dict(num_events)
        self._ipc.publish(c.SUBJ_STOP, payload)
