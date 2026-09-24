# FM Headend — Audio Processing

> Implementation status (2026-09-25): file-backed Source selection, Source Gain,
> shared GST Basic profiles, existing WebUI integration and audio-only restarts are
> implemented. See [file profiles](audio-profile-files.md). The larger modular DSP
> stages and Stereo Tool runtime described below remain future work; this iteration
> preserves the current Basic parameter defaults and ordering.

**Technical and UI specification · 2026-09-24 · design baseline**

> **The goal is not to display every knob. It is to make every visible knob meaningful, preserve settings that remain hidden, and keep eight distinct radio programmes on the air without interruption.**

## 0. How to read this document

This document distinguishes three kinds of statement:

- **Decision** — required headend behaviour and the frontend contract; implement it.
- **Candidate default** — a sensible starting point, **not** a measured FM broadcast-processing preset.
- **To be verified** — an element name, property, enum, range, default, or runtime cost that can only be finalized against the actual GStreamer/LSP versions installed on the target Raspberry Pi and through measurement.

The frontend may use English field labels; the short explanations below provide the substance of its future helper text. Tables describe **user-facing concepts**: for example, “Threshold (dBFS)” does not imply that the plugin's native property stores its value in decibels. The exact mapping is an implementation task.

**Scope:** Built-in / Modular DSP and its file-based Audio Profiles. The complete Stereo Tool processing path is a separate, later task. Introducing the new design must not silently change the sound of the existing `GST_BASIC` default profile merely to adopt the proposed new defaults.

---

## 1. System overview and design principles

The Raspberry Pi 3B+ decodes network streams, processes their audio, and sends stereo PCM to the RP2350; the QN8007B performs the FM-specific functions. The target is **eight different programmes processed simultaneously**. One stream means one decoder and one DSP chain: this is not a system broadcasting a single programme on eight frequencies.

**Non-functional requirements:**

1. Processing must fit reliably within the Pi 3B+'s CPU, memory, and latency budgets with eight separate streams. Measure this; do not assume it.
2. A missing plugin or invalid profile must not silently switch the affected programme to unprocessed audio.
3. A profile or source-audio change must manage **only the affected audio pipelines' lifecycle**; it must not stop or reinitialize the TX, RF, or RDS subsystems.
4. Files must remain hand-editable, while saving through the web UI must preserve less common, hidden, otherwise supported parameters.
5. Neither a digital limiter nor an LPF near 15 kHz replaces measurement of the final RF deviation and stereo multiplex.

### 1.1. Three source-processing modes

| Source `processor_mode` | Behaviour |
|---|---|
| `bypass` | No DSP profile; **Source Gain still applies**. |
| `builtin` | Source Gain followed by the selected file-based Audio Profile's modular chain. |
| `stereotool` | Source Gain followed by a future, independent Stereo Tool path. **Not** chained with the Built-in DSP stages. This round prepares only the mode/data model. |

Relevant Source fields: `processor_mode`, `input_gain_db` (**Source Gain**, initially 0 dB), and `audio_profile_path` (stable, relative profile reference; `null` for bypass). The user selects a profile from a list; **they do not type a path**. Stereo Tool may eventually have separate preset management; a Built-in JSON profile is not an `.sts` container.

### 1.2. Approved signal chain

**Source decode → Source Gain → [Built-in Audio Profile] → common PCM output → mux → RP2350 → QN8007B**

Within a Built-in profile, the order is **fixed**:

| # | Stage | Purpose |
|---:|---|---|
| 1 | Profile Input Gain | Level correction at the beginning of the profile. |
| 2 | HPF | Reduce unwanted infrasonic and very-low-frequency content. |
| 3 | Gate / Expander | Targeted treatment of noise, quiet passages, and dynamics. |
| 4 | AGC / Leveller | Slower levelling of varying source and programme levels. |
| 5 | EQ | Tonal and spectral correction. |
| 6 | Wideband Compressor | Control the dynamics of the full audio band. |
| 7 | Multiband Compressor | Control the dynamics of individual frequency bands. |
| 8 | Master Gain | Drive the limiter and set the finished programme's loudness. |
| 9 | Limiter | Control short peaks. |
| 10 | LPF | Restrict the upper audio-frequency range. |
| 11 | Output Trim | Final, **attenuation-only** adjustment of the PCM output. |

The two compressors are **independently switchable stages**. `AGC → Wideband → Multiband → Limiter` is a valid configuration; using only one of the compressors is equally valid. This is not a freely rearrangeable effects graph.

Stages belonging to a profile exist only in `builtin` mode. Source Gain is a shared source-level control: it remains present in bypass mode and in the future Stereo Tool mode.

---

## 2. Profile model, sharing, and lifecycle

### 2.1. A file, not a database record

A profile is an independent, versioned JSON resource. One profile can be assigned to multiple Sources; **editing that profile affects every Source referencing it**. No hidden per-source copies are made.

| Function | Requirement |
|---|---|
| Factory profiles | Tracked in Git under `audio-profiles/default/`; installed under `/usr/share/fmheadend/audio-profiles/default/`; **read-only**. |
| Custom profiles | `/var/lib/fmheadend/audio-profiles/custom/`; Create, Edit, Clone, Delete. |
| Built-in starting point | `audio-profiles/default/basic.json` — reproduce existing GST Basic behaviour without changing its sound during migration. |
| Profile selection | From the backend API's list; paths are not editable free text. |
| Rename | The human-readable name may change; the assigned relative file path **remains stable**. |
| Clone | A new, safely generated backend name/path; never overwrite the original. |
| Delete | Forbidden while referenced; always forbidden for a factory profile. |
| Save | Schema validation, atomic file write, then rebuild affected active audio pipelines. |

The file-profile foundation API family already exists: `/api/v1/audio-profiles/files` (list/get/create/update/clone/delete). `processor_mode`, `input_gain_db`, and `audio_profile_path` are now integrated with the existing GST Basic path. New modular DSP plugins remain **separate implementation work**; this document does not claim those plugins are finished. The repository's schema defines the exact keys of the existing v1 JSON format: **do not invent a second, “almost compatible” JSON representation here**.

### 2.2. Mandatory preservation rule

Settings for every supported plugin option must survive even when that plugin is inactive or a property is not shown in the frontend. Switching back to an earlier plugin must restore its previous sound settings. If the UI displays only six of a plugin's twenty parameters, saving **must not replace the old object with a new six-field object**. The valid schema determines how unknown, unsupported keys are handled; there is no silent “accept everything” policy.

If saving the profile succeeds but restarting audio does not, report the two outcomes separately: **file saved; application failed**. Do not claim that the on-air programme is using the new profile.

### 2.3. Editing and presentation rules

- Profiles are edited in structured forms, **not a raw JSON text box**.
- Stages use collapsible sections; show Off or a plugin selector only where the choice actually exists.
- Selecting a plugin preserves the previously configured values of alternative implementations.
- `default/` profiles offer **Clone**, not Edit/Delete; custom profiles offer all three.
- No automatic live “slider audition” or immediate pipeline restart; **Save** is the application boundary.
- No interactive EQ graph initially; a passive frequency-response preview may come later.
- dB, dBFS, dBTP, LUFS, Hz, Q, ms, and linear gain **are not interchangeable**. A typed, tested conversion layer must map UI concepts to plugin properties.

---

## 3. Stage specifications and helper text

“Visible fields” define the **required initial frontend**. Preserved background options and valid ranges are tied to the target system's `gst-inspect-1.0` output. An unspecified starting value is intentional: it is a **decision reserved for measurement**, not an omission.

### 3.1. Profile Input Gain — `volume`

**No selector.** One **Gain (dB)** field, default **0 dB**. This differs from Source Gain: the former belongs to a shared profile; the latter belongs to one Source.

**Helper:** “Adjusts the overall input level of this profile. Excessive boost may make the AGC, compressor, or limiter work harder later in the chain; 0 dB leaves the level unchanged.”

Native GStreamer `volume` can also accept a linear multiplier; the backend performs the dB ↔ linear conversion. If high positive gain is allowed, do not rely exclusively on a property whose range is too narrow in the target version. [G1]

### 3.2. HPF — `audiocheblimit` high-pass

**Selector:** Off / Chebyshev. **Visible:** Cutoff (Hz). **Candidate default:** On, **30 Hz**, 4 poles, Type I, `ripple=0` — the latter yields a Butterworth-like response. Preserve `poles`, `ripple`, and `type` in JSON; they need not be editable in the initial UI. GStreamer rounds an odd number of poles up to the next even number. [G2]

**Helper — Cutoff:** “Reduces unwanted very-low-frequency content. Set too high, it can also remove musical bass.”

**Engineering note:** 30 Hz is a cautious first setting, not a universal regulatory requirement or a measured value.

### 3.3. Gate / Expander

**Selector:** Off (**default**) / Basic Expander (`audiodynamic`) / LSP Expander Stereo / LSP Gate Stereo.

| Implementation | Visible fields and meaning |
|---|---|
| Basic Expander | Threshold, Ratio, Hard/Soft Knee. Do not invent Attack/Release controls for it. |
| LSP Expander Stereo | Mode: **Downward / Upward**; Threshold, Ratio, Knee, Attack, Release. |
| LSP Gate Stereo | Threshold, Reduction, Attack, Hold, Release. |

**Helper — Downward Expander:** “Makes quiet passages below the threshold quieter still. Can reduce background noise, but excessive expansion may cut off natural decays.”

**Helper — Upward Expander:** “Increases dynamics by raising louder passages. It cannot restore detail already lost to clipping or excessive compression.”

**Helper — Gate Reduction:** “Attenuation applied while the gate is closed. Gentler settings can sound more natural; stronger settings suppress more background noise but can make gating more obvious.”

Basic `audiodynamic` genuinely exposes only `mode`, `threshold`, `ratio`, and `characteristics`. Do **not** mislabel its native threshold and ratio units directly as “dBFS” and a conventional “2:1” ratio. If the frontend expresses gate Reduction as a positive “attenuation in dB”, explicitly map the signed semantics of LSP's native `Reduction`; ducking is not the same as ordinary gating. [G3] [L1] [L2]

**Preserve in the background:** LSP sidechain, hysteresis/transition zone, stereo split, makeup, dry/wet, and other options actually present in the specific plugin. Do not copy a “Hold” field into the LSP Expander UI simply because the Gate exposes it.

### 3.4. AGC / Leveller

**Selector:** Off / LSP Autogain Stereo / GStreamer Rust `audioloudnorm`. The preferred experimental path is **LSP Autogain**; `audioloudnorm` is an alternative. Do not include the speech-communications-oriented `webrtcdsp` AGC in the broadcast-processing list.

**LSP Autogain Stereo — visible fields:**

| Field | Short helper text |
|---|---|
| Target Loudness (LUFS) | “The longer-term perceived-loudness target the controller approaches. Not an instantaneous peak ceiling.” |
| Maximum Gain (dB) | “Caps amplification, protecting quiet or faulty streams from excessive boost.” |
| Silence Threshold | “Below this point, the AGC stops increasing gain in response to silence or background noise.” |
| Long-time Grow | “How quickly gain may rise when the programme stays quieter.” |
| Long-time Fall | “How quickly gain may fall when the programme stays louder.” |
| Short-time Fall | “Faster gain reduction during a sudden louder passage; not a substitute for a peak limiter.” |
| **Short-time Grow** | “When enabled, brings up the entire programme faster after a sudden drop in level. It may sound more consistent, but can cause pumping and raise noise or decays.” **OFF by default**; expose its speed when enabled. |

The LSP Autogain documentation explicitly describes separate long-time and short-time Grow/Fall mechanisms, silence freeze, and maximum gain. Preserve actual period/drift/weighting/sidechain parameters in JSON; they need not appear initially. **Do not invent LUFS, ms, or grow/fall defaults in advance.** [L3]

**`audioloudnorm` — visible fields:** Loudness Target (LUFS), Loudness Range Target (LU), Maximum True Peak (dBTP), Offset (LU). Do not add Attack/Release controls that do not exist. The documented element accepts and produces only **192 kHz / F64LE** raw audio caps; the 48 kHz/F32LE backbone therefore needs explicit local up/down conversion and latency/CPU measurements. This element was previously not found on the target Pi: installation/availability is **to be verified, not a completed feature**. [G4]

**Relationship to compression:** AGC moves the overall programme level; compression shapes shorter-term dynamics. **AGC + limiter, without wideband/multiband**, is a valid configuration intended to be lighter on CPU; any actual CPU advantage must be measured with eight streams.

### 3.5. Equalizer

**Selector:** Off / `equalizer-nbands` / LSP Parametric Equalizer x16 Stereo. **Starting layout:** 3 bands at **100 / 1100 / 11000 Hz**, all **0 dB**; up to 16 in the frontend. This does **not** assert that the new EQ produces exactly the same response as the old fixed three-band EQ.

| Implementation | Visible fields per band |
|---|---|
| GStreamer `equalizer-nbands` | Frequency (Hz), Gain (dB), **Bandwidth (Hz)**, Add/Remove. |
| LSP Parametric EQ x16 Stereo | Filter type: Bell / Low Shelf / High Shelf / Notch; Frequency (Hz), Gain (where applicable), Q, Add/Remove. |

**Helper — Frequency:** “The frequency around which this filter operates.” **Gain:** “Positive values boost, negative values cut; 0 dB by itself does not colour the sound.” **Bandwidth:** “The width of the affected range, in Hz.” **Q:** “Filter selectivity; a higher Q generally means a narrower affected range.”

**No per-band Enabled switch in the frontend.** For GStreamer, 0 dB or Remove is sufficient. For LSP, Remove sets that internal filter slot to **Off**; 0 dB is **not** a universal bypass (a Notch, for example, has no meaningful gain). The GStreamer plugin supports up to 64 bands, but we limit the UI to 16; its native property is `bandwidth`, **not Q**. [G5] [G6]

Include LSP's more complex filter types/modes/slopes, FIR/FFT/SPM, decramp, and other options in the profile only if they exist in the installed version and can be mapped correctly; the UI must always preserve existing supported values. The first UI has no EQ-curve editor. [G6]

### 3.6. Wideband Compressor — independent stage

**Selector:** Off / Basic Compressor (`audiodynamic`) / LSP Compressor Stereo. **Off in the proposed new neutral profile template.**

| Implementation | Visible fields |
|---|---|
| Basic | Threshold, Ratio, Hard/Soft Knee. GStreamer `audiodynamic` has **no** Attack, Release, or Makeup. |
| LSP Stereo | Threshold, Ratio, Attack, Release, Knee, **Makeup Gain**. |

**Helper — Threshold:** “The level around which compression begins.” **Ratio:** “Determines how much further level increase above the threshold remains; a stronger ratio restrains loud passages more.” **Attack:** “How quickly the compressor reacts to a loud transient.” **Release:** “How quickly gain reduction is released.” **Knee:** “How gradually compression engages around the threshold.” **Makeup Gain:** “Restores average level lost through compression; it may make the later limiter work harder.”

Basic's native `ratio` and `threshold` may not correspond to a conventional user-facing ratio/dBFS representation: test the conversion mathematics and preservation of the old GST Basic result. **Downward** is the default LSP compressor mode; upward, boosting, stereo split, sidechain, dry/wet, hold, and further details may be preserved in JSON. The plugin's separate “Release Thresh” must not be confused with the main compression Threshold. [G3] [L4]

### 3.7. Multiband Compressor — independent stage

**Selector:** Off / **LSP Multiband Compressor Stereo x8**. There is no extra “Basic Multiband”, and it is not merged into the Wideband stage. **Start with four active bands** on plugin selection; allow expansion up to the plugin's **eight bands**. Candidate starting crossover frequencies: **120 Hz / 1 kHz / 5 kHz**, in ascending order.

| Global field | Meaning |
|---|---|
| Active Bands | Number of frequency bands into which the programme is split (1–8, as supported by the plugin's actual model). |
| Crossover frequencies | Boundaries between adjacent bands; the outer usable frequency range is restricted separately by the HPF/LPF. |
| Processing Mode | Initial candidate: **Classic**. Supporting/displaying Modern and Linear Phase depends on the target version and CPU measurement. |

**Per band:** Threshold, Ratio, Attack, Release, Knee, Makeup Gain, **Compressor Bypass**. Individual bands are collapsible.

**Helper — Band Bypass:** “The band's audio remains present, but its compressor applies no gain reduction. This is **not** Mute, and it does not remove the band from the crossover system.”

The native LSP UI distinguishes a band's **Band** presence from the associated compressor's **On** state; **Band #0 is always present**. The frontend's “Bypass” disables the latter compressor: it does **not** delete the band or invoke Solo/Mute. [L5]

The conventional four-band labels “Bass / Low-mid / High-mid / High” are merely UI labels, not hard-coded frequency ranges. Modern and Linear Phase process differently and may change latency and CPU demand. Decide which modes can be used in production templates under the Pi 3B+'s full **8× distinct-stream load**; do not exclude or enable them automatically based on assumed cost. [L5]

### 3.8. Master Gain — `volume`, **before the limiter**

**No selector. Visible:** Gain (dB), default **0 dB**.

**Helper:** “Adjusts the entire processed programme's level before the limiter. Boosting can make the programme louder, but pushes more peaks into limiting, which can audibly alter dynamics and tone.”

Do not confuse this with Compressor Makeup Gain (compensation within the compressor) or Output Trim (output matching **after** the limiter). Positive Master Gain with the limiter Off can easily create excessive final PCM levels; the UI should warn about this, not secretly enable another processor.

### 3.9. Limiter

**Selector:** Off / Basic (`rglimiter`) / LSP Limiter Stereo. **A limiter is expected to be enabled in a processed broadcast profile**, but the shipped `default/basic.json` must preserve the inherited sound of its original settings.

**Basic:** GStreamer `rglimiter` performs soft-knee-like hard limiting around a **fixed −6 dB threshold**; its only meaningful plugin-specific control is `enabled`. **This is not a −6 dBFS output ceiling.** It has no adjustable threshold, attack, release, or ceiling; do not draw sliders for them. [G7]

**LSP Stereo — visible fields:**

| Field | Short helper text |
|---|---|
| Threshold (user-facing dBFS) | “The peak-level target for the selected limiting mode; **not** a guarantee of RF deviation after the later LPF or QN.” |
| Lookahead (ms) | “Gives the limiter advance notice of an approaching peak; introduces latency.” |
| Attack (ms) | “Time required to build peak attenuation; the valid range may depend on lookahead and algorithm.” |
| Release (ms) | “How long gain reduction takes to recover after a peak; too short a release can cause audible distortion or pumping.” |
| Stereo Link (%) | “Degree of shared attenuation between the two channels. An initial **100%** setting can help keep the stereo image stable.” |
| Oversampling | “Potentially better control of inter-sample peaks at greater CPU cost. Start **Off**; populate the selector with actual Half/Full/True Peak enum values only after verification.” |

Stereo Link is documented as `slink` in the relevant GStreamer LV2 wrapper. The limiter's native `th` property is **linear**: do not misrepresent it as dBFS; convert the frontend's dBFS value separately. Set `boost` **OFF** by default; ALR, knee, mode, sidechain, dither, plugin input/output gain, and the various oversampling algorithms may be supported only with an exact, versioned mapping. According to LSP's documentation, some limiting modes may permit overshoot above the threshold: claim a production “guaranteed ceiling” **only for a measured configuration**. [G8] [L6]

**Starting threshold/lookahead/release:** results of physical validation and the desired programme sound, not asserted numbers. The previously discussed **−3 dBFS** is only a possible *initial test point*, not a finalized profile parameter.

### 3.10. LPF — after the limiter

**Selector:** Off / Chebyshev (`audiocheblimit`, low-pass) / Windowed Sinc (`audiowsinclimit`, low-pass).

| Implementation | Visible fields | Preserve in background |
|---|---|---|
| Chebyshev | Cutoff (Hz), **Poles** | `type`, `ripple`, all additional supported settings. |
| Windowed Sinc | Cutoff (Hz), **Kernel Length** | `window` and other supported options. |

**Helper — Cutoff:** “Upper boundary of the audio low-pass filter; **not** a brick wall. Too low sounds dull; too high may compromise separation around the pilot.” **Poles / Kernel Length:** “Also affect transition steepness and computational cost; a longer sinc kernel is typically steeper, but more expensive and may add latency.”

**Target:** approximately **15 kHz** of FM audio bandwidth while protecting the region around the 19 kHz stereo pilot. **Candidate starting implementation: Windowed Sinc**, but do not commit to a long FIR kernel on the Pi 3B+ without a CPU benchmark. In GStreamer, `length` is a **kernel length rounded to an odd number**, not a “filter slope in dB/octave” property; Chebyshev's pole count is rounded to an even number. The sinc `window` enum is separate from kernel length, and the cutoff/length combination must be measured together. [G2] [G9]

**Important:** the LPF follows the limiter, so it can attenuate high-frequency content generated by limiting; however, filtering can also cause **sample peaks to rise again**. The later attenuation-only Output Trim can provide headroom, but it is **neither a measuring instrument nor a hard limiter**.

### 3.11. Output Trim — `volume`, **after the LPF**

**No selector. Visible:** Output Trim (dB); **default 0 dB; the frontend permits only 0 dB or attenuation**.

**Helper:** “Matches the final PCM level sent toward the QN. It does not drive the limiter harder or make the already-limited signal louder; this control can only turn the level down.”

Output Trim does not replace alignment of the QN's analogue/RF modulation chain. Negative trim reduces the output PCM level; it cannot repair clipping that may already have happened during an earlier fixed-point conversion. Keep the DSP backbone floating-point until the final output conversion.

---

## 4. Audio format and FM-specific constraints

**Planned common DSP caps:** stereo, **48 kHz, F32LE** in the GStreamer/LSP processing backbone, with explicit and minimal `audioconvert`/`audioresample` steps. The stereo LSP LV2-wrapper documentation likewise specifies two-channel F32LE for the inspected compressor/limiter/EQ elements. This is a **target**, not a claim about the precise caps of the already-running pipeline. [G6] [G8]

`audioloudnorm` is the exception: a local 192 kHz/F64LE island with its own conversion. The actual, repository- and hardware-verified RP2350 interface contract determines the final PCM format; do not invent a new SPI frame or bit-depth specification here.

The QN8007B performs **50 µs pre-emphasis**, stereo pilot/MPX generation, and RDS; the Pi must not apply another 50 µs of pre-emphasis. A DSP sample-peak limiter **cannot guarantee FM deviation after pre-emphasis**. Finalize the 15 kHz LPF response, spectrum around the 19 kHz pilot, stereo behaviour, and actual modulation level through **RF/MPX measurement**.

The QN's own AGC, observed to perform poorly, must not remain “accidentally active” as a substitute for software AGC; clarify and verify this separately in the hardware configuration. Saving a software profile, however, is **not** grounds for restarting the QN or the RF lifecycle.

---

## 5. Defaults, profiles, and resource budget

**Keep these distinct:**

- **Inherited factory Basic:** preserves the current `GST_BASIC` behaviour; introducing the profile system is not an arbitrary sound upgrade for existing users.
- **New, neutral modular-profile template:** 0 dB Source/Profile/Master/Output gain; flat EQ; Gate Off; compressors Off; Short-time Grow Off; HPF and LPF proposed, with filter parameters finalized by measurement; the limiter's role and exact threshold require separate validation.
- **Later target profiles:** conservative/music, milder general-purpose, CPU-conscious AGC + limiter, and compressor Off for already-processed streams. Do **not** promise “broadcast-ready” presets with invented loudness figures.

The cost of LSP multiband Classic/Modern/Linear Phase, EQ FIR/FFT, limiter oversampling, and sinc kernels **cannot be inferred from one source or from having eight nominally identical TX channels**. The mandatory load scenario is **8 distinct streams, 8 decoders, 8 active DSP chains**, on the actual target Pi. Measure total/per-core CPU, memory, underruns, latency, stream reconnection, cold start, and audio restart following a profile save. If a configuration is too expensive, document or disable it on the actual device; do not silently discard the profile.

---

## 6. Implementation and verification gates

### 6.1. Required fact-check before implementing each plugin

On the target Raspberry Pi, with the installed packages:

```bash
gst-inspect-1.0 volume
gst-inspect-1.0 audiocheblimit
gst-inspect-1.0 audiowsinclimit
gst-inspect-1.0 audiodynamic
gst-inspect-1.0 equalizer-nbands
gst-inspect-1.0 rglimiter
gst-inspect-1.0 audioloudnorm
gst-inspect-1.0 lv2
```

Inspect the element names and properties **actually listed on that machine** by the LSP GStreamer wrapper; do not treat generic LSP GUI labels as property names. Examples documented by GStreamer: `lsp-plug-in-plugins-lv2-compressor-stereo`, `lsp-plug-in-plugins-lv2-mb-compressor-stereo`, `lsp-plug-in-plugins-lv2-para-equalizer-x16-stereo`, `lsp-plug-in-plugins-lv2-limiter-stereo`. For **LSP Autogain Stereo**, the existence of a native LSP plugin does **not** prove that the installed GStreamer LV2 wrapper exposes it as an element; verify this separately on the Pi. [G6] [G8] [L3]

Build an explicit adapter table from the resulting property list: *frontend label → validated profile field → exact GStreamer property → unit/conversion → min/max/enum*. No guessed property assignment; no silently ignored version-dependent properties. Do not put obsolete controls or controls absent from the target system into the frontend.

### 6.2. Acceptance tests

1. **Inherited sound:** factory Basic produces the same chain and settings as the previous GST Basic; the legacy default does not change just because the JSON model does.
2. **Lossless editing:** inactive plugins and hidden, supported parameters survive an open → edit → save round trip.
3. **Parameter units:** dB ↔ linear gain; `audiodynamic` threshold/ratio; EQ Bandwidth vs Q; LSP gate Reduction; LSP limiter `th` and `slink` adapter tests.
4. **Band logic:** EQ Add/Remove and LSP Off slots; multiband crossover ordering, 4→8 bands, Band #0, per-band compressor bypass ≠ mute.
5. **Missing plugin/invalid profile:** no automatic bypass; accurate Source-specific error and traceable log.
6. **Lifecycle:** profile save restarts affected audio, not TX/RF/RDS. A shared profile affects every active audio chain referencing it.
7. **Integration:** bypass, builtin, and eventually stereotool paths; Source Gain in the planned position in all three; an OFF Source retains its saved configuration.
8. **Eight-stream stress:** CPU/memory/underruns, repeated stream faults, and profile saves. Finalize low-CPU and quality templates from measurements.
9. **RF verification:** LPF response, PCM true peak/post-LPF peaks, deviation after pre-emphasis, pilot/spectrum; no “guaranteed RF ceiling” label based solely on the PCM limiter.

### 6.3. Delivery boundary

In the first integration round, wire the Source/file-profile model and the **GST Basic processing already actually implemented** into a functioning file-based runtime path. LV2/Rust and the full modular chain can only be called complete after installation, adapter, and load verification. This document describes the **target state**, not a fabricated completion report. Changes require tests + changelog; **no automatic commit/push/deploy/flash**.

---

## 7. Primary technical references

The links below point to the plugins' **official, general documentation**. They do **not** supersede the versions actually installed on the target Pi or the repository's actual behaviour.

| ID | Source |
|---|---|
| [G1] | [GStreamer `volume`](https://gstreamer.freedesktop.org/documentation/volume/index.html) |
| [G2] | [GStreamer `audiocheblimit`](https://gstreamer.freedesktop.org/documentation/audiofx/audiocheblimit.html) |
| [G3] | [GStreamer `audiodynamic`](https://gstreamer.freedesktop.org/documentation/audiofx/audiodynamic.html) |
| [G4] | [GStreamer `audioloudnorm`](https://gstreamer.freedesktop.org/documentation/rsaudiofx/audioloudnorm.html) |
| [G5] | [GStreamer `equalizer-nbands`](https://gstreamer.freedesktop.org/documentation/equalizer/equalizer-nbands.html) |
| [G6] | [LSP Parametric EQ x16 Stereo in the GStreamer LV2 wrapper](https://gstreamer.freedesktop.org/documentation/lv2/lsp-plug-in-plugins-lv2-para-equalizer-x16-stereo.html) |
| [G7] | [GStreamer `rglimiter`](https://gstreamer.freedesktop.org/documentation/replaygain/rglimiter.html) |
| [G8] | [LSP Limiter Stereo in the GStreamer LV2 wrapper](https://gstreamer.freedesktop.org/documentation/lv2/lsp-plug-in-plugins-lv2-limiter-stereo.html) |
| [G9] | [GStreamer `audiowsinclimit`](https://gstreamer.freedesktop.org/documentation/audiofx/audiowsinclimit.html) |
| [L1] | [LSP Expander Stereo — developer documentation](https://lsp-plug.in/?page=manuals&section=expander_stereo) |
| [L2] | [LSP Gate Stereo — developer documentation](https://lsp-plug.in/?page=manuals&section=gate_stereo) |
| [L3] | [LSP Autogain Stereo — developer documentation](https://lsp-plug.in/?page=manuals&section=autogain_stereo) |
| [L4] | [LSP Compressor Stereo — developer documentation](https://www.lsp-plug.in/?page=manuals&section=compressor_stereo) |
| [L5] | [LSP Multiband Compressor Stereo x8 — developer documentation](https://lsp-plug.in/?page=manuals&section=mb_compressor_stereo) |
| [L6] | [LSP Limiter Stereo — developer documentation](https://lsp-plug.in/?page=manuals&section=limiter_stereo) |

---

*Implementation maxim: the profile describes what we want; the adapter knows what the plugin can do; measurement tells us what the radio can handle.*
