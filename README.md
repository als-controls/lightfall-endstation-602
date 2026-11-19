# lightfall-endstation-602

Lightfall integration for the ALS beamline 6.0.2 (QERLIN) endstation.

- `plugin/` - `lightfall-endstation-602` (import `lightfall_endstation_602`): Lightfall plugin with sim devices and UI.
- `rixs-live/` - `rixs-live` (import `rixs_live`): headless NATS-bound analysis service wrapping the QERLIN RIXS code.

The two packages share a small versioned NATS contract (`contract.py`), duplicated in each.
The service never imports the plugin at runtime.

## Distribution

Both packages are released under the Lightfall license (BSD-3-Clause, LBNL); see
`LICENSE.md` and `LEGAL.md`.

`rixs-live` depends on QERLIN's analysis code, `mRIXS-super-app` (imported as
`rixs_app`). That code is not part of this repository and is not covered by its
license; it is installed separately (see Setup).

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
rixs-live's tests also import the plugin's simulator and contract from
`../plugin/src` (set as `pythonpath` in its pytest config), so run them from this
checkout; the plugin does not need to be installed in the service venv.

## What the plugin contributes

- **Devices** (`qerlin_sim` backend): `marana` (2048x2048 frames), `sm3_pitch`,
  `sample_x` (mm), `mono_energy` (eV). See the table in the live demo checklist.
- **Plans** (category `rixs`):
  - `qerlin_focus_scan(marana, motor, mono, start, stop, num, exposure_s)`: a 1D
    focus scan along any motor; rixs-live fits FWHM vs position.
  - `qerlin_optimize(marana, motors, mono=None, exposure_s, timeout, score_timeout)`:
    N-D optimization (N >= 1 motors) driven by tsuchinoko/gpCAM. Each target gets one
    frame in the `frames` stream; rixs-live scores it and the plan writes
    `rixs_score` (1/FWHM px, 0 when unscorable) and `rixs_fwhm_px` with the motors to
    `primary`, which tsuchinoko reads. Three unscored frames in a row fail the run.
    It shows Lightfall's adaptive-plan panel (Stop/Pause).
- **Panel**: "QERLIN Focus" (Analysis).
- **Agent tools**: `qerlin_focusing` (`rixs_service_status`, `get_focus_result`, the
  `qerlin_focusing` skill) and `qerlin_optimization` (skill only).
- **Agent**: `qerlin` (`agents/qerlin.md`), with the `qerlin_focusing`,
  `qerlin_optimization`, `autonomous_experiment` and `plan_review` skills and
  Lightfall's `autonomous_experiment` (tsuchinoko) tools.

## Real Marana: frames over PVA

The simulated Marana stores its frames in Tiled, and rixs-live reads them from
there. The real Marana (`BL602:marana01:`) writes each run to an HDF5 file on
602marana. Tiled can only serve that file after shussebora has copied it to
bcgtiled at the end of capture, which is too late to score points while the scan
runs. So for the real camera rixs-live takes the **pixels from the IOC's `Pva1`
plugin** (an NTNDArray channel) and keeps reading **positions and `seq_num` from
Tiled**, where scalar columns land live.

- The plans add `pva_channel` (from `Pva1:PvName_RBV`, else `<prefix>Pva1:Image`)
  and `frame_counter_field` (`marana_array_counter`) to the `rixs` start-doc block
  whenever the detector has a `pva1` plugin. The sim has none, so nothing changes
  for it.
- Row to frame: each event reads `cam1:ArrayCounter_RBV` with a fresh CA get. The
  driver stamps every NDArray with `uniqueId` = that counter. Staging puts Pva1 on
  blocking callbacks, so the frame is posted before the acquisition completes.
  rixs-live keeps the last `pva.buffer_frames` frames per channel and pairs each
  row with the frame whose `uniqueId` matches its counter.
- A row whose frame never arrives fails the run loudly on `rixs.error` after
  `pva.frame_timeout_s` (default 20 s). The message names the channel, its
  connection state and the buffered ids. A frame stamped well before the run
  started (the counter reset after an IOC restart) is treated as not received.
- Compressed or colour NTNDArrays are refused; leave Pva1's compression off.

Run the service with the PVA settings for the IOC host:

```
rixs-live run --nats-url ... --lightfall-prefix ... \
  --pva-addr-list 602marana.dhcp.lbl.gov \
  --pva-channel BL602:marana01:Pva1:Image
```

`--pva-addr-list` turns broadcast search off, since the IOC is on another subnet.
`--pva-channel` starts the monitor when the service starts, so the first frame of
the first run is buffered even while the bind is still looking the run up in Tiled.
Both can also go in the YAML config (`pva:` with `addr_list`, `auto_addr_list`,
`channels`, `buffer_frames`, `frame_timeout_s`, `stale_slack_s`). The host running
rixs-live must reach 602marana on PVA (TCP 5075; UDP 5076 for search). The CSM
firewall group for 6.0.2 IOCs has to allow that.

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
   qerlin_sim devices: ['marana', 'mono_energy', 'sm3_pitch']   # sample_x added since
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
   **Proxy.** Nothing to configure: each bind also carries `tiled_proxy`, the
   proxy Lightfall itself uses for that Tiled URL (Preferences -> Network Proxy, e.g.
   `socks5://localhost:1080` for `*.lbl.gov`), and the service connects through
   it. When the bind carries no proxy, `ALL_PROXY` / `HTTPS_PROXY` in the
   service's environment are still honoured (socksio is installed for SOCKS). If
   the service cannot reach Tiled, the panel's status line shows the `rixs.error`
   text, which names the Tiled URL and the proxy used (or "no proxy").
   The service reconnects to NATS on its own after a broker restart; if the
   connection is ever closed for good it exits with status 1 so a supervisor
   can restart it.
   An empty `app_version` in `rixs_service_status` is expected for an editable install.
3. **Backend.** Start Lightfall and enable the `qerlin_sim` device backend
   (Settings -> Devices; the preference is `device_plugin_qerlin_sim_enabled`). Check that
   these appear:

   | Device | Units | Limits | Start | Hidden optimum (`LIGHTFALL_SIM_SEED`) |
   |---|---|---|---|---|
   | `sm3_pitch` | native (as logged) | 90..125 | 105 | 100..114 (unset: 104.0) |
   | `sample_x` | mm | -1.5..0.5 | -0.85 | -0.85..-0.70 (unset: -0.80) |
   | `mono_energy` | eV | 0..5000 | 850 | |
   | `marana` | 2048x2048 uint16 frames | | | |

   The sim line width depends on both motors and is calibrated to QERLIN's real scans
   (about 5 px FWHM at best; pitch +12 -> ~18.5 px, -6 -> ~8.5 px; Sample X +-0.1 mm ->
   ~15 px). The best Sample X shifts by 0.005 mm per pitch unit away from the best pitch
   (a guess, not measured), so Sample X needs re-checking after a pitch change.
4. **Panel.** Open the "QERLIN Focus" panel (Analysis). Its title-bar toggle
   is on by default; the status line should read "bound: waiting for a
   qerlin_focus_scan run" once the RunEngine is up. The right-hand health
   indicator checks rixs-live every ~10 s (off the GUI thread): "service: not
   responding" means discovery got no answer, and a scan started meanwhile reads
   "run <uid> not analysed: rixs-live not responding". `rixs_service_status`
   reports the same state.
5. **Plan from the UI.** Run `qerlin_focus_scan` once (devices marana, motor
   = `sm3_pitch`, mono; defaults 98..116, 19 points). The left plot fills with
   FWHM vs position, its x axis labelled with the motor name and units from the
   service (`sm3_pitch`, or `sample_x (mm)`), the right shows the zeroth-order
   preview with its fitted line, and on `rixs.zeroth.complete` the fitted
   parabola and a vertical line at the optimum appear. Then scan `sample_x`,
   e.g. -0.92..-0.80 mm in 13 points.
6. **Agent.** Open the "qerlin" agent in the assistant panel and ask it to
   focus the spectrometer. It checks `rixs_service_status`, then, like QERLIN,
   focuses SM3 pitch first and Sample X second (coarse then fine each), reading
   `get_focus_result` after every scan, moving each motor to its optimum and
   re-checking Sample X after any pitch change.

### N-D optimization (gpCAM via tsuchinoko)

To optimize several motors at once (e.g. SM3 pitch and Sample X together), gpCAM picks
each point inside tsuchinoko, `qerlin_optimize` measures it, and rixs-live scores the
frame; gpCAM maximizes `rixs_score` = 1/FWHM px.

1. Do steps 1-4 of the checklist above (NATS, rixs-live, backend, panel) with the same
   broker and topic prefix everywhere. Lightfall needs gpCAM installed (with its
   skills) for the agent's design step.
2. **Start tsuchinoko** on the same broker and prefix, from its own venv:
   `C:/Users/rp/PycharmProjects/tsuchinoko/.venv/Scripts/tsuchinoko run --nats-url nats://127.0.0.1:4222 --lightfall-prefix <ipc_topic_prefix>`.
   It reads the run's `primary` stream from the same Tiled that Lightfall writes to
   (URL and key come in the plan's `bind_run`).
3. **Ask the qerlin agent**, e.g. "optimize SM3 pitch and Sample X together with
   gpCAM". It loads the `qerlin_optimization` skill: it checks `rixs_service_status`,
   asks which motors and bounds (suggesting SM3 pitch ~98-116 and Sample X ~-0.95 to
   -0.65 mm), proposes a budget of ~15 points per motor at ~31 s per point, designs the
   GP with gpCAM's skills, resets and configures tsuchinoko, checks the motors are in
   bounds, and launches `qerlin_optimize` (not `adaptive_experiment`). When the budget
   is reached or the optimum settles it stops tsuchinoko, moves to the best point and
   confirms it with a single-exposure `qerlin_focus_scan`.
4. Watch progress in the adaptive visualization widgets and the QERLIN Focus panel. The
   service's result for the run (`get_focus_result`) has no 1D fit ("multi-motor run:
   no 1D fit"); `best_measured.positions` holds the best measured point.

Run it by hand from the Plans panel or `lightfall_run_plan` with
`{"marana": "marana", "motors": ["sm3_pitch", "sample_x"], "mono": "mono_energy"}`
after configuring tsuchinoko (the `parameter_bounds` axes in the same order as
`motors`). The plan refuses to start until the rixs binder is enabled (open the QERLIN
Focus panel or call `rixs_service_status`).

Headless check: `rixs-live/tests/test_e2e_optimize.py` runs the plan over a real
nats-server, in-process Tiled and rixs-live, with a fake tsuchinoko that reads
`rixs_score` back from `primary` the way tsuchinoko's TiledReader does.

An aborted or failed scan completes as soon as its rows stop arriving
(`drain_settle_s`): the stop document's `exit_status` and `num_events` are
forwarded to the service, which no longer waits for the start document's
`num_points`.
