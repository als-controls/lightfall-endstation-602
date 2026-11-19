"""ServicePlugin declaring the external ``rixs-live`` analysis service.

rixs-live runs on its own (a beamline host, under systemd or by hand), so the descriptor
is ``launch="external"``: Lightfall's ServiceManager connects to it over the main IPC
broker, at ``{ipc_topic_prefix}.svc.rixs-live`` (e.g. ``als.602.svc.rixs-live``), and
keeps a private session open. :class:`~lightfall_endstation_602.client.RixsServiceClient`
follows that session's client.
"""

from __future__ import annotations

from lightfall.plugins.service_plugin import ServicePlugin
from lightfall_utils.ipc.services.descriptor import ServiceDescriptor

from lightfall_endstation_602 import contract as c


class RixsLiveServicePlugin(ServicePlugin):
    """Declares the external ``rixs-live`` service (QERLIN frame scoring)."""

    @property
    def name(self) -> str:
        return c.SERVICE_NAME

    @property
    def description(self) -> str:
        return "QERLIN RIXS frame scoring service (rixs-live), reached over NATS"

    def descriptor(self) -> ServiceDescriptor:
        return ServiceDescriptor(
            name=c.SERVICE_NAME,
            scope="{prefix}",
            contract=c.SERVICE_CONTRACT,
            launch="external",
            hint=("Start rixs-live on the beamline host with --lightfall-prefix set to "
                  "this Lightfall's ipc_topic_prefix (see the lightfall-endstation-602 "
                  "README)"),
        )
