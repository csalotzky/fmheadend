# QN8007B Soft Clipping – Experimental Characterization

**Version:** 1.0  
**Status:** 1 kHz and 10 kHz characterization complete  
**Purpose:** Technical reference for further development of the FM audio processing chain and for future analysis of the QN8007B `TX_SFTCLPEN` behavior.

---

# 1. Executive Summary

Measurements indicate that the QN8007B `Soft Clipping` function is **not a simple transparent ±75 kHz safety limiter**.

With Soft Clip enabled:

1. At low and medium input levels, the effective output level increases by approximately **+4 dB**.
2. Above a frequency-dependent threshold, the transfer characteristic gradually enters **soft saturation / soft clipping**.
3. At stronger drive levels, significant odd-order harmonics appear.
4. Under heavy overdrive, FM peak deviation is constrained to approximately **75–79 kHz**.
5. The clipping threshold clearly follows the frequency-dependent gain of the QN8007B internal **50 µs pre-emphasis**.

The best current functional model is:

> **I²S audio → 50 µs pre-emphasis → approximately +4 dB effective drive → soft saturation / soft clipping → MPX / FM modulation**

This is an **empirical model**. The exact internal transfer function is not documented by the manufacturer.

---

# 2. Test Configuration

The characterization was performed with the following configuration:

- IC: **QN8007B**
- transmitter channel: **TX4**
- audio input: **digital / I²S**
- QN gain control: **MANUAL**
- QN digital gain / trim: **0 dB**
- QN AGC FSM: **not used for gain control**
- custom DSP processing during sine-wave characterization: **bypassed**
  - HPF off
  - LPF off
  - compressor off
  - limiter off
  - EQ off
  - all other dynamics processing off
- sine-wave level adjusted using upstream digital gain
- QN 50 µs pre-emphasis: **enabled and not bypassable**
- `TX_FDEV`: not overwritten by normal firmware configuration; left at its default/reset state during testing

---

# 3. `TXAGC_GAIN` / Register `0x0D` Audit

Firmware inspection shows that for TX4 in MANUAL mode with 0 dB analog gain and 0 dB digital trim:

- Soft Clip OFF: `0x41`
- Soft Clip ON: `0xC1`

XOR:

```text
0x41 XOR 0xC1 = 0x80
```

Therefore, during the Soft Clip ON/OFF comparison, **only bit 7 (`TX_SFTCLPEN`) changes**.

This rules out accidental changes in AGC mode, VGA gain, or digital gain as the cause of the measured level difference.

---

# 4. Confidence Levels

## 4.1 MEASURED

- Soft Clip ON increases the effective output level by approximately **+4 dB** at low and medium signal levels.
- The effective gain decreases progressively above the clipping knee.
- Stronger drive produces significant odd-order harmonics.
- With a 1 kHz, 0 dBFS sine wave, Soft Clip ON limits FM peak deviation to approximately **76–79 kHz**.
- With normal program material, Soft Clip ON raises the main body of the modulation distribution while suppressing the high-deviation tail.
- At 10 kHz, the clipping knee appears approximately **9–10 dB lower in digital input level** than at 1 kHz.

## 4.2 FIRMWARE-AUDITED

- On TX4, only `TX_SFTCLPEN` changes between Soft Clip OFF and ON.
- QN gain control is in MANUAL mode.
- Digital gain / trim is 0 dB.

## 4.3 STRONGLY INFERRED

Based on the 10 kHz measurements:

> **The QN8007B soft-clipping function operates on a signal that already includes the internal 50 µs pre-emphasis, or its control path is derived from such a signal.**

The measured knee shift closely matches the theoretical 1 kHz-to-10 kHz gain difference of a 50 µs pre-emphasis network.

## 4.4 UNKNOWN

The following remain unknown:

- exact mathematical transfer function;
- exact internal block implementation;
- whether the approximately +4 dB effect is a separate gain stage or simply the low-level slope of a nonlinear transfer characteristic;
- any internal attack/release or memory behavior;
- intermodulation performance;
- exact dependency of the final deviation ceiling on `TX_FDEV`.

---

# 5. Program-Material A/B Test

Identical external DSP settings were used in both cases:

- QN MANUAL mode
- QN digital gain 0 dB
- `TX_FDEV` default
- software Overall Gain: +12 dB
- software limiter threshold: −8 dBFS
- limiter lookahead: 5 ms
- attack: 0.25 ms
- release: 20 ms
- identical reference program material

## Soft Clip OFF

- absolute peak: **97.88 kHz**
- P99.9: **46.08 kHz**
- P99.99: **61.15 kHz**
- PIRA-style histogram mode: **46 kHz**
- 50 ms peak average: **47.21 kHz**
- samples >75 kHz: **1547**
- samples >77 kHz: **1280**
- >77 kHz events: **79**
- MPX power: **+0.00 dBr**

## Soft Clip ON

- absolute peak: **77.22 kHz**
- P99.9: **57.05 kHz**
- P99.99: **63.79 kHz**
- PIRA-style histogram mode: **58 kHz**
- 50 ms peak average: **57.09 kHz**
- samples >75 kHz: **44**
- samples >77 kHz: **3**
- >77 kHz events: **2**
- MPX power: **+3.35 dBr**

## Interpretation

With Soft Clip enabled:

- the normal program modulation level increases substantially;
- the high-deviation tail in the 75–100 kHz range is strongly suppressed.

This shows that the function is not merely a last-stage peak catcher.

---

# 6. 1 kHz / 0 dBFS Stress Test

Custom DSP bypassed, QN MANUAL mode, digital gain 0 dB.

## Soft Clip OFF

- peak deviation: **98.83 kHz**
- P99.9: **96.61 kHz**
- P99.99: **97.22 kHz**
- PIRA-style mode: **97 kHz**
- 50 ms peak average: **94.47 kHz**
- MPX power: **+14.04 dBr**
- strong odd-order harmonics visible in the audio spectrum

## Soft Clip ON

- peak deviation: **79.00 kHz**
- P99.9: **76.23 kHz**
- P99.99: **76.97 kHz**
- PIRA-style mode: **77 kHz**
- 50 ms peak average: **74.71 kHz**
- MPX power: **+12.61 dBr**
- very strong odd-order harmonics visible in the audio spectrum

## Interpretation

A 0 dBFS, 1 kHz sine wave is a **stress-test condition**, not a normal operating point.

With Soft Clip enabled, the output reaches an approximately **77 kHz deviation ceiling**, but with substantial nonlinear distortion.

---

# 7. 1 kHz Transfer Characterization

The spectrum levels below were read visually from the SDR Audio Spectrum display and therefore have a few dB of uncertainty. The relative ON/OFF behavior is the relevant result.

| Input level | SC OFF – 1 kHz | SC ON – 1 kHz | Effective ON/OFF difference | Observation |
|---:|---:|---:|---:|---|
| −18 dBFS | ~−23…−24 dB | ~−19…−20 dB | ~+4 dB | linear region, low distortion |
| −12 dBFS | ~−17…−18 dB | ~−13…−14 dB | ~+4 dB | still close to linear |
| −10.5 dBFS | ~−15.5…−16 dB | ~−12 dB | ~+3.5…4 dB | 3rd harmonic ~−43 dB, 5th ~−52 dB |
| −9 dBFS | ~−14…−15 dB | ~−11 dB | ~+3…3.5 dB | clear odd-order harmonics |
| −6 dBFS | ~−11…−12 dB | ~−9…−10 dB | ~+2 dB | strong soft clipping |
| 0 dBFS | heavily overdriven | heavily soft-clipped | nonlinear | ~77–79 kHz deviation ceiling |

## 1 kHz Knee

The approximately +4 dB linear region remains clearly visible down to about **−12 dBFS**.

The onset of measurable soft-clipping behavior is approximately:

> **−12 to −10.5 dBFS**

By approximately −9 dBFS, the nonlinearity is already obvious.

---

# 8. 10 kHz Characterization

The purpose of the 10 kHz test was primarily to determine how the soft-clipping threshold relates to the QN8007B internal 50 µs pre-emphasis.

The 0–16 kHz Audio Spectrum view cannot display the relevant harmonics of a 10 kHz sine:

- 2nd harmonic: 20 kHz
- 3rd harmonic: 30 kHz
- 4th harmonic: 40 kHz

Therefore, the main observable is the **10 kHz fundamental level with Soft Clip OFF versus ON**.

## Results

| Input level | SC OFF – 10 kHz | SC ON – 10 kHz | Effective ON/OFF difference | Observation |
|---:|---:|---:|---:|---|
| −24 dBFS | ~−26 dB | ~−22…−23 dB | ~+3…4 dB | close to linear |
| −21 dBFS | ~−24 dB | ~−20 dB | ~+4 dB | still below the knee |
| −18 dBFS | slightly below −20 dB | ~−18 dB | ~+2 dB | clear compression / above knee |

Small components observed around 8 kHz and 14 kHz at approximately −88 to −95 dB are not harmonics of the 10 kHz test tone.

## 10 kHz Knee

The measurements place the 10 kHz knee approximately between:

> **−21 and −18 dBFS**

A practical estimate is approximately **−20 to −19 dBFS**.

---

# 9. Relationship to 50 µs Pre-Emphasis

For a 50 µs pre-emphasis network:

```text
|H(f)| = sqrt(1 + (2*pi*f*tau)^2)
tau = 50 us
```

Theoretical gain:

- 1 kHz: **~+0.41 dB**
- 10 kHz: **~+10.36 dB**

Difference:

> **~9.95 dB**

Measured knee regions:

- 1 kHz: approximately **−11 dBFS**
- 10 kHz: approximately **−20 to −19 dBFS**

Observed shift:

> **approximately 9–10 dB**

This is in very close agreement with the theoretical **9.95 dB** pre-emphasis difference between 1 kHz and 10 kHz.

## Conclusion

The measurements strongly indicate that:

> **The QN8007B soft-clipping behavior is referenced to a pre-emphasized signal.**

More precisely:

> either the clipping block itself is located after the 50 µs pre-emphasis stage, or the clipping control signal is derived from a signal that already contains the pre-emphasis gain.

From the external system-design perspective, both cases have essentially the same consequence.

---

# 10. Functional Model

Current best-fit external model:

```text
I²S PCM
  |
  v
QN internal 50 µs pre-emphasis
  |
  v
Soft Clip ON: approximately +4 dB effective drive
  |
  v
Soft saturation / soft clipping
  |
  |-- knee at 1 kHz: approximately -12...-10.5 dBFS input
  |-- knee at 10 kHz: approximately -21...-18 dBFS input
  |-- increasing odd-order harmonics with stronger drive
  |-- heavy overdrive: approximately 75–79 kHz final peak deviation ceiling
  |
  v
Stereo / MPX / RDS / FM modulator
```

The separate +4 dB gain block is not directly proven. It is simply the most useful functional representation of the measured input/output behavior.

---

# 11. Implications for the External Audio Processor

The external DSP limiter operates **before** the QN8007B internal pre-emphasis.

Therefore, a conventional PCM peak limiter cannot guarantee final FM deviation by itself, because the QN8007B subsequently applies frequency-dependent high-frequency boost.

Example:

- identical PCM peaks at 1 kHz and 10 kHz;
- the 10 kHz component is driven approximately 10 dB closer to the clipping threshold by the internal 50 µs pre-emphasis.

Therefore, the external processor requires **pre-emphasis-aware peak/deviation control**.

---

# 12. Possible System Strategies

## A. QN Soft Clip OFF

Advantages:

- nonlinear behavior remains under external DSP control;
- no QN soft-clipping distortion;
- no approximately +4 dB effective Soft Clip drive.

Disadvantages:

- the external DSP must guarantee safe deviation **after the effect of the QN internal 50 µs pre-emphasis**;
- a conventional PCM limiter is insufficient.

## B. QN Soft Clip ON

Advantages:

- provides a strong final deviation safety mechanism;
- extreme peaks around 95–100 kHz are reduced to approximately 75–79 kHz.

Disadvantages:

- approximately +4 dB effective drive at low and medium levels;
- nonlinear behavior above the knee;
- odd-order harmonics under strong drive;
- if activated frequently, audible processing/distortion is likely.

## Preferred Design Principle if Soft Clip Remains Enabled

The external DSP should be tuned so that:

> **the QN8007B soft clipper operates only rarely and lightly during normal program material, acting primarily as a final safety stage.**

DSP headroom should therefore be based on the expected **pre-emphasized peak level**, not only on the raw PCM peak.

---

# 13. Why the Previous PCM Limiter Was Insufficient

The external stereo limiter limits the PCM audio before the QN8007B.

The QN8007B then:

1. applies 50 µs pre-emphasis;
2. increases high-frequency components by a frequency-dependent amount;
3. applies the measured soft-clipping behavior to the resulting signal.

Therefore:

> **A PCM signal limited to −8 dBFS does not automatically imply safe FM deviation.**

High-frequency transients can grow substantially after the internal pre-emphasis stage.

---

# 14. Interpretation of the Approximately +4 dB Effect

At low and medium levels, Soft Clip ON produces approximately:

> **ON output ≈ OFF output + 4 dB**

This behavior appears at both 1 kHz and 10 kHz as long as the signal remains below the clipping knee.

Important limitations:

- it is not known whether this is implemented as a separate +4 dB gain stage;
- it may instead be the low-level slope of the same nonlinear transfer characteristic;
- for DSP design, however, it is practical to treat it as approximately **+4 dB effective drive**.

---

# 15. Measurement Method Notes

## SDR Analyzer

- RTL-SDR IQ sample rate: **1.024 MS/s**
- tuning offset: **+250 kHz**
- manual gain: `rtl_sdr -g 1`
- capture duration: approximately 60 s
- RF LPF: **160 kHz**
- MPX LPF: **75 kHz**
- 50 ms PIRA-like peak-hold histogram
- MPX / BS.412-like power metric
- IQ clipping diagnostics

## MPX Power Reference

In the custom analyzer:

> **0 dBr = power of a sine wave producing ±19 kHz peak deviation**

This is an internal comparison reference, not a calibrated broadcast-compliance measurement.

## Low-Level Sine-Wave Caveat

At low audio levels, fixed pilot/RDS components can significantly affect total MPX and PIRA-style measurements.

Therefore, the **Audio Spectrum fundamental level** is the preferred metric for low-level ON/OFF gain comparison.

---

# 16. Final Characterization

The QN8007B `TX_SFTCLPEN` behavior can currently be summarized as follows:

> **Soft Clip ON produces approximately +4 dB of effective drive at low and medium signal levels, followed by a progressive soft-saturation characteristic. The clipping knee is frequency-dependent and shifts according to the internal 50 µs pre-emphasis gain. Under heavy drive, final FM peak deviation is constrained to approximately 75–79 kHz, while significant odd-order harmonic distortion may be generated.**

The key result is:

> **The measured 1 kHz-to-10 kHz knee shift is approximately 9–10 dB, while the theoretical 50 µs pre-emphasis difference is 9.95 dB.**

This provides strong experimental evidence that the soft-clipping function is effectively **post-pre-emphasis / pre-emphasis-aware**.

---

# 17. Evidence Classification

Future updates should distinguish clearly between:

- **MEASURED** – directly observed measurement result
- **FIRMWARE-AUDITED** – verified from firmware/register behavior
- **STRONGLY INFERRED** – strongly supported by measurement correlation
- **UNKNOWN** – undocumented or not yet measured

The exact internal implementation of the QN8007B soft-clipping function should not be stated as fact unless new documentation or direct measurements establish it.

