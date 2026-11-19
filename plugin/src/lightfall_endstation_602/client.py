"""Qt-side client for the external rixs-live analysis service.

rixs-live is an IPC-services ``ServiceHost`` ("rixs-live" under the scope of Lightfall's
``ipc_topic_prefix``). The plugin registers it with Lightfall's ``ServiceManager``
(:mod:`lightfall_endstation_602.service_plugin`); the manager's ``QtServiceClient``
opens a private session (handshake, lease pings, reconnects). This class wraps that
client: bind/stop/result are rpcs on the session, and the frame/complete/status/error
events of runs this Lightfall bound arrive on it and nowhere else. Health comes from the
service's heartbeat (``state_changed``), not from polling.
"""

from __future__ import annotations

import concurrent.futures
import threading
import time
from collections import OrderedDict

from loguru import logger
from qtpy.QtCore import QObject, Signal

from lightfall_endstation_602 import contract as c

_UP_STATES = frozenset({"up", "degraded"})
_DOWN_STATES = frozenset({"down", "incompatible", "unauthorized"})


def _invoke_in_main_thread(fn, *args) -> None:
    try:
        from lightfall.utils.threads import invoke_in_main_thread
    except Exception:  # noqa: BLE001 - headless use (tests, the rixs-live venv)
        fn(*args)
        return
    invoke_in_main_thread(fn, *args)


class RixsServiceClient(QObject):
    frameScored = Signal(dict)
    scanComplete = Signal(dict)
    statusChanged = Signal(dict)
    errorReceived = Signal(dict)
    # True/False when the service's health flips (heartbeat-driven).
    healthChanged = Signal(bool)
    # Run uid of every run.bind sent; may be emitted from the RunEngine thread.
    bindSent = Signal(str)

    # rpc timeouts: bind/stop answer at once (the work runs in the background).
    CALL_TIMEOUT_S = 5.0
    # Per-frame scores kept for plans that wait on them (qerlin_optimize), newest last.
    SCORE_CACHE_MAX = 5000
    SCORE_KEYS = ("run_uid", "seq_num", "position", "positions", "fwhm_px", "fwhm_mev",
                  "resolving_power", "score_valid", "failure_reason")
    EVENTS = (c.EVT_FRAME, c.EVT_COMPLETE, c.EVT_STATUS, c.EVT_ERROR)

    def __init__(self, service=None, manager=None, parent=None) -> None:
        """``service``: a QtServiceClient-like object (tests). Without it the client
        follows ``ServiceManager``'s ``rixs-live`` client, including re-creations."""
        super().__init__(parent)
        self._svc = None
        self.last_complete: dict | None = None
        # Run uid of the most recent run.bind (set by RixsRunBinder).
        self.last_bound_uid: str | None = None
        # Health of rixs-live, the one place the panel and the agent tools read it:
        # None until known, then whether the service is up (heartbeat).
        self.service_up: bool | None = None
        self.service_info: dict | None = None
        self.health_checked_at: float | None = None
        # (run_uid, seq_num) -> compact frame score. Written on the Qt main thread by
        # _on_frame, read from the RunEngine thread by qerlin_optimize: always under
        # the condition's lock.
        self._scores: OrderedDict[tuple[str, int], dict] = OrderedDict()
        self._scores_cond = threading.Condition()
        if service is not None:
            self.attach(service)
            return
        if manager is None:
            try:
                from lightfall.services.manager import ServiceManager

                manager = ServiceManager.get_instance()
            except Exception:  # noqa: BLE001 - no Lightfall (headless use)
                manager = None
        self._manager = manager
        if manager is not None:
            manager.client_created.connect(self._on_client_created)
            existing = manager.client(c.SERVICE_NAME)
            if existing is not None:
                self.attach(existing)

    # -- service client wiring -------------------------------------------------
    def _on_client_created(self, name: str) -> None:
        if name == c.SERVICE_NAME and self._manager is not None:
            client = self._manager.client(name)
            if client is not None:
                self.attach(client)

    def attach(self, service) -> None:
        """Follow ``service`` (a QtServiceClient): its events, health and info."""
        if service is self._svc:
            return
        self._svc = service
        for name in self.EVENTS:
            service.watch_event(name)
        service.event.connect(self._on_event)
        service.state_changed.connect(self._on_state)
        service.info_changed.connect(self._on_info)
        if hasattr(service, "restarted"):
            service.restarted.connect(self._on_restarted)
        self._on_state(getattr(service, "health", "unknown"))

    def _on_event(self, name: str, payload: dict) -> None:
        if name == c.EVT_FRAME:
            self.record_score(payload)
            self.frameScored.emit(payload)
        elif name == c.EVT_COMPLETE:
            self.last_complete = payload
            self.scanComplete.emit(payload)
        elif name == c.EVT_STATUS:
            self.statusChanged.emit(payload)
        elif name == c.EVT_ERROR:
            self.errorReceived.emit(payload)

    def _info_dict(self) -> dict | None:
        """``{app_name, app_version, dispersion_mev_per_px, ...}`` from meta.info."""
        info = getattr(self._svc, "info", None) if self._svc is not None else None
        if not isinstance(info, dict):
            return None
        summary = info.get("summary") if isinstance(info.get("summary"), dict) else {}
        return {"app_name": summary.get("app_name", info.get("name", c.SERVICE_NAME)),
                "app_version": info.get("version", ""), **summary}

    def _on_state(self, health: str) -> None:
        if health in _UP_STATES:
            self.record_health(self._info_dict() or {"app_name": c.SERVICE_NAME})
        elif health in _DOWN_STATES:
            self.record_health(None)

    def _on_info(self, _info: dict) -> None:
        if self.service_up:
            self.service_info = self._info_dict() or self.service_info

    def _on_restarted(self) -> None:
        logger.warning("rixs-live restarted: a run bound before the restart is not "
                       "analysed any more")

    # -- per-frame score cache (thread-safe) ----------------------------------
    def record_score(self, data: dict) -> None:
        """Cache a frame event payload (without its preview) by (run_uid, seq_num)."""
        if not isinstance(data, dict):
            return
        uid, seq = data.get("run_uid"), data.get("seq_num")
        if not isinstance(uid, str) or not uid or isinstance(seq, bool):
            return
        try:
            seq = int(seq)
        except (TypeError, ValueError):
            return
        entry = {k: data[k] for k in self.SCORE_KEYS if k in data}
        entry["seq_num"] = seq
        with self._scores_cond:
            self._scores[(uid, seq)] = entry
            self._scores.move_to_end((uid, seq))
            while len(self._scores) > self.SCORE_CACHE_MAX:
                self._scores.popitem(last=False)
            self._scores_cond.notify_all()

    def get_score(self, run_uid: str, seq_num: int) -> dict | None:
        """Non-blocking: the cached score of frame ``seq_num`` of ``run_uid``, else None."""
        with self._scores_cond:
            entry = self._scores.get((run_uid, int(seq_num)))
            return dict(entry) if entry is not None else None

    def wait_score(self, run_uid: str, seq_num: int, timeout_s: float) -> dict | None:
        """Blocking: wait up to ``timeout_s`` for that frame's score; None on timeout.

        For worker threads only: never call it on the RunEngine thread (plans poll
        ``get_score`` between ``bps.sleep`` calls) or on the Qt main thread (where
        the scores arrive).
        """
        key = (run_uid, int(seq_num))
        deadline = time.monotonic() + max(float(timeout_s), 0.0)
        with self._scores_cond:
            while key not in self._scores:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._scores_cond.wait(remaining)
            return dict(self._scores[key])

    def clear_scores(self, run_uid: str | None = None) -> None:
        """Forget the cached scores of ``run_uid`` (all runs when None)."""
        with self._scores_cond:
            if run_uid is None:
                self._scores.clear()
            else:
                for key in [k for k in self._scores if k[0] == run_uid]:
                    del self._scores[key]

    # -- rpcs -----------------------------------------------------------------
    def _call(self, action: str, payload: dict,
              timeout_s: float | None = None) -> concurrent.futures.Future | None:
        if self._svc is None:
            return None
        try:
            return self._svc.call(action, payload, timeout_s or self.CALL_TIMEOUT_S)
        except Exception as ex:  # noqa: BLE001 - e.g. the client is not running
            logger.debug(f"rixs {action} failed: {ex!r}")
            return None

    def _call_blocking(self, action: str, payload: dict, timeout_ms: int) -> dict | None:
        """The rpc's reply dict, or None (no client, timeout, error reply, exception)."""
        timeout_s = max(timeout_ms, 1) / 1000.0
        fut = self._call(action, payload, timeout_s)
        if fut is None:
            return None
        try:
            reply = fut.result(timeout=timeout_s + 1.0)
        except Exception as ex:  # noqa: BLE001
            logger.debug(f"rixs {action} failed: {ex!r}")
            return None
        if not isinstance(reply, dict) or reply.get("status") != "ok":
            logger.debug(f"rixs {action}: {reply!r}")
            return None
        return reply

    def _report_failure(self, action: str, run_uid: str, fut) -> None:
        """Turn a failed bind/stop rpc into an errorReceived (on the GUI thread)."""
        def done(f: concurrent.futures.Future) -> None:
            try:
                reply = f.result()
            except Exception as ex:  # noqa: BLE001
                reply = {"status": "error", "message": repr(ex)}
            if isinstance(reply, dict) and reply.get("status") == "ok":
                return
            message = reply.get("message", reply) if isinstance(reply, dict) else reply
            logger.warning(f"rixs-live {action} for {run_uid} failed: {message}")
            _invoke_in_main_thread(self.errorReceived.emit, {
                "run_uid": run_uid, "error": f"{action} failed: {message}"})

        fut.add_done_callback(done)

    def discover(self, timeout_ms: int = 2000) -> dict | None:
        """The service's info (``app_name``, ``app_version``, ``dispersion_mev_per_px``)
        while it is up, else None. No network round trip: health is heartbeat-driven."""
        return self.service_info if self.service_up else None

    # -- health ---------------------------------------------------------------
    def record_health(self, reply: dict | None) -> None:
        """GUI thread: store the service's health; emits healthChanged when it flips."""
        up = bool(reply)
        changed = up != self.service_up
        self.service_up = up
        self.service_info = reply if up else None
        self.health_checked_at = time.time()
        if changed:
            if not up:
                logger.warning("rixs-live is not responding")
            self.healthChanged.emit(up)

    def start_health_monitor(self, interval_ms: int | None = None) -> None:
        """Kept for callers: health follows the service heartbeat; re-read it now."""
        self.check_health_async()

    def stop_health_monitor(self) -> None:
        return None

    def check_health_async(self) -> None:
        """GUI thread: re-read the service client's current health (no network)."""
        if self._svc is not None:
            self._on_state(getattr(self._svc, "health", "unknown"))
        elif self.service_up is None:
            self.record_health(None)

    def get_result(self, run_uid: str, timeout_ms: int = 5000) -> dict | None:
        """The stored focus result for ``run_uid``.

        Returns the complete payload, or the service's ``{"pending": True, run_uid,
        frames_done, total}`` reply while the run is still bound or being drained
        (callers check ``.get("pending")``). None for no reply, timeout, an error or an
        unknown run. Blocks: never call it on the GUI thread.
        """
        reply = self._call_blocking(c.RPC_RESULT, {"run_uid": run_uid}, timeout_ms)
        if reply is None:
            return None
        if reply.get("pending"):
            return {k: reply[k] for k in ("pending", "run_uid", "frames_done", "total")
                    if k in reply}
        return reply.get("result")

    def bind_run(self, run_uid: str, tiled_url: str = "",
                 tiled_proxy: str | None = None, **_ignored) -> None:
        """Ask rixs-live to analyse ``run_uid`` (non-blocking ``run.bind`` rpc).

        ``tiled_proxy`` is the proxy Lightfall uses for ``tiled_url`` (or None), so the
        service reaches the same Tiled without configuring the proxy twice. The Tiled
        key is never sent: rixs-live gets it from Lightfall's trust handshake.
        """
        payload = {"run_uid": run_uid, "tiled_url": tiled_url,
                   "tiled_proxy": tiled_proxy or None,
                   "contract_version": c.CONTRACT_VERSION}
        fut = self._call(c.RPC_BIND, payload)
        if fut is not None:
            self._report_failure(c.RPC_BIND, run_uid, fut)
        else:
            logger.warning(f"run {run_uid} not sent to rixs-live: service client missing")
        # Listeners compare against service_up to report a bind to a service known down.
        self.bindSent.emit(run_uid)

    def run_stop(self, run_uid: str, exit_status: str | None = None,
                 num_events: dict | None = None) -> None:
        """``run.stop`` rpc (non-blocking); ``exit_status``/``num_events`` from the stop doc."""
        payload: dict = {"run_uid": run_uid}
        if exit_status is not None:
            payload["exit_status"] = exit_status
        if isinstance(num_events, dict):
            payload["num_events"] = dict(num_events)
        fut = self._call(c.RPC_STOP, payload)
        if fut is not None:
            self._report_failure(c.RPC_STOP, run_uid, fut)
