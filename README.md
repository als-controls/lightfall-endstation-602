# lightfall-endstation-6.0.2

Lightfall integration for the ALS beamline 6.0.2 (QERLIN) endstation. Private until licensed.

- `plugin/` - `lightfall-endstation-602` (import `lightfall_endstation_602`): Lightfall plugin with sim devices and UI.
- `rixs-live/` - `rixs-live` (import `rixs_live`): headless NATS-bound analysis service wrapping the QERLIN RIXS code.

The two packages share a small versioned NATS contract (`contract.py`), duplicated in each.
The service never imports the plugin at runtime.
