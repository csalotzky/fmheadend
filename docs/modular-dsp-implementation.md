# Modular Built-in DSP implementation — 2026-09-25

The current approved design is [fmheadend-audio-processing.md](fmheadend-audio-processing.md).
This document records implementation and measurement, not an RF acceptance report.

## Implemented

The existing file API, Source selection, ownership, output fan-out and audio-only
Save lifecycle are retained. No TX/QN/RDS, mux, SPI or protocol code changes.
The editor has eleven collapsible stages in the approved order:

`Input Gain → HPF → Gate/Expander → AGC → EQ → Wideband → Multiband → Master Gain → Limiter → LPF → Output Trim`

Source Gain precedes this chain independently. Built-in processing negotiates
F32LE/48 kHz/interleaved stereo, then converts once to the existing S16LE/48 kHz
stereo output. Bypass still applies Source Gain; Stereo Tool still fails explicitly.
Master Gain is before limiting; Output Trim is attenuation-only after LPF.

Audio Profile JSON and SQLite each use their current structure as version **1**.
No development-format conversion or backward compatibility is supported.
Every stage has `selected` and `implementations`; saved settings for inactive
implementations are validated and preserved. `OFF` creates no element. A stage's
settings are not erased by switching it off. No arbitrary native property bag is
accepted. Authoritative fields, bounds and defaults are in `daemon/models/dsp.py`.

Example shape (abbreviated; all eleven stages are required):

```json
{
  "schema_version": 1,
  "name": "My processing",
  "stages": {
    "input_gain": {"selected":"VOLUME","implementations":{"VOLUME":{"gain_db":0}}},
    "hpf": {"selected":"CHEBYSHEV","implementations":{"CHEBYSHEV":{"cutoff_hz":30,"poles":4,"ripple_db":0,"type":1}}},
    "gate": {"selected":"OFF","implementations":{}},
    "agc": {"selected":"OFF","implementations":{}},
    "eq": {"selected":"OFF","implementations":{}},
    "wideband": {"selected":"OFF","implementations":{}},
    "multiband": {"selected":"OFF","implementations":{}},
    "master_gain": {"selected":"VOLUME","implementations":{"VOLUME":{"gain_db":0}}},
    "limiter": {"selected":"OFF","implementations":{}},
    "lpf": {"selected":"CHEBYSHEV","implementations":{"CHEBYSHEV":{"cutoff_hz":15000,"poles":4,"ripple_db":0,"type":1}}},
    "output_trim": {"selected":"TRIM","implementations":{"TRIM":{"gain_db":0}}}
  }
}
```

`GET /api/v1/audio-profiles/files/dsp` supplies the neutral creation template,
ordered stage choices, validated field descriptions/defaults and **daemon-local**
factory availability. It is editor metadata in the existing API family, not a new
profile store. Available does not certify every possible parameter combination or
eight-programme load. Save/start instantiate selected factories, set parameters
and verify native readback; negotiation/start failures remain Source errors.

### First-release defaults

`default/basic.json` keeps its stable filename. It now contains the eleven-stage
schema directly: unity gains, Chebyshev HPF 40 Hz / LPF 15 kHz (4 poles, type I,
ripple 0), other optional processors OFF. It is not a tuned broadcast preset.
The new-profile editor template uses HPF 30 Hz and the same LPF; EQ, dynamics
and limiter start OFF. Selecting an LSP implementation loads its existing validated
settings defaults. Hidden and inactive settings for supported implementations survive
Save. Every profile uses Limiter before LPF, with no ordering exception.

## Verified native mappings

LSP factory prefix below is `lsp-plug-in-plugins-lv2-`; all listed suffixes end
`-stereo`. Installed LV2 wrappers use **`enabled=true`** to process; false bypasses.
Online older wrapper documentation showing `bypass` is not used as the mapping.

| Stage / implementation | Exact properties / conversion |
|---|---|
| Three profile gains / `volume` | `gain_db → volume = 10^(dB/20)`; max +20 dB, Output Trim max 0 dB |
| HPF/LPF Chebyshev / `audiocheblimit` | `cutoff_hz→cutoff`, `poles→poles`, `ripple_db→ripple`, `type→type`; mode 1 HPF, 0 LPF. Even poles 2–32; cutoff strictly between 0 and 24 kHz. Type/ripple hidden but preserved |
| Expander / `expander-stereo` | downward/upward→`em=0/1`; threshold→`al` linear, ratio→`er`, knee dB→`kn=10^(-dB/20)`, attack/release→`at/rt` ms; makeup→`mk` linear |
| Gate / `gate-stereo` | threshold→`gt` linear; positive attenuation `reduction_db→gr=10^(-dB/20)`; attack/hold/release→`at/hold/rt` ms; hidden zone/hysteresis→`gz/gh/ht/hz` |
| AGC / `autogain-stereo` | target→`level` LUFS; max enable/gain→`max-on/max-amp` dB; silence→`silence`; long grow/fall→`tgrow-l/tfall-l` ms; short fall/grow→`tfall-s/tgrow-s` ms; short Grow enable→`qamp` (Off by default); K weighting `weight=5`, internal `scmode=0` |
| Wideband / `compressor-stereo` | downward `cm=0`; threshold→`al` linear; ratio→`cr`; `at/rt` ms; knee→`kn` linear; makeup→`mk` linear. Hidden hold/release threshold/stereo split/dry/wet→`hold/rrl/ssplit/cdr/cwt/cdw` |
| LSP EQ / `para-equalizer-x16-stereo` | IIR `mode=0`; Bell/Low Shelf/High Shelf/Notch→`ft-N=1/5/3/6`; frequency→`f-N`, gain→`g-N` linear, Q→`q-N`, RLC(BT) `fm-N=0`, slope `s-N=0`. All unused slots explicitly `ft-N=0`; never mute/solo to remove a filter |
| Multiband / `mb-compressor-stereo` | Classic `mode=0`; N ascending crossovers produce N+1 bands: `cbe-1..7`, `sf-1..7`. Band 0 always present. Bypass→`ce-N=false`, **never `bm-N` or band removal**. Per-band `al/at/rt/cr/kn/mk/ht-N` mirror dynamics units. All solos/mutes false |
| LSP limiter / `limiter-stereo` | threshold→`th` linear; lookahead/attack/release→`lk/at/rt` ms; link→`slink` percent, initially 100; `boost=false`, `ovs=0`, `mode=0` (Herm Thin). Hidden ALR preserved. No RF ceiling claim |

AGC native grow/fall amounts are separate from their times. Saved amount indexes
`0..12` map to `[0.1,0.5,1,3,6,9,10,12,15,18,20,21,24]` dB; default index 7 (12 dB)
and times come from installed LSP metadata, whose TTL specifies milliseconds.
Maximum-gain limitation starts enabled with the plugin's saved 36 dB value;
this is a cap, not a recommended broadcast target. Short-time Grow's speed remains
saved while Off. Enabling it can produce large transient boosts; a limiter remains
a separate stage. No leveller is substituted for final RF measurement.

Four multiband bands start at 120/1000/5000 Hz boundaries. Add/remove crossovers to
reach 1–8 bands; all eight band settings remain saved when inactive. Wideband and
Multiband may both run. Per-band bypass leaves crossover audio present.

## Blocked / limited implementations

* **LSP advanced modes:** limiter oversampling only Off; Herm Thin only; EQ IIR/RLC(BT)
  only; multiband Classic only. Other native enum values were inspected but are
  not accepted as working options without algorithm/load validation. Actual limiter
  `ovs` enum spans None, Half x2/x3/x4/x6/x8 (16/24 bit), Full equivalents; this
  installed 1.2.21 wrapper has no additional True Peak enum. They are not guessed.
* Not every LV2 sidechain/control port is supported. Unknown fields are rejected,
  not ignored. All **supported** hidden settings survive editor saves.

These are the current supported mode limits.

## Pi environment and measured evidence

Target: Raspberry Pi 3 Model B Plus Rev 1.3, aarch64 Debian 13 (trixie),
GStreamer 1.26.2, PyGObject 3.50.0. Operator installed from existing repositories:
`lsp-plugins-lv2=1.2.21-5`, `lsp-plugins-r3d-glx=1.2.21-5` (no upgrades/removals).
Existing native packages: plugins-good `1.26.2-1+deb13u2`, plugins-bad
`1.26.2-3+rpt3+deb13u2`, base `1.26.2-1+rpt3+deb13u1`, tools `1.26.2-2`.
No application deployment, service restart, production DB change or hardware
command occurred. Tests used a separate `/tmp` workspace and localhost files.

Isolated native results, not RF/audio-quality acceptance:

* Native volume multiplier .5 measured .500000001; left-only inputs remained
  silent on the right through all seven LSP stereo adapters.
* HPF 30 Hz: 10 Hz gain .01234, 1 kHz gain ~1. Chebyshev LPF 15 kHz:
  1 kHz ~1, 20 kHz .02585 (four poles/type I/ripple 0).
* LSP Bell at 1 kHz/+6 dB measured ~1.99526 amplitude gain.
  LSP Notch at 1 kHz measured ~2.94e-6 steady-state gain; removal restores unity.
* LSP limiter .1 threshold measured peak ~.099999 in its isolated test.
* Four/eight-band bypass retained unity RMS within 2%; active compression test
  measured gains .1153/.1296. These measurements don't certify flat phase or every
  crossover/ratio combination.
* Autogain tracked a level step; explicitly enabling Short-time Grow changed its
  recovery (final measured RMS .01767 vs .004565 with the selected slow Grow test).
* Real HTTP WAV decode resampled 44.1 kHz mono to S16LE/48 kHz stereo fan-out.
  Source −6 dB measured peak 4007; Source −6 plus Profile −6 measured 2010
  from an 8000-peak fixture. Audio-only rebuild retained the same output object.
  Cold start ~537 ms; subsequent starts/rebuilds ~47–57 ms; max process RSS ~58 MiB.
  These startup times are **not** steady-state end-to-end programme latency.

### Operator-authorized bounded eight-stream run

Two six-second measurement windows used **eight different 128 kbit/s MP3 files**
(different tones), eight localhost HTTP decoders and eight independent chains.
No shared decoder/source shortcut, mux connection or audio device was used.

| Measurement | AGC + limiter | AGC + LSP EQ + wideband + four-band + limiter |
|---|---:|---:|
| Whole-host CPU, averaged over four cores | 39.7% | 73.2% |
| Per-core CPU | 38.6 / 40.7 / 40.5 / 38.8% | 73.4 / 71.6 / 73.2 / 74.5% |
| Test process CPU (one core = 100%) | 160.5% | 281.9% |
| Process high-water RSS | 169.5 MiB | 298.2 MiB |
| Host available memory at end | 576.9 MiB | 461.8 MiB |
| Start all eight | 1.09 s | 1.80 s |
| Maximum observed PCM delivery gap | 37.6 ms | 58.4 ms |
| Staging queue drops, each of eight Sources | 0 | 0 |
| Rebuild one Source, same output ownership | 134 ms | 303 ms |
| Other seven Sources remain RUNNING | yes | yes |

Each Source delivered approximately 1.15–1.17 MB of PCM per window (expected
192,000 bytes/s). Slight excess reflects buffer boundaries/startup backlog.
RSS is process high-water memory across sequential cases, not isolated per-plugin
allocation. Host CPU includes other existing activity. Queue-drop counts are
**not RP/mux underrun measurements**; delivery gaps are not end-to-end latency.
Fixtures, CPU overhead and the short window do not establish programme-quality,
network-jitter or sustained thermal/load suitability. Limiter oversampling remained
Off, multiband Classic, EQ IIR; expensive modes remain unverified.

## Reproduce safely / deployment prerequisites

Inspect packages and factories first. Install from configured repositories only:

```sh
sudo apt-get install --no-upgrade lsp-plugins-lv2
gst-inspect-1.0 lsp-plug-in-plugins-lv2-autogain-stereo
gst-inspect-1.0 lsp-plug-in-plugins-lv2-mb-compressor-stereo
```

Use an isolated copy of the Python package plus `daemon/tools/` in a temporary
workspace, using an existing Python with GI/Pydantic. Do not update the deployed
checkout to run tests. From that workspace:

```sh
nice -n 15 python dsp_adapter_probe.py
nice -n 15 python dsp_source_probe.py
# Only when operator approves an eight-stream load window:
timeout 65s nice -n 15 python dsp_load_probe.py
```

`dsp_native_probe.py` and `lsp_native_probe.py` retain standalone property/audio
discovery cases for the retained implementations. All output
is to memory or temporary fixtures, never an audio device or mux socket. Backend
property descriptions match installed names, and readback detects clamping on
incompatible plugin versions. Save failure leaves files unchanged; saved/restart
failure remains the existing explicit partial result. Final source negotiation is
still checked by the real pipeline before a Source start reports success.

Eight distinct-stream endurance, live-network jitter/reconnection, total pipeline
latency, expensive optional modes and RF/MPX/deviation/pilot-region
measurements remain separate acceptance gates. No synthetic test proves those.

## Validation

The measurements above are from the earlier isolated native run, not a claim of
new RF or deployment validation. Current schema-cleanup tests and one-time Pi
file/database checks are recorded in [pre-production cleanup](preproduction-schema-cleanup.md).
