# RIXS terminology for QERLIN focusing

## Instrument

- **RIXS**: resonant inelastic X-ray scattering. QERLIN is the imaging RIXS spectrometer on
  ALS beamline 6.0.2; the Andor **Marana** CMOS camera records the dispersed signal.
- **Zeroth order (m = 0)**: the undispersed specular beam on the detector. It is the
  sharpest feature and the energy reference, so its width is what we focus on.
- **SM3 mirror pitch** (`sm3_pitch`, mrad, limits +-3): the mirror angle that sets focus
  on the detector. Other focusing variables exist (sample X, slits) but this workflow
  scans SM3 only.
- **mono energy** (`mono_energy`, eV): incident photon energy; needed for resolving power.

## Metrics

- **FWHM**: full width at half maximum of the zeroth-order line, from a Gaussian fit along
  the dispersion direction. Reported in pixels (`fwhm_px`) and, when the dispersion is
  known, in meV (`fwhm_mev = fwhm_px * dispersion_mev_per_px`).
- **Resolving power**: R = E / dE, with E the mono energy and dE the FWHM in energy. It needs
  both a nonzero dispersion and a mono energy. Without them report px only; never guess.
- **Focus curve**: FWHM versus SM3 pitch. Near focus it is approximately a parabola, so the
  rixs-live service fits a quadratic and takes its vertex as `optimal_position`. That fitted
  vertex has sub-step precision and is the number to report, while noting the lowest single
  measured point (`best_measured`).
- **score_valid**: false means the line fit at that point failed or was unusable; the point
  is excluded from the focus fit.

## Pipeline (context)

The full reduction chain is: dark/pixel masking, single-photon clustering and gating, drift
correction and summing, zeroth-order focusing, energy calibration, spectra extraction.
Focusing runs at every sample change; the other steps are offline and out of scope here.

## Working habit: inspect, explain, then run

1. Inspect first: service status, device positions and limits, the plan's signature.
2. Explain what you will do, the side effects (the scan moves SM3 and takes frames) and the
   numbers you chose (range, `num`, exposure).
3. Then run it through the plan tools with an explicit `reason`, and report what came back.
