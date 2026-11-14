# QERLIN optimization notes

## Score convention

- rixs-live fits the zeroth-order line in each Marana frame and reports its FWHM in
  pixels (`fwhm_px`) and whether the fit is trustworthy (`score_valid`).
- `qerlin_optimize` turns that into `rixs_score` = 1 / `fwhm_px` (QERLIN's own
  convention) when valid, else 0,
  and writes it, with `rixs_fwhm_px` (NaN when invalid), to the run's `primary`
  stream. tsuchinoko reads `rixs_score` as the measured value and gpCAM maximizes it.
- At best focus QERLIN's line is about 5 px FWHM, a score of about 0.2. A 10 px line
  scores 0.1, a 20 px line 0.05.
- tsuchinoko passes no measurement variance, so gpCAM applies its default noise,
  (1 % of mean |score|)^2. Do not configure a noise setting (see below).
- A frame the service does not score within `score_timeout` also records 0 (logged).
  Three such frames in a row fail the run: the service is probably not running.

## Data layout of a `qerlin_optimize` run

- `frames` stream: the Marana image plus every motor, one row per point. rixs-live
  reads the images from here (the start document's `rixs.image_stream`).
- `primary` stream: every motor plus `rixs_score` and `rixs_fwhm_px`, one row per
  point, in the same order. tsuchinoko (TiledReader) reads this stream.
- Point *i* is `seq_num` *i* in both streams.
- rixs-live's result for the run (`get_focus_result`): `motor_fields`, per-point
  `positions`, `best_measured.positions`; `fit.ok` is false ("multi-motor run: no 1D fit").

## Cost model

About 31 s per point on the beamline (motion, exposure, readout and scoring). Budget
about 15 points per motor:

| Motors | Points | Time |
|---|---|---|
| 1 | 15 | ~8 min (prefer the 1D `qerlin_focusing` scans) |
| 2 | 30 | ~15 min |
| 3 | 45 | ~23 min |

## Known ranges (suggested bounds)

| Motor | Units | Bounds | Notes |
|---|---|---|---|
| `sm3_pitch` | native (as logged) | ~98..116 | lopsided: width grows faster above the optimum |
| `sample_x` | mm | ~-0.95..-0.65 | its best moves slightly with pitch, which is why a joint 2D search helps |

## Tested gpCAM configuration

`gpcam-configure.json` (also inlined in SKILL.md step 4) was validated offline with
tsuchinoko's own engine (GPCAMInProcessEngine with `update_measurements` replacing
gpCAM's data, `tell(append=False)`, and no measurement variance, as TiledReader now
reads it), configured through its NATS configure handler, against the sim focus model:
30 points per run, SM3 pitch 98..116 x Sample X -0.95..-0.65 mm, `rixs_score` = 1/FWHM
with FWHM^2 = 5.56 sigma^2 + 13.5 (the scorer calibration). 48 runs per row, each with
its own random start: 24 at the default hidden optimum (seed None: pitch 104.0,
Sample X -0.80 mm) and 24 over sim seeds 1-8 (best pitch 101.2 to 113.2). "Best FWHM"
is the true FWHM at the best measured point; "jitter" adds Gaussian scatter of that
relative size to each measured FWHM (the sim scorer itself scatters by under 0.1 %).

| Design | Jitter | Best FWHM median | p90 | Runs within 5.5 px | Repeated targets |
|---|---|---|---|---|---|
| **this file** | 0 | **5.10 px** | 5.10 | **100 %** | 1 |
| this file | 2 % | 5.13 px | 5.23 | 98 % | 6 |
| this file | 5 % | 5.21 px | 5.45 | 94 % | 0 |

The best possible line is 5.1 px. Without jitter, seed None lands within 0.06 pitch
units and 0.0005 mm (median) of the optimum in all 24 starts. Trained hyperparameters
settle near [0.0056, 12.3, 0.12] (signal variance, pitch, Sample X mm), well inside the
bounds. gpCAM's default noise is enough: no `noise_variances` or `noise_function` is
needed. In 32-run checks, variants (initial length scales 4.0 / 0.08 mm, matern_5_2,
5 initial points, milestones from 8) all reached 100 % within 5.5 px without jitter and
91 to 100 % with 5 % jitter; none was clearly better, and matern_5_2 repeated targets
more (41 in 32 runs without jitter).

Before tsuchinoko's fix (fixed variance 1.0 on every point, history appended again on
each update, so `initial_points` and milestones counted k(k+1)/2 rows) the same search
needed `rixs_score` = 100/FWHM to work at all; 1/FWHM with tsuchinoko's defaults
reached 17.9 px median with 189 repeated targets.
