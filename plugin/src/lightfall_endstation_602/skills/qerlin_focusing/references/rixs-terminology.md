# RIXS terminology for QERLIN focusing

## Instrument

- **RIXS**: resonant inelastic X-ray scattering. QERLIN is the imaging RIXS spectrometer on
  ALS beamline 6.0.2; the Andor **Marana** CMOS camera records the dispersed signal.
- **Zeroth order (m = 0)**: the undispersed specular beam on the detector. It is the
  sharpest feature and the energy reference, so its width is what we focus on.
- **SM3 mirror pitch** (`sim_sm3_pitch` in simulation, `sm3_mirror_pitch` on BCS; native units as logged at the beamline, limits
  90..125; QERLIN's best values were about 104-113): the mirror angle that sets focus on
  the detector. FWHM grows faster above the optimum than below it.
- **Sample X** (`sim_sample_x` in simulation; mm, limits -1.5..0.5; QERLIN's best values were about
  -0.82 and -0.72): sample position along the beam; the line broadens by roughly 150 px
  per mm away from best. Its optimum can move when SM3 pitch changes, so re-check it
  after every pitch change.
- Other focusing variables exist (slits, ...); the focus scan takes any single motor.
- **mono energy** (`sim_mono_energy` in simulation, `mono_energy` on BCS; eV): incident photon energy; needed for resolving power.

## Metrics

- **FWHM**: full width at half maximum of the zeroth-order line, from a Gaussian fit along
  the dispersion direction. Reported in pixels (`fwhm_px`) and, when the dispersion is
  known, in meV (`fwhm_mev = fwhm_px * dispersion_mev_per_px`).
- **Resolving power**: R = E / dE, with E the mono energy and dE the FWHM in energy. It needs
  both a nonzero dispersion and a mono energy. Without them report px only; never guess.
- **Focus curve**: FWHM versus the scanned motor position (SM3 pitch or Sample X). Near focus it is approximately a parabola, so the
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
2. Explain what you will do, the side effects (the scan moves the chosen motor and takes
   frames) and the numbers you chose (motor, range with units, `num`, exposure).
3. Then run it through the plan tools with an explicit `reason`, and report what came back.
