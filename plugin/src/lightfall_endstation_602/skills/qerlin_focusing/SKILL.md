---
name: qerlin-focusing
description: "Closed-loop focusing on the QERLIN (ALS 6.0.2) RIXS spectrometer along any motor (SM3 mirror pitch, then Sample X): coarse scan, fit, fine scan, move, confirm"
---

## QERLIN focusing (BL 6.0.2)

Find the motor position that minimizes the zeroth-order line width on the Marana
detector. The scan is a normal Lightfall plan; the fit is done by the headless rixs-live
service and read back with `get_focus_result`. Focusing works along any single motor; the
scan is always 1D (no 2D scans).

**See `references/rixs-terminology.md`** for the terms (zeroth order, FWHM, resolving
power) and the inspect-explain-run habit.

Devices: `marana` (detector), `mono_energy` (eV) and the focusing motors:

| Motor | Units | Limits | Coarse scan (QERLIN practice) |
|---|---|---|---|
| `sm3_pitch` (SM3 Mirror Pitch) | native units as logged | 90..125 | about 98 to 116, step ~1 (19 points) |
| `sample_x` (Sample X) | mm | -1.5..0.5 | a 0.12-0.15 mm span around the current position, step 0.01 (13-16 points) |

Plan: `qerlin_focus_scan(marana, motor, mono, start, stop, num, exposure_s)`; its device
arguments are device names, `start`/`stop` are in the scanned motor's units, `exposure_s`
is seconds. Use `motor="sm3_pitch"` unless told otherwise. `get_focus_result` returns
`motor_field` and `motor_units` (empty for SM3 pitch's native units).

### Order of motors

Current QERLIN practice, from their 2026 zeroth-order scans:

1. **SM3 Mirror Pitch first**, coarse then fine.
2. **Then Sample X**, coarse then fine, at the new pitch.
3. **Re-check Sample X after any pitch change.** The best Sample X can move when the pitch
   changes, so a Sample X optimum found at an old pitch is stale. If you change pitch again
   later, run another Sample X scan before reporting.

Tell the user this order before you start, and say which motor each scan moves.

### Procedure (for each motor)

1. **Service check.** Call `rixs_service_status`. If `service_up` is false, stop and tell
   the user (quote the `hint`); do not scan, since nothing would analyze the frames. If
   `binder_enabled` is false, also stop and tell the user: scans would run but would not be
   sent to the service for analysis (usually the RunEngine is not up yet). Note
   `dispersion_mev_per_px`. One check per session is enough unless a scan goes unanalysed.
2. **Coarse scan.** Read the motor's current position and limits, tell the user the motor,
   range and point count, then call `lightfall_run_plan("qerlin_focus_scan", {...})` with
   an explicit `reason`. Use the coarse ranges in the table above (SM3 pitch about 98-116
   step ~1; Sample X a 0.12-0.15 mm span step 0.01, centred on its current position).
   Choose `num` deliberately: it trades agent turns against scan points. At least 5 points
   are needed in practice (the fit needs 3 valid points and a concave-up curve).
3. **Read the fit.** Wait for the run to finish with `lightfall_wait_for_idle`, then call
   `get_focus_result`, passing `run_uid` when the plan-run or wait tool gave you the run's
   uid (otherwise it defaults to the last bound focus scan). The service finishes scoring
   a few seconds after the RunEngine goes idle; `get_focus_result` waits for that analysis
   (up to `wait_s`, default 60 s) rather than returning an older run's fit. If it reports
   the analysis is still running, call it again; do not fall back to an earlier result.
   - If `fit.ok`: run a fine scan of +-3x the coarse step around `fit.optimal_position`
     (for example SM3 pitch +-3 at step 0.5, Sample X +-0.03 mm at step 0.005), clamped to
     the motor's limits, again with an explicit `reason`, then wait for idle and call
     `get_focus_result` on the new run. The SM3 pitch curve is lopsided (steeper above the
     optimum), so a wide scan's parabola vertex sits slightly low; the fine scan fixes that.
   - If not ok: say why, quoting `fit.reason` verbatim ("fewer than 3 valid points",
     "curve not concave-up (no minimum)", "optimum outside scanned range"). Then widen the
     range, or re-center a new scan around `best_measured.position`, and repeat. Stop and ask
     the user after two failed attempts on the same motor.
4. **Move.** With the device tools, move the motor to the final optimum (the fine-scan
   `fit.optimal_position` if ok). Never move outside the motor's limits.
5. **Confirm.** Run one more exposure with `num=1` and `start == stop ==` the optimum, with
   an explicit `reason`. Wait for idle and read it with `get_focus_result`; its single
   point is the confirming FWHM (the fit will not be ok for one point, which is expected).
   At best focus QERLIN's line is about 5 px FWHM.
6. **Next motor.** After SM3 pitch, do Sample X (steps 2-5). After a pitch change, always
   re-check Sample X.
7. **Report.** For each motor give the optimum with its units (SM3 pitch in native units,
   Sample X in mm), then the final confirming FWHM in px and meV, and resolving power R.
   If `dispersion_mev_per_px` is 0 or unknown, or the mono energy is missing, report FWHM
   in px only and say plainly that resolving power is unavailable; never estimate it.
   Mention any point with `score_valid` false.

### Rules

- Every plan submission needs an explicit `reason`; approvals are the user's call.
- Scan one motor at a time; no 2D scans.
- Do not report an optimum that the fit did not produce; `best_measured` is the lowest
  measured FWHM, not a fitted optimum.
- Keep the exposure and mono energy the same across the coarse, fine and confirming scans.
