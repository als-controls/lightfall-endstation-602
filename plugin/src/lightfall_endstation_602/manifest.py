"""Lightfall plugin manifest for lightfall-endstation-6.0.2."""

from lightfall.plugins import PluginEntry, PluginManifest

manifest = PluginManifest(
    name="lightfall-endstation-602",
    plugins=[
        PluginEntry("device_backend", "qerlin_sim",
                    "lightfall_endstation_602.backend:QerlinSimBackendPlugin"),
        PluginEntry("plan", "qerlin_focus_scan",
                    "lightfall_endstation_602.plan_plugin:QerlinFocusScanPlanPlugin"),
        PluginEntry("panel", "qerlin_focus",
                    "lightfall_endstation_602.panel:QerlinFocusPanelPlugin",
                    preload=True),
        PluginEntry("tool", "qerlin_focusing",
                    "lightfall_endstation_602.tools:QerlinFocusingTools"),
    ],
)
