"""Lightfall device backend exposing the simulated QERLIN devices."""
from lightfall.plugins.happi_database_plugin import HappiDatabasePlugin


class QerlinSimBackendPlugin(HappiDatabasePlugin):
    """Simulated 6.0.2 devices (SM3 pitch, mono energy, Marana) from a packaged happi DB."""

    database_resource = ("lightfall_endstation_602.sim", "qerlin_happi.json")
    instantiate = "background"

    @property
    def name(self) -> str:
        return "qerlin_sim"

    @property
    def description(self) -> str:
        return "Simulated QERLIN (ALS 6.0.2) devices: SM3 pitch, mono energy, Marana"
