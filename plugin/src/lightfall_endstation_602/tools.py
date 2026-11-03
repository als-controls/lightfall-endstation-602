"""Agent tools for the QERLIN closed-loop focusing workflow.

Thin wrappers over the rixs-live service client. The runtime singletons (QObjects)
are created and touched on the Qt main thread via ``run_on_main_thread``; only the
blocking IPC request runs in a worker thread (``asyncio.to_thread``).
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from loguru import logger

from lightfall.plugins.tool_plugin import ToolPlugin

from . import runtime

_PKG_DIR = Path(__file__).parent
_REGISTERED = False

_HINT = "start it with: rixs-live run --nats-url ..."

# get_focus_result: how long to wait for a still-running analysis, and how often to ask.
WAIT_DEFAULT_S = 60.0
WAIT_MAX_S = 300.0
POLL_INTERVAL_S = 1.0


def _register_agent_dir() -> None:
    """Register the packaged agent specs (once per process)."""
    global _REGISTERED
    if _REGISTERED:
        return
    _REGISTERED = True
    try:
        from lightfall.agents.registry import AgentSpecRegistry

        AgentSpecRegistry.get_instance().register_scope_dir("beamline", _PKG_DIR / "agents")
    except Exception as ex:  # noqa: BLE001 - agent spec is optional, never break plugin load
        logger.warning(f"could not register QERLIN agent specs: {ex!r}")


def _on_main(func):
    """Run ``func`` on the Qt main thread (the runtime singletons are QObjects)."""
    from lightfall.claude._internal.threading import run_on_main_thread

    return run_on_main_thread(func)


def _prepare_status() -> tuple[Any, bool]:
    """Main thread: enable the binder and fetch the client."""
    binder_enabled = runtime.ensure_binder_enabled()
    return runtime.get_client(), binder_enabled


def _status_payload(reply: dict | None, binder_enabled: bool) -> dict[str, Any]:
    if not reply:
        return {"service_up": False, "hint": _HINT}
    return {
        "service_up": True,
        "app_version": reply.get("app_version"),
        "dispersion_mev_per_px": reply.get("dispersion_mev_per_px"),
        "binder_enabled": binder_enabled,
    }


def _prepare_result(run_uid: str | None) -> tuple[Any, str, bool]:
    """Main thread: re-arm the binder, fetch the client, resolve the default run uid."""
    binder_enabled = runtime.ensure_binder_enabled()
    client = runtime.get_client()
    # Default to the last *bound* run: its complete message lands seconds after the
    # RunEngine goes idle, so last_complete would still be the previous scan.
    uid = run_uid or getattr(client, "last_bound_uid", None)
    if not uid and client.last_complete:
        uid = client.last_complete.get("run_uid")
    return client, uid or "", binder_enabled


def _wait_s(value) -> float:
    try:
        w = float(value)
    except (TypeError, ValueError):
        return WAIT_DEFAULT_S
    return min(max(w, 0.0), WAIT_MAX_S) if w == w else WAIT_DEFAULT_S


def _fetch_result(client, uid: str, wait_s: float) -> dict | None:
    """Worker thread: ask for the result, re-asking while the service says pending."""
    deadline = time.monotonic() + wait_s
    while True:
        result = client.get_result(uid)
        if not (isinstance(result, dict) and result.get("pending")):
            return result
        if time.monotonic() >= deadline:
            return result  # still pending
        time.sleep(min(POLL_INTERVAL_S, max(deadline - time.monotonic(), 0.0)))


class QerlinFocusingTools(ToolPlugin):
    """QERLIN focusing skill plus rixs-live service tools."""

    def __init__(self) -> None:
        super().__init__()
        _register_agent_dir()

    @property
    def name(self) -> str:
        return "qerlin_focusing"

    @property
    def display_name(self) -> str:
        return "QERLIN Focusing"

    @property
    def description(self) -> str:
        return ("Closed-loop focusing (SM3 pitch, Sample X, any motor) for the QERLIN (6.0.2) "
                "RIXS spectrometer via the rixs-live service")

    @property
    def category(self) -> str:
        return "acquisition"

    def get_skill_dir(self) -> Path | None:
        return _PKG_DIR / "skills" / "qerlin_focusing"

    def get_references_dir(self) -> Path | None:
        return _PKG_DIR / "skills" / "qerlin_focusing" / "references"

    def create_tools(self) -> list[Any]:
        try:
            from lightfall.agents.tooling import tool
        except ImportError:
            try:
                from claude_agent_sdk import tool
            except ImportError:
                logger.warning("no agent tool decorator available, qerlin_focusing tools disabled")
                return []

        from lightfall.plugins.agents._mcp_helpers import mcp_error, mcp_result

        @tool(
            name="rixs_service_status",
            description=(
                "Check whether the headless rixs-live analysis service is reachable. Returns "
                "{service_up, app_version, dispersion_mev_per_px, binder_enabled}, or "
                "{service_up: false, hint} when it is down. Call before every focus scan; if "
                "the service is down, stop and tell the user. dispersion_mev_per_px of 0 or "
                "null means resolving power cannot be computed."
            ),
            input_schema={"type": "object", "properties": {}},
        )
        async def rixs_service_status(args: dict) -> dict[str, Any]:
            try:
                client, binder_enabled = _on_main(_prepare_status)
                reply = await asyncio.to_thread(client.discover, 2000)
                # Same state the QERLIN Focus panel shows: record the fresh result.
                record = getattr(client, "record_health", None)
                if record is not None:
                    _on_main(lambda: record(reply))
                return mcp_result(_status_payload(reply, binder_enabled))
            except Exception as ex:  # noqa: BLE001 - surface to the agent
                return mcp_error(f"rixs service status failed: {ex}")

        @tool(
            name="get_focus_result",
            description=(
                "Fetch the fitted focus curve for a qerlin_focus_scan run from the rixs-live "
                "service. run_uid is optional and defaults to the most recently started "
                "(bound) focus scan. The service finishes scoring a few seconds after the "
                "RunEngine goes idle; this tool waits for that analysis (up to wait_s "
                "seconds, default 60, max 300) and then returns {run_uid, motor_field, "
                "motor_units, points:[{position, fwhm_px, fwhm_mev, resolving_power, "
                "score_valid}], fit:{ok, optimal_position, coeffs, reason}, best_measured}; "
                "positions are in the scanned motor's units (motor_units, \"\" = native "
                "units, e.g. SM3 pitch). Errors if the "
                "service is down, the run is unknown to it, or the analysis is still "
                "running after wait_s."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "run_uid": {
                        "type": "string",
                        "description": "Run uid of the focus scan (default: last bound run).",
                    },
                    "wait_s": {
                        "type": "number",
                        "description": (
                            "Seconds to wait for a still-running analysis (default 60, "
                            "max 300)."),
                        "default": WAIT_DEFAULT_S,
                    },
                },
            },
        )
        async def get_focus_result(args: dict) -> dict[str, Any]:
            args = args or {}
            requested = args.get("run_uid") or None
            wait_s = _wait_s(args.get("wait_s", WAIT_DEFAULT_S))
            try:
                client, uid, binder_enabled = _on_main(lambda: _prepare_result(requested))
                result = (await asyncio.to_thread(_fetch_result, client, uid, wait_s)
                          if uid else None)
            except Exception as ex:  # noqa: BLE001 - surface to the agent
                return mcp_error(f"get_focus_result failed: {ex}")
            if isinstance(result, dict) and result.get("pending"):
                total = result.get("total")
                return mcp_error(
                    f"analysis of {uid} is still running after {wait_s:g} s "
                    f"({result.get('frames_done', 0)}/{'?' if total is None else total} "
                    "frames scored); call get_focus_result again, optionally with a "
                    "larger wait_s")
            if result is None:
                label = uid or requested or "<no run>"
                msg = f"no focus result for {label} (service down, or scan not finished)"
                if not binder_enabled:
                    msg += ("; the rixs run binder is not enabled (no RunEngine?), so scans "
                            "are not sent to the service for analysis")
                return mcp_error(msg)
            return mcp_result(result)

        return [rixs_service_status, get_focus_result]
