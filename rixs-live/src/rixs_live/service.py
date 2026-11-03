"""rixs-live event loop: bind -> poll Tiled -> score + publish each frame; stop -> fit + complete."""
from __future__ import annotations

import asyncio
from collections import OrderedDict

from loguru import logger

from rixs_live.config import AppConfig
from rixs_live.contract import (
    CONTRACT_VERSION,
    SUBJ_BIND,
    SUBJ_COMPLETE,
    SUBJ_DISCOVER,
    SUBJ_ERROR,
    SUBJ_FRAME,
    SUBJ_RESULT,
    SUBJ_STATUS,
    SUBJ_STOP,
    parse_start_rixs,
)
from rixs_live.focus import fit_focus_curve, make_preview, score_frame
from rixs_live.nats_client import NatsClient
from rixs_live.tiled_io import FocusRunReader, connect_tiled, env_proxy, redact_proxy

EVENTS = [SUBJ_STATUS, SUBJ_ERROR, SUBJ_FRAME, SUBJ_COMPLETE]
ACTIONS = [SUBJ_RESULT]
POINT_KEYS = ("position", "fwhm_px", "fwhm_mev", "resolving_power", "score_valid")


class RixsLiveService:
    def __init__(self, config: AppConfig, *, nats_client=None, tiled_connect=None) -> None:
        self._config = config
        self._nats = nats_client or NatsClient(config.nats)
        self._tiled_connect = tiled_connect or connect_tiled
        self._warned_bind_key = False
        self._warned_prefix: str | None = None
        self._poll_task: asyncio.Task | None = None
        # dict: run_uid, reader, info, total, next, frames (per-frame payloads), stop (Event)
        self._current: dict | None = None
        self.results: OrderedDict[str, dict] = OrderedDict()
        self._binding: set[str] = set()  # run uids whose bind is still in progress
        self._pending_stops: dict[str, dict] = {}  # stops that arrived during their bind

    @property
    def busy(self) -> bool:
        return self._current is not None

    def main(self) -> None:
        code = asyncio.run(self._main())
        if code:
            raise SystemExit(code)

    async def _main(self) -> int:
        """Run until cancelled (0) or until the NATS connection is closed for good (1).

        A closed connection never comes back, so exit non-zero and let a supervisor
        restart the service instead of sitting alive but deaf.
        """
        await self._nats.connect()
        self._nats.on_reconnected = self._on_reconnected
        await self._register_subscriptions()
        asyncio.get_running_loop().create_task(self._authenticate_bg())
        logger.info("rixs-live ready")
        try:
            while not self._nats.is_closed:
                await asyncio.sleep(0.5)
            logger.error("NATS connection closed for good; exiting so a supervisor "
                         "can restart rixs-live")
            return 1
        except (KeyboardInterrupt, asyncio.CancelledError):
            return 0
        finally:
            await self._nats.close()

    async def _on_reconnected(self) -> None:
        # Subscriptions are replayed by nats-py itself; the Tiled credentials came from
        # a handshake on the old connection, so refresh them (kept until replaced).
        asyncio.get_running_loop().create_task(self._authenticate_bg())

    async def _authenticate_bg(self) -> None:
        try:
            await self._nats.authenticate()
        except Exception as ex:
            logger.warning(f"background auth failed: {ex}")

    async def _register_subscriptions(self) -> None:
        await self._nats.subscribe(SUBJ_BIND, self._handle_bind)
        await self._nats.subscribe(SUBJ_STOP, self._handle_stop)
        await self._nats.subscribe(SUBJ_RESULT, self._handle_result)
        await self._nats.subscribe(SUBJ_DISCOVER, self._handle_discover)
        await self._nats.subscribe("rixs.meta.actions", self._handle_discover)
        await self._nats.subscribe("rixs.meta.events", self._handle_discover)

    async def _handle_discover(self, data, msg) -> None:
        await self._nats.reply(msg, {
            "app_name": self._config.nats.app_name,
            "app_version": self._config.nats.app_version,
            "actions": ACTIONS,
            "events": EVENTS,
            "dispersion_mev_per_px": self._config.dispersion_mev_per_px,
        })

    async def _handle_result(self, data: dict, msg) -> None:
        run_uid = data.get("run_uid", "")
        result = self.results.get(run_uid)
        if result is None:
            result = self._pending_reply(run_uid)
        await self._nats.reply(msg, result if result is not None
                               else {"error": f"unknown run {run_uid}"})

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
        """URL: bind, then cached auth, then config. Key: never from the bind."""
        if data.get("tiled_api_key") is not None and not self._warned_bind_key:
            self._warned_bind_key = True
            logger.warning("ignoring tiled_api_key in rixs.run.bind: API keys must not "
                           "travel on public subjects (update the Lightfall plugin)")
        cached = self._nats.tiled_credentials or {}
        url = data.get("tiled_url") or cached.get("tiled_url") or self._config.tiled.url
        key = cached.get("tiled_token") or self._config.tiled.api_key
        return url, key

    def _check_bind_prefix(self, data: dict) -> None:
        """Log once per distinct value when the bind's prefix differs from ours.

        The service only uses its own ``lightfall_prefix`` (for auth); a mismatch
        usually means the service is pointed at a different facility than Lightfall.
        """
        prefix = data.get("lightfall_prefix")
        mine = self._config.nats.lightfall_prefix
        if prefix and prefix != mine and prefix != self._warned_prefix:
            self._warned_prefix = prefix
            logger.warning(f"bind lightfall_prefix {prefix!r} differs from this service's "
                           f"--lightfall-prefix {mine!r}; auth/subjects use the service's "
                           f"value (check for a misconfigured prefix)")

    async def _status(self, cur: dict, state: str) -> None:
        await self._nats.publish(SUBJ_STATUS, {
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

    async def _handle_bind(self, data: dict, msg) -> None:
        run_uid = data.get("run_uid", "")
        self._binding.add(run_uid)
        try:
            await self._bind(run_uid, data)
        finally:
            self._binding.discard(run_uid)
        if run_uid in self._pending_stops:
            await self._handle_stop(self._pending_stops.pop(run_uid), None)

    async def _bind(self, run_uid: str, data: dict) -> None:
        try:
            version = data.get("contract_version", CONTRACT_VERSION)
            if version != CONTRACT_VERSION:
                raise ValueError(
                    f"contract_version {version} != {CONTRACT_VERSION}; refusing bind")
            self._check_bind_prefix(data)
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
            if self._current is not None:
                logger.warning(f"replacing bound run {self._current['run_uid']}")
                await self._teardown_poll()
            self._current = {
                "run_uid": run_uid,
                "reader": FocusRunReader(run, info),
                "info": info,
                "total": start.get("num_points"),
                "next": 0,
                "frames": [],
                "stop": asyncio.Event(),
            }
            self._poll_task = loop.create_task(self._poll_run(self._current))
            await self._status(self._current, "binding")
            logger.info(f"bound run {run_uid} ({info.image_field} vs {info.motor_field})")
        except Exception as ex:
            logger.exception(f"bind failed for {run_uid}")
            await self._nats.publish(SUBJ_ERROR, {"run_uid": run_uid, "error": str(ex)})

    @staticmethod
    def _tiled_connect_error(url: str, proxy: str | None, ex: Exception) -> str:
        """rixs.error text for a failed Tiled connect: URL, proxy used, what to check."""
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

        Raises KeyError while the primary stream does not exist yet. Stops early
        (between frames) once a stop/rebind is requested, except when draining.
        """
        loop = asyncio.get_running_loop()
        first = cur["next"]
        rows = await loop.run_in_executor(None, cur["reader"].read_new, first)
        info = cur["info"]
        disp = self._config.dispersion_mev_per_px
        for seq_num, position, frame in rows:
            if cur["stop"].is_set() and not drain:
                break  # stop/rebind requested: leave the rest to the drain (or drop it)
            score = await loop.run_in_executor(
                None, score_frame, frame, disp, info.mono_energy_ev)
            preview = await loop.run_in_executor(None, make_preview, frame)
            payload = {"run_uid": cur["run_uid"], "seq_num": seq_num,
                       "motor_field": info.motor_field, "motor_units": info.motor_units,
                       "position": position, **score.to_dict(), "preview": preview}
            cur["frames"].append(payload)
            cur["next"] += 1
            await self._nats.publish(SUBJ_FRAME, payload)
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
                await self._nats.publish(SUBJ_ERROR,
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

        Stop doc ``num_events.primary`` wins when present; else a non-success
        ``exit_status`` (abort/fail) means the start doc's ``num_points`` will never
        be reached; else ``num_points``.
        """
        ne = stop.get("num_events")
        primary = ne.get("primary") if isinstance(ne, dict) else None
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
        re-polling until the expected frame count (stop doc ``num_events.primary``,
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
        points = [{k: f[k] for k in POINT_KEYS} for f in frames]
        valid = [p for p in points if p["score_valid"] and p["fwhm_px"] is not None]
        fit = fit_focus_curve([p["position"] for p in valid], [p["fwhm_px"] for p in valid])
        best = min(valid, key=lambda p: p["fwhm_px"]) if valid else None
        return {
            "run_uid": cur["run_uid"],
            "motor_field": cur["info"].motor_field,
            "motor_units": cur["info"].motor_units,
            "points": points,
            "fit": fit,
            "best_measured": ({"position": best["position"], "fwhm_px": best["fwhm_px"]}
                              if best else None),
        }

    def _store(self, run_uid: str, payload: dict) -> None:
        self.results[run_uid] = payload
        self.results.move_to_end(run_uid)
        while len(self.results) > max(self._config.keep_results, 0):
            self.results.popitem(last=False)

    async def _handle_stop(self, data: dict, msg) -> None:
        run_uid = data.get("run_uid", "")
        cur = self._current
        if run_uid in self._binding and (cur is None or cur["run_uid"] != run_uid):
            logger.info(f"stop for {run_uid} arrived during its bind; deferred")
            self._pending_stops[run_uid] = data
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
            await self._nats.publish(SUBJ_COMPLETE, payload)
            logger.info(f"run {run_uid} complete: {len(cur['frames'])} frames, "
                        f"fit {payload['fit']}")
        except Exception as ex:
            logger.exception(f"completion failed for {run_uid}")
            await self._nats.publish(SUBJ_ERROR, {"run_uid": run_uid, "error": str(ex)})
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
