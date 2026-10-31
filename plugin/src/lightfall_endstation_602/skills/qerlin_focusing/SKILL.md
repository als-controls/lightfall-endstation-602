---
name: qerlin-focusing
description: "Closed-loop SM3 mirror-pitch focusing on the QERLIN (ALS 6.0.2) RIXS spectrometer: coarse scan, fit, fine scan, move, confirm"
---

## QERLIN focusing (BL 6.0.2)

Find the SM3 mirror pitch that minimizes the zeroth-order line width on the Marana
detector. The scan is a normal Lightfall plan; the fit is done by the headless rixs-live
service and read back with `get_focus_result`.

**See `references/rixs-terminology.md`** for the terms (zeroth order, FWHM, resolving
power) and the inspect-explain-run habit.

Devices: `marana` (detector), `sm3_pitch` (mrad, limits +-3), `mono_energy` (eV).
Plan: `qerlin_focus_scan(marana, sm3, mono, start, stop, num, exposure_s)`; its device
arguments are device names, `start`/`stop` are SM3 pitch in mrad, `exposure_s` is seconds.

### Procedure

1. **Service check.** Call `rixs_service_status`. If `service_up` is false, stop and tell
   the user (quote the `hint`); do not scan, since nothing would analyze the frames. If
   `binder_enabled` is false, also stop and tell the user: scans would run but would not be
   sent to the service for analysis (usually the RunEngine is not up yet). Note
   `dispersion_mev_per_px`.
2. **Coarse scan.** Tell the user the range and point count, then call
   `lightfall_run_plan("qerlin_focus_scan", {...})` across the SM3 range of interest
   (default -1 to 1 mrad) with an explicit `reason`. Choose `num` deliberately: it trades
   agent turns against scan points. A small `num` (5-9) leaves refinement to you; a large one
   gives a dense scan. At least 5 points are advisable (the fit needs 3 valid points and a
   concave-up curve).
3. **Read the fit.** Wait for the run to finish with `lightfall_wait_for_idle`, then call
   `get_focus_result`, passing `run_uid` when the plan-run or wait tool gave you the run's
   uid (otherwise it defaults to the last bound focus scan). The service finishes scoring
   a few seconds after the RunEngine goes idle; `get_focus_result` waits for that analysis
   (up to `wait_s`, default 60 s) rather than returning an older run's fit. If it reports
   the analysis is still running, call it again; do not fall back to an earlier result.
   - If `fit.ok`: run a fine scan of +-3x the coarse step around `fit.optimal_position`
     (clamped to +-3 mrad), again with an explicit `reason`, then wait for idle and
     call `get_focus_result` on the new run.
   - If not ok: say why, quoting `fit.reason` verbatim ("fewer than 3 valid points",
     "curve not concave-up (no minimum)", "optimum outside scanned range"). Then widen the
     range, or re-center a new scan around `best_measured.position`, and repeat. Stop and ask
     the user after two failed attempts.
4. **Move.** With the device tools, move `sm3_pitch` to the final optimum (the fine-scan
   `fit.optimal_position` if ok). Never move outside +-3 mrad.
5. **Confirm.** Run one more exposure with `num=1` and `start == stop ==` the optimum, with
   an explicit `reason`. Wait for idle and read it with `get_focus_result`; its single
   point is the confirming FWHM (the fit will not be ok for one point, which is expected).
6. **Report.** Give the optimum pitch (mrad), the confirming FWHM in px and meV, and
   resolving power R. If `dispersion_mev_per_px` is 0 or unknown, or the mono energy is
   missing, report FWHM in px only and say plainly that resolving power is unavailable;
   never estimate it. Mention any point with `score_valid` false.

### Rules

- Every plan submission needs an explicit `reason`; approvals are the user's call.
- Do not report an optimum that the fit did not produce; `best_measured` is the lowest
  measured FWHM, not a fitted optimum.
- Keep the exposure and mono energy the same across the coarse, fine and confirming scans.
