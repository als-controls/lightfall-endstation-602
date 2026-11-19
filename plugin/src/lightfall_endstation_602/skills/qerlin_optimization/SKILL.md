---
name: qerlin-optimization
description: "N-D focusing on the QERLIN (ALS 6.0.2) RIXS spectrometer with gpCAM via tsuchinoko: 2-3 motors optimized together (e.g. SM3 pitch + Sample X), rixs-live scores each frame, gpCAM picks the next point"
---

## QERLIN N-D optimization (BL 6.0.2)

Optimize two or more focusing motors **together**. gpCAM (running inside tsuchinoko)
picks each next point, the `qerlin_optimize` plan moves there and takes one Marana
frame, and rixs-live scores that frame. The objective is **maximize `rixs_score`**
= 1 / FWHM (px) of the zeroth-order line, so a sharper line scores higher. A frame the
service cannot score (no line, too broad) has score 0.

For a single motor, use the `qerlin_focusing` skill (coarse/fine 1D scans and a parabola
fit) instead: it is cheaper and gives a fitted optimum. Use this skill when the user asks
to optimize 2D/3D, or several motors at once.

**See `references/optimization-notes.md`** for the score convention, the cost model and
the defaults table.

### Procedure

1. **Service check.** Call `rixs_service_status`. If `service_up` is false, stop and tell
   the user (quote the `hint`). If `binder_enabled` is false, stop too: the run would
   never be scored (and `qerlin_optimize` refuses to start). Calling this tool is also
   what enables the binder, so always call it first in a session.
2. **Motors and bounds.** Ask the user which motors to optimize and the bounds of each,
   in that motor's units. Nothing is fixed: any motors work, in any number (N >= 1).
   Suggest these defaults from QERLIN's known ranges when the user has none:

   | Motor | Units | Suggested bounds | Limits |
   |---|---|---|---|
   | `sim_sm3_pitch` (SM3 Mirror Pitch) | native units as logged | about 98 to 116 | 90..125 |
   | `sim_sample_x` (Sample X) | mm | about -0.95 to -0.65 | -1.5..0.5 |

   These are the simulated motors (`sim_` prefix); real devices have no prefix and may
   be offline.

   For any other motor, read its position and limits with `lightfall_read_device` and
   ask. Keep the motor order fixed from here on: it is the axis order of
   `parameter_bounds` and of the plan's `motors` list.
3. **Budget and cost.** Propose a point budget of about **15 x the number of motors**
   (30 for 2D, 45 for 3D) unless the user gives one. Tell the user the cost before
   starting: about **31 s per point** on the real beamline (exposure, readout, motion),
   so 30 points is about 15 minutes and 45 points about 23 minutes. Get their go-ahead.
4. **Configure tsuchinoko BEFORE the plan starts.** Load the `autonomous_experiment`
   skill for the general workflow, but use this tested QERLIN design instead of designing
   one from scratch. Unconfigured, tsuchinoko runs its default design (variance-only
   acquisition, default hyperparameters): it only explores, proposes points on the bounds
   and repeats them, and never converges. So, in this order, every time:

   1. `tsuchinoko_discover()`; if empty, stop and ask the user to start tsuchinoko.
   2. `tsuchinoko_status()`. Unless `state` is `Inactive`, call `tsuchinoko_stop()` and
      poll `tsuchinoko_status()` until `state == "Inactive"` (configure does not reset
      data; the stop does).
   3. `tsuchinoko_configure(payload=...)` with this payload (also in
      `references/gpcam-configure.json`), for `sim_sm3_pitch` then `sim_sample_x`:

      ```json
      {
        "parameter_bounds": [[98.0, 116.0], [-0.95, -0.65]],
        "acquisition_function": "ucb",
        "kernel": "matern_3_2",
        "initial_points": 7,
        "hyperparameters": [0.005, 8.0, 0.1],
        "hyperparameter_bounds": [[2.5e-5, 0.25], [1.0, 20.0], [0.01, 0.3]],
        "training_method": "global",
        "global_training": [6, 10, 15, 20, 25]
      }
      ```

      It must return `success: true`; if not, fix the payload and retry (stop first).
      No `tsuchinoko_upload_design_code` is needed: everything here is built in.
   4. *Pre-flight*: read each motor with `lightfall_read_device` and check it lies inside
      its bounds; if not, ask before moving it, or widen the bounds and redo 2-3.
   5. Only now launch the plan (step 5).

   What the payload means, and how to adapt it (keep the motor order everywhere):
   - `parameter_bounds`: the bounds agreed in step 2, one `[lo, hi]` per motor.
   - `acquisition_function: "ucb"`: posterior mean + 3 sigma, maximized; balances
     exploiting the sharpest region with exploring uncertain ones (`variance` only
     explores).
   - `kernel: "matern_3_2"`, anisotropic: `hyperparameters` = [signal variance, one
     length scale per motor]. 0.005 ~ (0.07 score units)^2 suits `rixs_score` = 1/FWHM px
     (about 0.04 to 0.2). Length scales: about half of each motor's bound span (8.0 for
     an 18-unit pitch span, 0.1 mm for a 0.3 mm Sample X span; training settles near
     12 and 0.12 mm on the sim); `hyperparameter_bounds` per length scale about
     [span/20, span] (pitch [1, 20], Sample X [0.01, 0.3] mm). For other bounds or a
     third motor, add one length scale and one bound pair per motor by the same rule;
     the signal-variance entries stay [0.005] and [2.5e-5, 0.25].
   - `initial_points: 7` random points first (2 x N + 3; use 9 for 3 motors).
   - `training_method: "global"` with `global_training` milestones: hyperparameters are
     re-fit once the point count exceeds each milestone (6: right after the random
     points). For 3 motors, start the milestones at 8.
   - Noise: send neither `noise_function` nor `noise_variances`. tsuchinoko passes no
     measurement variance, so gpCAM uses its own default ((1 % of mean score)^2); the
     validation held up with 2-5 % FWHM scatter on top (see the notes).
   - Repeats: tsuchinoko has no setting against re-measuring a point. `qerlin_optimize`
     drops tsuchinoko's re-sent copies of a target it is already measuring; gpCAM may
     still ask for the same point twice near the optimum, which is fine.
5. **Run.** Launch `qerlin_optimize`, **not** `adaptive_experiment`:

   ```
   lightfall_run_plan(
       plan_name="qerlin_optimize",
       params={"marana": "sim_marana", "motors": ["sim_sm3_pitch", "sim_sample_x"],
               "mono": "sim_mono_energy", "exposure_s": 1.0,
               "timeout": 300.0, "score_timeout": 60.0},
       reason="Optimize SM3 pitch and Sample X together with gpCAM (30 points)",
   )
   ```

   Never start it before step 4's configure succeeded. `motors` must be in the same
   order as `parameter_bounds`. `exposure_s` is seconds;
   keep it the same as in any earlier focus scans. `timeout` is how long the plan waits
   for new targets before closing the run; `score_timeout` is how long it waits for
   each frame's score.
6. **Monitor.** Check progress with `tsuchinoko_status` (iteration, `data_count`). Point
   the user at the Visualization panel's adaptive widgets for the posterior. Watch for
   score-0 points (see Rules).
7. **Stop.** Call `tsuchinoko_stop` when either:
   - the point budget is reached (`data_count` >= budget), or
   - the optimum has settled: the best point has not improved for about 5 x N points and
     the posterior uncertainty around it is small (ask the user to confirm from the
     adaptive widgets if unsure).
   `qerlin_optimize` sees the stop (or tsuchinoko going `Inactive` on its own) and closes
   its run within a few seconds; `timeout` is only the fallback if tsuchinoko vanishes.
   Wait with `lightfall_wait_for_idle`.
8. **Read the result.** Call `get_focus_result` (it defaults to the last bound run, which
   is the optimization run). For an N-D run `fit.ok` is false with reason
   "multi-motor run: no 1D fit"; that is expected. `best_measured.positions` holds every
   motor of the best (lowest FWHM, highest score) measured point, and `points` lists
   each point's `positions`, `fwhm_px` and `score_valid`. Prefer the GP's predicted
   maximum when tsuchinoko or the user reports one inside the bounds and close to good
   measured points; otherwise use `best_measured`.
9. **Move and confirm.** Move every motor to the chosen point with the device tools
   (never outside its limits). Then take one confirming exposure with
   `qerlin_focus_scan`: `motor` = the first motor, `num=1`, `start == stop ==` its
   current position, the same `exposure_s` and an explicit `reason`. Wait for idle and
   read it with `get_focus_result`; its single point is the confirming FWHM.
10. **Report.** Give each motor's final position with units (SM3 pitch native, Sample X
    mm), the confirming FWHM in px and meV, resolving power if `dispersion_mev_per_px`
    and the mono energy are known (else say it is unavailable; never estimate it), how
    many points were measured and how many scored 0.

### Rules

- Every plan submission needs an explicit `reason`; approvals are the user's call.
- Never hard-code the motors: ask, then use the user's choice and order everywhere.
- **Score-0 points** are frames rixs-live could not score. A few near the edges of the
  bounds are normal. If more than about a third of the points so far scored 0, the
  bounds are too wide or the beam is off: call `tsuchinoko_stop`, tell the user, and
  propose narrower bounds (or check the beam) before trying again.
- If the run fails with "rixs-live scored none of the last 3 frames", the service is down
  or not bound: call `rixs_service_status`, tell the user, and do not restart until it
  is up.
- Always configure tsuchinoko (step 4) before `qerlin_optimize`, and always stop it
  (until `Inactive`) before reconfiguring; and
  never start a second `qerlin_optimize` or `adaptive_experiment` while one is running.
- Do not report `best_measured` as a fitted optimum; it is the best measured point.
