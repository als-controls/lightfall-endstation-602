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
   | `sm3_pitch` (SM3 Mirror Pitch) | native units as logged | about 98 to 116 | 90..125 |
   | `sample_x` (Sample X) | mm | about -0.95 to -0.65 | -1.5..0.5 |

   For any other motor, read its position and limits with `lightfall_read_device` and
   ask. Keep the motor order fixed from here on: it is the axis order of
   `parameter_bounds` and of the plan's `motors` list.
3. **Budget and cost.** Propose a point budget of about **15 x the number of motors**
   (30 for 2D, 45 for 3D) unless the user gives one. Tell the user the cost before
   starting: about **31 s per point** on the real beamline (exposure, readout, motion),
   so 30 points is about 15 minutes and 45 points about 23 minutes. Get their go-ahead.
4. **Autonomous-experiment workflow.** Load the `autonomous_experiment` skill and follow
   its steps for the design and the tsuchinoko side, with these QERLIN specifics:
   - *Design* (gpCAM `experiment-designer`): the objective is to maximize `rixs_score`;
     the input space is the chosen motors with the agreed bounds. A UCB-style
     acquisition suits optimization (exploit with some exploration); the default
     `variance` acquisition only explores. Use about 2 x N + 3 initial points.
   - *Discover, reset, configure*: `tsuchinoko_discover`, `tsuchinoko_status` /
     `tsuchinoko_stop` until `Inactive`, then `tsuchinoko_configure` with
     `parameter_bounds` in the motor order from step 2.
   - *Pre-flight*: read each motor with `lightfall_read_device` and check it lies inside
     its bounds; if not, ask before moving it or widen the bounds.
5. **Run.** Launch `qerlin_optimize`, **not** `adaptive_experiment`:

   ```
   lightfall_run_plan(
       plan_name="qerlin_optimize",
       params={"marana": "marana", "motors": ["sm3_pitch", "sample_x"],
               "mono": "mono_energy", "exposure_s": 1.0,
               "timeout": 300.0, "score_timeout": 60.0},
       reason="Optimize SM3 pitch and Sample X together with gpCAM (30 points)",
   )
   ```

   `motors` must be in the same order as `parameter_bounds`. `exposure_s` is seconds;
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
   The plan then closes its run after `timeout` without new targets; wait with
   `lightfall_wait_for_idle`.
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
- Always stop tsuchinoko before reconfiguring (see the autonomous_experiment skill), and
  never start a second `qerlin_optimize` or `adaptive_experiment` while one is running.
- Do not report `best_measured` as a fitted optimum; it is the best measured point.
