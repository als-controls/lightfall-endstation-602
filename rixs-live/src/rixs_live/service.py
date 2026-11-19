"""rixs-live event loop: bind -> poll Tiled -> score + emit each frame; stop -> fit + complete.

rixs-live is a :class:`~rixs_live.svc.host.ServiceHost` ("rixs-live" under the scope of
Lightfall's topic prefix, e.g. ``als.602``). Lightfall opens a private session and
calls the ``run.bind`` / ``run.stop`` / ``result.get`` rpcs; each run's frame, status,
error and complete events go only to the session that bound it. Nothing but discovery,
``meta.info`` and the heartbeat is public.

Frames come from the run's image field in Tiled or, when the start doc names a
``pva_channel`` (the real Marana), from that PVA channel, joined to Tiled's event
rows on the detector's array counter (see :mod:`rixs_live.pva_io`).
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from importlib.metadata import PackageNotFoundError, version

from loguru import logger

from rixs_live.config import AppConfig
from rixs_live.contract import (
    CONTRACT_VERSION,
    EVT_COMPLETE,
    EVT_ERROR,
    EVT_FRAME,
    EVT_STATUS,
    RPC_BIND,
    RPC_RESULT,
    RPC_STOP,
    SERVICE_CONTRACT,
    SERVICE_NAME,
    parse_start_rixs,
)
from rixs_live.focus import fit_focus_curve, make_preview, score_frame
from rixs_live.nats_client import NatsClient
from rixs_live.pva_io import PvaFrameBuffer, PvaRunReader, make_pva_context
from rixs_live.svc.host import ServiceError, ServiceHost, allow_apps
from rixs_live.tiled_io import FocusRunReader, connect_tiled, env_proxy, redact_proxy

# Sessions are opened by Lightfall (app_name "lightfall"). app_name is self-reported,
# so this only keeps other well-behaved apps out; per-connection isolation comes from
# the broker's auth callout.
TRUSTED_APPS = ("lightfall",)


def _pkg_version() -> str:
    try:
        return version("rixs-live")
    except PackageNotFoundError:
        return ""
POINT_KEYS = ("position", "fwhm_px", "fwhm_mev", "resolving_power", "score_valid")
MULTI_MOTOR_NO_FIT = "multi-motor run: no 1D fit"


class RixsLiveService:
    def __init__(self, config: AppConfig, *, nats_client=None, tiled_connect=None,
                 pva_context_factory=None, host=None, emit=None) -> None:
        """``host``/``emit`` are for tests: a stand-in ServiceHost, and an async
        ``emit(session, name, payload)`` replacing delivery through the host."""
        self._config = config
        self._nats = nats_client or NatsClient(config.nats)
        self._host = host or self._make_host()
        self._emit_fn = emit
        self._tasks: set[asyncio.Task] = set()
        self._tiled_connect = tiled_connect or connect_tiled
        self._pva_context_factory = pva_context_factory or (
            lambda: make_pva_context(config.pva.addr_list, config.pva.auto_addr_list))
        self._pva_context = None
        self._pva_buffers: dict[str, PvaFrameBuffer] = {}
        self._warned_bind_key = False
        self._poll_task: asyncio.Task | None = None
        # dict: run_uid, session, reader, info, total, next, frames (per-frame payloads),
        # stop (Event)
        self._current: dict | None = None
        self.results: OrderedDict[str, dict] = OrderedDict()
        self._binding: set[str] = set()  # run uids whose bind is still in progress
        self._pending_stops: dict[str, dict] = {}  # stops that arrived during their bind

    @property
    def busy(self) -> bool:
        return self._current is not None

    @property
    def scope(self) -> str:
        return self._config.nats.scope or self._config.nats.lightfall_prefix

    def _make_host(self) -> ServiceHost:
        host = ServiceHost(SERVICE_NAME, self.scope, contract=SERVICE_CONTRACT,
                           version=self._config.nats.app_version or _pkg_version(),
                           single_instance=True, trust=allow_apps(TRUSTED_APPS))
        host.set_state(summary={
            "app_name": self._config.nats.app_name,
            "dispersion_mev_per_px": self._config.dispersion_mev_per_px,
            "rixs_contract_version": CONTRACT_VERSION,
        })
        host.rpc(RPC_BIND)(self._rpc_bind)
        host.rpc(RPC_STOP)(self._rpc_stop)
        host.rpc(RPC_RESULT)(self._rpc_result)
        return host

    # -- rpcs (each returns at once; the analysis runs as a task) ------------------------

    @staticmethod
    def _run_uid(payload: dict) -> str:
        uid = payload.get("run_uid")
        if not isinstance(uid, str) or not uid:
            raise ServiceError("bad_request", "run_uid must be a non-empty string")
        return uid

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _rpc_bind(self, session, payload: dict) -> dict:
        uid = self._run_uid(payload)
        self._binding.add(uid)  # now, so a stop rpc that overtakes the bind task waits
        self._spawn(self._handle_bind(payload, session))
        return {"accepted": True}

    async def _rpc_stop(self, session, payload: dict) -> dict:
        self._run_uid(payload)
        self._spawn(self._handle_stop(payload, session))
        return {"accepted": True}

    async def _rpc_result(self, session, payload: dict) -> dict:
        uid = self._run_uid(payload)
        if uid not in self.results:
            pending = self._pending_reply(uid)
            if pending is not None:
                return pending
        return {"result": self.results.get(uid)}

    async def _emit(self, session, name: str, payload: dict) -> None:
        """Deliver ``payload`` to ``session`` only (dropped if that session has ended)."""
        if self._emit_fn is not None:
            await self._emit_fn(session, name, payload)
            return
        if session is None or self._host.sessions.get(session.token) is not session:
            logger.debug(f"{name} for {payload.get('run_uid')!r} dropped: session gone")
            return
        try:
            await self._host.emit(session, name, payload)
        except Exception as ex:
            logger.warning(f"could not deliver {name}: {ex!r}")

    def main(self) -> None:
        code = asyncio.run(self._main())
        if code:
            raise SystemExit(code)

    async def _main(self) -> int:
        """Run until cancelled (0) or until the NATS connection is closed for good (1).

        A closed connection never comes back, so exit non-zero and let a supervisor
        restart the service instead of sitting alive but deaf.
        """
        for channel in self._config.pva.channels:
            self._pva_buffer(channel)
        await self._nats.connect()
        self._nats.on_reconnected = self._on_reconnected
        await self._host.start(self._nats.core)
        self._host.set_state(state="ready")
        asyncio.get_running_loop().create_task(self._authenticate_bg())
        logger.info(f"rixs-live ready: service {SERVICE_NAME!r} on scope {self.scope!r}")
        try:
            while not self._nats.is_closed:
                await asyncio.sleep(0.5)
            logger.error("NATS connection closed for good; exiting so a supervisor "
                         "can restart rixs-live")
            return 1
        except (KeyboardInterrupt, asyncio.CancelledError):
            return 0
        finally:
            try:
                await self._host.stop()
            except Exception as ex:
                logger.warning(f"service host stop failed: {ex!r}")
            self.close_pva()
            await self._nats.close()

    def _pva_buffer(self, channel: str) -> PvaFrameBuffer:
        """The started frame buffer of ``channel``: one monitor per channel, kept."""
        buf = self._pva_buffers.get(channel)
        if buf is None:
            if self._pva_context is None:
                self._pva_context = self._pva_context_factory()
            buf = PvaFrameBuffer(channel, self._pva_context,
                                 capacity=self._config.pva.buffer_frames)
            buf.start()
            self._pva_buffers[channel] = buf
        return buf

    def close_pva(self) -> None:
        for buf in self._pva_buffers.values():
            buf.close()
        self._pva_buffers.clear()
        ctx, self._pva_context = self._pva_context, None
        if ctx is not None and hasattr(ctx, "close"):
            ctx.close()

    def _reader(self, run, info, start: dict):
        """Frame reader for a bound run: Tiled's image field, or its PVA channel."""
        if not info.uses_pva:
            return FocusRunReader(run, info)
        cfg = self._config.pva
        t0 = start.get("time")
        return PvaRunReader(
            run, info, self._pva_buffer(info.pva_channel),
            run_start_time=float(t0) if isinstance(t0, (int, float)) else None,
            frame_timeout_s=cfg.frame_timeout_s, stale_slack_s=cfg.stale_slack_s)

    async def _on_reconnected(self) -> None:
        # The host's subscriptions are replayed by nats-py; the Tiled credentials came from
        # a handshake on the old connection, so refresh them (kept until replaced).
        asyncio.get_running_loop().create_task(self._authenticate_bg())

    async def _authenticate_bg(self) -> None:
        try:
            await self._nats.authenticate()
        except Exception as ex:
            logger.warning(f"background auth failed: {ex}")

    def _pending_reply(self, run_uid: str) -> dict | None:
        """``{"pending": True, ...}`` while ``run_uid`` is binding, bound or draining."""
        cur = self._current
        if run_uid and cur is not None and cur["run_uid"] == run_uid:
            return {"pending": True, "run_uid": run_uid,
                    "frames_done": len(cur["frames"]), "total": cur["total"]}
        if run_uid and (run_uid in self._binding or run_uid in self._pending_stops):
            return {"pending": True, "run_uid": run_uid, "frames_done": 0, "total": None}
        return None

    def _resolve_credentials(self, data: dict) -> tuple[str, str | None]:
        """URL: bind, then cached auth, then config. Key: never from the bind.

        The key comes from Lightfall's trust handshake (approved by the user), not from
        whoever opened a session, so a bind can never point rixs-live's key elsewhere.
        """
        if data.get("tiled_api_key") is not None and not self._warned_bind_key:
            self._warned_bind_key = True
            logger.warning("ignoring tiled_api_key in run.bind: the key comes from "
                           "Lightfall's auth handshake")
        cached = self._nats.tiled_credentials or {}
        url = data.get("tiled_url") or cached.get("tiled_url") or self._config.tiled.url
        key = cached.get("tiled_token") or self._config.tiled.api_key
        return url, key

    async def _status(self, cur: dict, state: str) -> None:
        await self._emit(cur["session"], EVT_STATUS, {
            "run_uid": cur["run_uid"], "state": state,
            "frames_done": len(cur["frames"]), "total": cur["total"]})

    async def _lookup_run(self, tiled_client, run_uid: str):
        """``tiled_client[run_uid]``, retried with backoff while it raises KeyError.

        An asynchronous writer may not have created the run container yet when the
        bind arrives; give it up to ``bind_lookup_timeout_s``.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._config.bind_lookup_timeout_s
        delay = 0.05
        while True:
            try:
                return await loop.run_in_executor(None, tiled_client.__getitem__, run_uid)
            except KeyError:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise
                await asyncio.sleep(min(delay, remaining))
                delay = min(delay * 2, 0.5)

    async def _handle_bind(self, data: dict, session) -> None:
        run_uid = data.get("run_uid", "")
        self._binding.add(run_uid)
        try:
            await self._bind(run_uid, data, session)
        finally:
            self._binding.discard(run_uid)
        if run_uid in self._pending_stops:
            stop, stop_session = self._pending_stops.pop(run_uid)
            await self._handle_stop(stop, stop_session)

    async def _bind(self, run_uid: str, data: dict, session) -> None:
        try:
            version = data.get("contract_version", CONTRACT_VERSION)
            if version != CONTRACT_VERSION:
                raise ValueError(
                    f"contract_version {version} != {CONTRACT_VERSION}; refusing bind")
            url, key = self._resolve_credentials(data)
            proxy = data.get("tiled_proxy")
            proxy = proxy if isinstance(proxy, str) and proxy else None
            loop = asyncio.get_running_loop()
            try:
                tiled_client = await loop.run_in_executor(
                    None, self._tiled_connect, url, key, proxy)
            except Exception as ex:
                raise RuntimeError(self._tiled_connect_error(url, proxy, ex)) from ex
            run = await self._lookup_run(tiled_client, run_uid)
            start = run.metadata.get("start", {})
            info = parse_start_rixs(run_uid, start)
            if info is None:
                logger.info(f"bind ignored for non-rixs run {run_uid}")
                return
            reader = self._reader(run, info, start)
            if self._current is not None:
                logger.warning(f"replacing bound run {self._current['run_uid']}")
                await self._teardown_poll()
            self._current = {
                "run_uid": run_uid,
                "session": session,
                "reader": reader,
                "info": info,
                "total": start.get("num_points"),
                "next": 0,
                "frames": [],
                "stop": asyncio.Event(),
            }
            self._poll_task = loop.create_task(self._poll_run(self._current))
            await self._status(self._current, "binding")
            source = (f"PVA {info.pva_channel} by {info.frame_counter_field}"
                      if info.uses_pva else f"{info.image_stream}.{info.image_field}")
            logger.info(f"bound run {run_uid} ({source} vs {', '.join(info.fields)})")
        except Exception as ex:
            logger.exception(f"bind failed for {run_uid}")
            await self._emit(session, EVT_ERROR, {"run_uid": run_uid, "error": str(ex)})

    @staticmethod
    def _tiled_connect_error(url: str, proxy: str | None, ex: Exception) -> str:
        """Error event text for a failed Tiled connect: URL, proxy used, what to check."""
        env = env_proxy()
        if proxy:
            via = f"proxy {redact_proxy(proxy)} (from Lightfall)"
        elif env is not None:
            via = f"proxy {redact_proxy(env)} (from the service environment)"
        else:
            via = "no proxy"
        target = url or "<no Tiled URL>"
        return (f"cannot connect to Tiled at {target} using {via}: "
                f"{type(ex).__name__}: {ex}. Check the proxy: Lightfall's proxy "
                f"preferences are sent with each bind, and ALL_PROXY/HTTPS_PROXY in "
                f"rixs-live's environment are used when the bind has none")

    async def _score_new(self, cur: dict, *, drain: bool = False) -> int:
        """Score and publish every frame not yet seen; returns how many were new.

        Raises KeyError while the image stream does not exist yet. Stops early
        (between frames) once a stop/rebind is requested, except when draining.
        """
        loop = asyncio.get_running_loop()
        first = cur["next"]
        rows = await loop.run_in_executor(None, cur["reader"].read_new_rows, first)
        info = cur["info"]
        disp = self._config.dispersion_mev_per_px
        for seq_num, positions, frame in rows:
            if cur["stop"].is_set() and not drain:
                break  # stop/rebind requested: leave the rest to the drain (or drop it)
            score = await loop.run_in_executor(
                None, score_frame, frame, disp, info.mono_energy_ev)
            preview = await loop.run_in_executor(None, make_preview, frame)
            payload = {"run_uid": cur["run_uid"], "seq_num": seq_num,
                       "motor_field": info.motor_field, "motor_units": info.motor_units,
                       "position": positions[info.fields[0]], "positions": positions,
                       **score.to_dict(), "preview": preview}
            cur["frames"].append(payload)
            cur["next"] += 1
            await self._emit(cur["session"], EVT_FRAME, payload)
        return cur["next"] - first

    async def _poll_run(self, cur: dict) -> None:
        while not cur["stop"].is_set():
            try:
                try:
                    n_new = await self._score_new(cur)
                except KeyError:
                    n_new = 0  # stream not created yet
                if n_new:
                    await self._status(cur, "scoring")
            except Exception as ex:
                logger.exception(f"poll failed for {cur['run_uid']}")
                await self._emit(cur["session"], EVT_ERROR,
                                 {"run_uid": cur["run_uid"], "error": str(ex)})
                if self._current is cur:
                    self._current = None
                    await self._status(cur, "idle")
                return
            try:
                await asyncio.wait_for(cur["stop"].wait(),
                                       self._config.poll_interval_s)
            except asyncio.TimeoutError:
                pass

    async def _teardown_poll(self) -> None:
        if self._current is not None:
            self._current["stop"].set()
        if self._poll_task is not None:
            try:
                await self._poll_task
            except Exception:
                logger.exception("poll task died")
            self._poll_task = None
        self._current = None

    @staticmethod
    def _expected_frames(cur: dict, stop: dict) -> int | None:
        """Frame count the drain should wait for, or None to wait for rows to settle.

        Stop doc ``num_events[<image stream>]`` (``primary`` unless the start doc
        names another ``image_stream``) wins when present; else a non-success
        ``exit_status`` (abort/fail) means the start doc's ``num_points`` will never
        be reached; else ``num_points``.
        """
        ne = stop.get("num_events")
        primary = ne.get(cur["info"].image_stream) if isinstance(ne, dict) else None
        if isinstance(primary, int) and not isinstance(primary, bool) and primary >= 0:
            return primary
        status = stop.get("exit_status")
        if status is not None and status != "success":
            return None
        total = cur["total"]
        return total if isinstance(total, int) and total > 0 else None

    async def _drain(self, cur: dict, stop: dict | None = None) -> None:
        """Score the frames the poll loop has not seen, including late ones.

        With an asynchronous writer the last events may land after the stop. Keep
        re-polling until the expected frame count (stop doc ``num_events`` of the image stream,
        else start doc ``num_points`` for a successful run) has been read, else
        (unknown count, or an aborted/failed run) until no new rows appeared for
        ``drain_settle_s``. The cap,
        ``drain_timeout_s``, counts only time spent *waiting without new rows*:
        scoring a backlog (~1 s/frame) never cuts off frames that are already there.
        Then complete with whatever arrived.
        """
        loop = asyncio.get_running_loop()
        cfg = self._config
        total = self._expected_frames(cur, stop or {})
        known = total is not None
        interval = min(cfg.poll_interval_s, 0.1)
        idle = 0.0  # waiting time since the last new row
        while True:
            t = loop.time()
            try:
                n_new = await self._score_new(cur, drain=True)
            except KeyError:
                n_new = 0  # stream not created (yet)
            if known and len(cur["frames"]) >= total:
                return
            if n_new:
                idle = 0.0
                continue
            idle += loop.time() - t
            if not known and idle >= cfg.drain_settle_s:
                return
            if idle >= cfg.drain_timeout_s:
                logger.warning(f"drain for {cur['run_uid']} gave up waiting with "
                               f"{len(cur['frames'])}/{total} frames")
                return
            await asyncio.sleep(interval)
            idle += interval

    def _complete_payload(self, cur: dict) -> dict:
        frames = cur["frames"]
        info = cur["info"]
        multi = info.multi_motor
        keys = POINT_KEYS + ("positions",) if multi else POINT_KEYS
        points = [{k: f[k] for k in keys} for f in frames]
        valid = [p for p in points if p["score_valid"] and p["fwhm_px"] is not None]
        if multi:
            # An N-D run (gpCAM optimization) has no single axis to fit along.
            fit = {"ok": False, "optimal_position": None, "coeffs": None,
                   "reason": MULTI_MOTOR_NO_FIT}
        else:
            fit = fit_focus_curve([p["position"] for p in valid],
                                  [p["fwhm_px"] for p in valid])
        # Lowest FWHM == highest score (1/FWHM).
        best = min(valid, key=lambda p: p["fwhm_px"]) if valid else None
        best_measured = None
        if best:
            best_measured = {"position": best["position"], "fwhm_px": best["fwhm_px"]}
            if multi:
                best_measured["positions"] = dict(best["positions"])
        payload = {
            "run_uid": cur["run_uid"],
            "motor_field": info.motor_field,
            "motor_units": info.motor_units,
            "points": points,
            "fit": fit,
            "best_measured": best_measured,
        }
        if multi:
            payload["motor_fields"] = list(info.fields)
        return payload

    def _store(self, run_uid: str, payload: dict) -> None:
        self.results[run_uid] = payload
        self.results.move_to_end(run_uid)
        while len(self.results) > max(self._config.keep_results, 0):
            self.results.popitem(last=False)

    async def _handle_stop(self, data: dict, session) -> None:
        run_uid = data.get("run_uid", "")
        cur = self._current
        if run_uid in self._binding and (cur is None or cur["run_uid"] != run_uid):
            logger.info(f"stop for {run_uid} arrived during its bind; deferred")
            self._pending_stops[run_uid] = (data, session)
            return
        if cur is None or cur["run_uid"] != run_uid:
            logger.info(f"stop for unknown/unbound run {run_uid}; ignoring")
            return
        try:
            cur["stop"].set()
            if self._poll_task is not None:
                await self._poll_task
                self._poll_task = None
            if self._current is not cur:  # poll failed (error already published)
                return
            await self._drain(cur, data)
            payload = self._complete_payload(cur)
            self._store(run_uid, payload)
            await self._emit(cur["session"], EVT_COMPLETE, payload)
            logger.info(f"run {run_uid} complete: {len(cur['frames'])} frames, "
                        f"fit {payload['fit']}")
        except Exception as ex:
            logger.exception(f"completion failed for {run_uid}")
            await self._emit(cur["session"], EVT_ERROR, {"run_uid": run_uid, "error": str(ex)})
        finally:
            if self._current is cur:
                self._current = None
                await self._status(cur, "idle")

    async def wait_idle(self) -> None:
        """Test helper: await completion of any in-flight poll task."""
        if self._poll_task is not None:
            if self._current is not None:
                self._current["stop"].set()
            await self._poll_task
            self._poll_task = None
