# QERLIN optimization notes

## Score convention

- rixs-live fits the zeroth-order line in each Marana frame and reports its FWHM in
  pixels (`fwhm_px`) and whether the fit is trustworthy (`score_valid`).
- `qerlin_optimize` turns that into `rixs_score` = 1 / `fwhm_px` when valid, else 0,
  and writes it, with `rixs_fwhm_px` (NaN when invalid), to the run's `primary`
  stream. tsuchinoko reads `rixs_score` as the measured value and gpCAM maximizes it.
- At best focus QERLIN's line is about 5 px FWHM, a score of about 0.2. A 10 px line
  scores 0.1, a 20 px line 0.05.
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
