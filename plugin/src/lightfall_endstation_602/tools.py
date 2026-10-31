"""Agent tools for the QERLIN closed-loop focusing workflow.

Thin wrappers over the rixs-live service client. Blocking IPC requests run in a
worker thread (``asyncio.to_thread``), never on the Qt main thread.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from loguru import logger

from lightfall.plugins.tool_plugin import ToolPlugin

from . import runtime

_PKG_DIR = Path(__file__).parent
_REGISTERED = False

_HINT = "start it with: rixs-live run --nats-url ..."


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


def _status() -> dict[str, Any]:
    binder_enabled = runtime.ensure_binder_enabled()
    reply = runtime.get_client().discover(2000)
    if not reply:
        return {"service_up": False, "hint": _HINT}
    return {
        "service_up": True,
        "app_version": reply.get("app_version"),
        "dispersion_mev_per_px": reply.get("dispersion_mev_per_px"),
        "binder_enabled": binder_enabled,
    }


def _focus_result(run_uid: str | None) -> tuple[str, dict | None]:
    client = runtime.get_client()
    uid = run_uid
    if not uid and client.last_complete:
        uid = client.last_complete.get("run_uid")
    if not uid:
        return "", None
    return uid, client.get_result(uid)


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
        return "Closed-loop SM3 focusing for the QERLIN (6.0.2) RIXS spectrometer via the rixs-live service"

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
                return mcp_result(await asyncio.to_thread(_status))
            except Exception as ex:  # noqa: BLE001 - surface to the agent
                return mcp_error(f"rixs service status failed: {ex}")

        @tool(
            name="get_focus_result",
            description=(
                "Fetch the fitted focus curve for a finished qerlin_focus_scan run from the "
                "rixs-live service. run_uid is optional and defaults to the most recently "
                "completed run. Returns {run_uid, motor_field, points:[{position, fwhm_px, "
                "fwhm_mev, resolving_power, score_valid}], fit:{ok, optimal_position, coeffs, "
                "reason}, best_measured}. Errors if the service is down or the scan is not "
                "finished yet."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "run_uid": {
                        "type": "string",
                        "description": "Run uid of the focus scan (default: last completed run).",
                    }
                },
            },
        )
        async def get_focus_result(args: dict) -> dict[str, Any]:
            requested = (args or {}).get("run_uid") or None
            try:
                uid, result = await asyncio.to_thread(_focus_result, requested)
            except Exception as ex:  # noqa: BLE001 - surface to the agent
                return mcp_error(f"get_focus_result failed: {ex}")
            if result is None:
                label = uid or requested or "<no run>"
                return mcp_error(
                    f"no focus result for {label} (service down, or scan not finished)"
                )
            return mcp_result(result)

        return [rixs_service_status, get_focus_result]
