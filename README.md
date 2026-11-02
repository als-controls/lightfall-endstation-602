# lightfall-endstation-6.0.2

Lightfall integration for the ALS beamline 6.0.2 (QERLIN) endstation. Private until licensed.

- `plugin/` - `lightfall-endstation-602` (import `lightfall_endstation_602`): Lightfall plugin with sim devices and UI.
- `rixs-live/` - `rixs-live` (import `rixs_live`): headless NATS-bound analysis service wrapping the QERLIN RIXS code.

The two packages share a small versioned NATS contract (`contract.py`), duplicated in each.
The service never imports the plugin at runtime.

## Licensing

This repository stays private (local, no remote push) until QERLIN's code
(`mRIXS-super-app`, imported as `rixs_app`) has a license. Do not publish it or
the `rixs-live` package before then.

## Setup

Both packages use pip and plain venvs.

**Plugin** - install editable into the Lightfall dev venv (the shared one next
to the core checkout):

```
cd plugin
../../lightfall/.venv/Scripts/python -m pip install -e ".[dev]"
```

**Service** - its own Python 3.12 venv, with QERLIN's app installed from the
sibling checkout:

```
cd rixs-live
py -3.12 -m venv .venv
.venv/Scripts/python -m pip install -e ../../mRIXS-super-app
.venv/Scripts/python -m pip install -e ".[test]"
```

Tests: `../../lightfall/.venv/Scripts/python -m pytest` in `plugin/`;
`.venv/Scripts/python -m pytest` in `rixs-live/`.

## Demo

### Headless smoke (run 2026-10-30)

Two scripts live outside git under
`.superpowers/sdd/2026-10-30-qerlin-602-closed-loop/`:

1. `smoke_loader.py` (run with the Lightfall venv python, offscreen Qt) loads
   the manifest through Lightfall's real `PluginLoader` and checks the result:

   ```
   loader: 4 successful, 0 failed
     device_backend:qerlin_sim -> PluginStatus.READY
     plan:qerlin_focus_scan -> PluginStatus.READY
     panel:qerlin_focus -> PluginStatus.READY
     tool:qerlin_focusing -> PluginStatus.READY
   AgentSpecRegistry 'qerlin': True qerlin
   plan plugin name: qerlin_focus_scan fn: _qerlin_focus_scan_ui
   qerlin_sim devices: ['marana', 'mono_energy', 'sm3_pitch']
   panel: lightfall_endstation_602.panels.qerlin_focus / QERLIN Focus
   panel default construction: toggle checked = True binder enabled = False
     status = waiting for RunEngine (...)   # no engine thread headless; the panel retries every second
   ```

2. `smoke_nats.py` starts a real `nats-server` on a free port, then
   `rixs-live/.venv/Scripts/rixs-live.exe run --nats-url nats://127.0.0.1:<port> --dispersion 2.5 --lightfall-prefix als.602`,
   and makes one `_rixs.discover` request with nats-py:

   ```
   {"app_name": "rixs-live", "app_version": "",
    "actions": ["rixs.zeroth.result"],
    "events": ["rixs.status", "rixs.error", "rixs.zeroth.frame", "rixs.zeroth.complete"],
    "dispersion_mev_per_px": 2.5}
   ```

   Both processes are terminated at the end.

### Live demo checklist (GUI)

1. **NATS.** If Lightfall is already configured for a broker (`ipc_nats_url`,
   or `ipc_use_local_nats`), use that URL. Otherwise start one:
   `../lightfall/.venv/Scripts/nats-server.exe -p 4222` and set Lightfall's
   `ipc_nats_url` to `nats://127.0.0.1:4222`.
2. **Service.** Start rixs-live with the same broker, Lightfall's
   `ipc_topic_prefix` (Preferences; default `als.7011`, use whatever your
   Lightfall shows) and the spectrometer dispersion in meV/px:
   `rixs-live/.venv/Scripts/rixs-live run --nats-url nats://127.0.0.1:4222 --lightfall-prefix <ipc_topic_prefix> --dispersion 2.5`
   Tiled is whatever Lightfall is configured for: the plugin sends only the Tiled
   URL in `rixs.run.bind`. The API key never travels on that public subject; the
   service gets it from the private auth handshake with Lightfall. On a local
   broker without the auth callout, pass `--tiled-api-key <key>` (or set
   `RIXS_LIVE_TILED_API_KEY`), and `--tiled-url` if needed; pass nothing for a
   Tiled that allows anonymous reads.
   An empty `app_version` in `rixs_service_status` is expected for an editable install.
3. **Backend.** Start Lightfall and enable the `qerlin_sim` device backend
   (Settings -> Devices; the preference is `device_plugin_qerlin_sim_enabled`). Check that `sm3_pitch`, `mono_energy` and `marana`
   appear.
4. **Panel.** Open the "QERLIN Focus" panel (Analysis). Its title-bar toggle
   is on by default; the status line should read "bound: waiting for a
   qerlin_focus_scan run" once the RunEngine is up.
5. **Plan from the UI.** Run `qerlin_focus_scan` once (devices marana, sm3,
   mono; defaults -1..1 mrad, 21 points). The left plot fills with FWHM vs
   pitch, the right shows the zeroth-order preview with its fitted line, and
   on `rixs.zeroth.complete` the fitted parabola and a vertical line at the
   optimal pitch appear.
6. **Agent.** Open the "qerlin" agent in the assistant panel and ask it to
   focus the spectrometer. It checks `rixs_service_status`, runs focus scans,
   reads `get_focus_result`, and moves SM3 to the optimum.

An aborted or failed scan completes as soon as its rows stop arriving
(`drain_settle_s`): the stop document's `exit_status` and `num_events` are
forwarded to the service, which no longer waits for the start document's
`num_points`.
