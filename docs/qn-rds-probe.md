# TX0 QN8007B RDS polling probe

Standalone `firmware/fmheadend_rp2354/tools/qn_rds_probe.py`, using the existing
Linux `Rp2354Client` against **unchanged production firmware 0.8.6a**, host I2C
address **0x62**. No RP consumer, FIFO access, scheduler, GPIO26 use or firmware
change is included. The old temporary RDS bench program was not a successful
handshake reference and is unavailable; this experiment records actual behavior.

## Two explicit byte-order cases

Both cases use the existing Linux `daemon.rds.encoder.group_0a` with PI `0x1234`,
PTY 0, TP false, stereo true, no AF and PS exactly `FMTEST  ` (eight characters).
The block words are identical in both cases; only their byte order in the QN
registers differs. This does not change the RDS_PUSH wire format.

| Segment | Blocks A B C D | RDSD0..7, `low-first` | RDSD0..7, `high-first` |
| --- | --- | --- | --- |
| 0 FM | 1234 0008 E0CD 464D | 34 12 08 00 CD E0 4D 46 | 12 34 00 08 E0 CD 46 4D |
| 1 TE | 1234 0009 E0CD 5445 | 34 12 09 00 CD E0 45 54 | 12 34 00 09 E0 CD 54 45 |
| 2 ST | 1234 000A E0CD 5354 | 34 12 0A 00 CD E0 54 53 | 12 34 00 0A E0 CD 53 54 |
| 3 spaces | 1234 000F E0CD 2020 | 34 12 0F 00 CD E0 20 20 | 12 34 00 0F E0 CD 20 20 |

`--byte-order` is mandatory. The tool does not auto-detect, switch or declare the
winning byte order. Run separate cases with a physical RP reset between them.

## Setup and exact sequence

The tool requires all eight TXs initially OFF and no active hardware fault. It
only configures/writes TX0: **87.5 MHz, 22.5 dB attenuation, DIGITAL, stereo,
50 us pre-emphasis, RDS enabled, AUTO gain, soft clipping off**. Current production
configuration supplies 48 kHz / 16-bit I2S (`0x07 = 0x71`). It checks SYSTEM1 is
`0x07` and reads `GAIN_TXPLT (0x0F)` to verify RDS_INT_EN bit 1 is already clear.
It never enables interrupt output or accesses GPIO26.

For each group experiment, operations are:

1. Read SYSTEM2 `0x01` and log it.
2. Read STATUS2 `0x1B` and log the baseline byte and bit 7.
3. Write exactly eight separate raw registers `0x10` through `0x17`, in ascending
   order, using the selected explicit byte-order case.
4. Read STATUS2 as the **very next bus command after RDSD7**, with no inserted
   sleep/log output between those commands. Log the value and host call interval.
5. Read SYSTEM2 again. Stop if it unexpectedly changed from step 1, or if
   SWRST/RECAL were asserted; do not silently clear or copy those bits into a write.
6. Write `SYSTEM2_before XOR 0x04`. Only RDSTXRDY changes; stereo, pre-emphasis,
   timeout and every other bit are preserved.
7. Read back SYSTEM2. Stop on failure/mismatch; there is no recovery or replay.
8. Poll STATUS2 for a bounded window (default 1 second), printing the first sample
   and every subsequent full-byte value transition, including returns to previous
   values. Record every observed bit-7 transition time.
9. Report baseline-to-after-RDSD7 bit-7 change separately from post-toggle polling
   changes. There is **no acceptance verdict** and no FIFO pop.

RF-OFF `handshake` mode performs one segment-0 experiment and exits. It is a real
register experiment, **not an offline dry-run**. A lack of STATUS2 movement while
TXREQ is zero does not prove that the active-TX handshake fails.

`over-air` explicitly enables RF before the first raw write and repeats segments
0,1,2,3 for the selected window (default 60 seconds, maximum 120). Each group gets
the same bounded observation sequence. If no bit-7 transition is observed during
a group experiment, repetition stops and the trace must be inspected. This guard
does not establish which operation caused a transition or whether a group went
on air. The tool never synthesizes FIFO credits or retries uncertain submissions.

Both modes attempt normal SET_TX_ENABLED OFF and SYSTEM1 verification on exit,
including errors and Ctrl-C. Cleanup failure prints **RF OFF NOT VERIFIED**;
physically disable bench RF if this occurs. No automatic reset/recovery is added.

### Existing-client limits that matter to interpretation

- Both raw-register commands retain the approved **100 ms pre-read wait**. Thus
  the post-RDSD7 observation is the next command, but is not a microsecond-level
  measurement. All log times are Pi monotonic timestamps with read-call intervals.
  Transitions can occur between samples, or during the SYSTEM2 readback gap.
- The default 25 ms polling interval is an additional sleep: effective sampling
  is roughly one read per 125 ms plus OS/bus overhead. The 1-second polling
  window can overrun by one bounded raw-command exchange. If that is too coarse,
  retain the trace; do not infer unobserved edges or shorten the existing client
  contract silently.
- Eight data writes alone take about 0.8 s through this interface. Repeated PS is
  deliberately slow and intended for observation, not sustained 11.4-group/s
  service. Finishing an in-progress experiment may extend `--seconds` by one
  complete experiment. No application-level retries are added; the client's BUSY
  retry and production raw-register transaction retries remain unchanged.
- M8.3 invalidates configuration eligibility after raw non-SYSTEM1 writes. The
  over-air path therefore enables TX0 **before** the first raw write, using normal
  SET_TX_ENABLED; it never bypasses this check with raw TXREQ writes. Subsequent
  raw writes also remove this lane's DIGITAL PCM source eligibility. The test
  uses DIGITAL silence, not SINE; shared I2S clocks and SPI reception keep running.
  RF OFF remains available without configuration. Reset/reconfigure before other
  bench work; the tool does not restore a previous configuration.

## First physical commands

Use the existing terminated RF bench. Keep `fmheadendd` and all other I2C/control
clients stopped. Keep the physically tested **M8.6A firmware flashed**. No firmware
build/flash is required for this Python-only experiment. Deploy the tool through
the normal Windows commit/push (operator action) and Pi `git pull --ff-only`.

On the Pi, after resetting the RP, first run RF OFF:

```bash
cd ~/fmheadend
git pull --ff-only
PY=/home/fmheadend/venvs/fmheadend/bin/python
set -o pipefail
"$PY" firmware/fmheadend_rp2354/tools/qn_rds_probe.py \
  --address 0x62 --byte-order low-first handshake |& tee /tmp/qn-rds-low-off.log
```

Physically reset the RP, then run the separate second case:

```bash
"$PY" firmware/fmheadend_rp2354/tools/qn_rds_probe.py \
  --address 0x62 --byte-order high-first handshake |& tee /tmp/qn-rds-high-off.log
```

Review those traces first. Optional over-air verification is explicitly selected:
reset RP before **each** command, tune the receiver to **87.5 MHz**, and clear
stale receiver PS/PI display state between cases.

```bash
"$PY" firmware/fmheadend_rp2354/tools/qn_rds_probe.py \
  --address 0x62 --byte-order low-first over-air --seconds 60 \
  |& tee /tmp/qn-rds-low-on.log
# Physically reset RP before running the other byte-order case.
"$PY" firmware/fmheadend_rp2354/tools/qn_rds_probe.py \
  --address 0x62 --byte-order high-first over-air --seconds 60 \
  |& tee /tmp/qn-rds-high-on.log
```

Record the actual PI (if displayed), PS, carrier and which byte-order case is
running. The desired decoded values are PI **1234** and PS **FMTEST** with two
trailing spaces. Observing a carrier or STATUS2 transition alone is insufficient
to establish RDS content/byte-order correctness.

## Expected observations, not assumed hardware values

There is intentionally no fixed expected STATUS2 byte. Capture:

- `1 SYSTEM2 before data` and `2 STATUS2 baseline`.
- `4 STATUS2 immediately after RDSD7`, including whether bit 7 already changed.
- `5 SYSTEM2 before toggle`, `6 WRITE SYSTEM2`, `7 SYSTEM2 readback`. If the
  initial SYSTEM2 is `0x03`, the toggle write/readback should be `0x07`; if `0x07`,
  it should become `0x03`. The XOR must always be exactly `0x04`.
- All `8 STATUS2 poll` transitions and `9 OBSERVATION` timing summaries.
- Whether bit 7 changes before the explicit toggle, afterward, in both phases,
  or neither. A `0x00 -> 0x80` or `0x80 -> 0x00` is a bit-7 transition;
  other-bit changes alone are not. No transition is invented from elapsed time.
- Final RF OFF verification and receiver observations for both separate cases.

The unresolved datasheet descriptions are Rev. 2.09 pp.27, 36, 38 and 40-41:
[Quintic QN8007B datasheet](https://file2.dzsc.com/icpdf/18/07/11/671969_134915924.pdf).
The tool deliberately performs the requested observations without choosing an
acceptance interpretation. If the trace remains ambiguous, stop and retain the
complete logs rather than introducing delays, a guessed pop rule or GPIO26 use.

## Automated validation

The probe test uses the real Linux 0A encoder with fake QN/client/time objects.
It checks exact words/bytes, both explicit cases, register order, XOR preservation,
RF-OFF and over-air lifecycles, four-segment repetition, no-transition stop,
write/readback failures and cleanup, and refusal of an active bench or enabled
QN RDS interrupt output. It does not establish actual hardware behavior.

Validation: **9 probe tests passed; all 17 firmware CTest suites passed**. The
unchanged production firmware build succeeded (no rebuild needed); the probe's
CLI/import path was checked with the existing isolated Python dependencies.
No physical probe run or commit was performed.
