# QERLIN optimization notes

## Score convention

- rixs-live fits the zeroth-order line in each Marana frame and reports its FWHM in
  pixels (`fwhm_px`) and whether the fit is trustworthy (`score_valid`).
- `qerlin_optimize` turns that into `rixs_score` = 100 / `fwhm_px` when valid, else 0,
  and writes it, with `rixs_fwhm_px` (NaN when invalid), to the run's `primary`
  stream. tsuchinoko reads `rixs_score` as the measured value and gpCAM maximizes it.
- At best focus QERLIN's line is about 5 px FWHM, a score of about 20. A 10 px line
  scores 10, a 20 px line 5.
- Why 100 and not 1: tsuchinoko tells gpCAM each measurement with a fixed variance of
  1.0. On a 1/FWHM scale (0.04 to 0.2) that is pure noise, the GP fits a flat model and
  gpCAM only explores the bounds.
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
tsuchinoko's own engine (GPCAMInProcessEngine, configured through its NATS configure
handler, every measurement told with variance 1.0 as TiledReader does) against the sim
focus model, 30 points per run, SM3 pitch 98..116 x Sample X -0.95..-0.65 mm:

| Design | GP-max FWHM median (32 runs) | Runs within 5.5 px | Repeated targets |
|---|---|---|---|
| bounds only (tsuchinoko defaults, 1/FWHM), 20 runs | 17.9 px | - | 189 |
| this file (ucb, matern_3_2, 100/FWHM) | 5.10 px | 94 % | 6 |

The best possible line is 5.1 px. For the default hidden optimum (pitch 104.0, Sample X
-0.80 mm) it lands within 0.2 pitch units and 0.001 mm in 5 of 6 random starts; the
sixth settles on the lopsided pitch ridge at about 107 (6.7 px), which the confirming
exposure would show.
