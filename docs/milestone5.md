# Milestone 5: Linux RDS

M5 implements information-group encoding, per-modulator content scheduling and RP
FIFO refill on Linux. RP firmware remains a transport endpoint. No RDS checkwords
are generated: each group is exactly four unsigned 16-bit information words.
The separate I2C frame CRC belongs to the RP client, not the RDS encoder.
The [RP2354 protocol](rp2354-protocol.md) remains the only RP wire specification.

## Configuration

Per-modulator fields are `rds_enabled`, `rds_pi`, `rds_ecc`, `rds_pty`, `rds_tp`,
`rds_ps`, `rds_af_khz`, `rds_rt`, `rds_rt_enabled`, `rds_rt_termination` and
`rds_metadata_to_rt`. PI remains an integer
uint16 in the API; ECC follows that style as nullable integer uint8. For example,
PI 4660 represents hex 1234 and ECC 224 represents hex E0. Hex strings are not
accepted in JSON. No country code is inferred. Null ECC means no ECC group is sent.

`rds_rt_enabled` defaults to true, preserving RT service for existing records that
omit this field. It alone enables/disables 2A scheduling; an empty text or missing
metadata never disables RT. `rds_rt_termination` independently selects `CR`
(default) or `PAD`. These are persistent per-TX backend/API settings; no UI or
RP protocol field is added. Existing Stop-before-edit API lifecycle rules apply.

Fresh pre-V1 schema-1 databases seed rds_ecc=null and rds_af_khz=[] from the current model. Old development databases are unsupported and may be recreated; see architecture section 16.4.

`rds_af_khz` is an ordered list of at most 25 integer frequencies, each exactly on
the 100 kHz raster from 87600 through 107900 kHz. The encoder alone derives AF codes.
There is no discovery, sorting, deduplication or automatic AF generation.

Example PATCH body (configure the actual PI/ECC/frequencies for your service):

```json
{
  "rds_enabled": true,
  "rds_pi": 4660,
  "rds_ecc": 224,
  "rds_pty": 10,
  "rds_tp": true,
  "rds_ps": [{"text": "RADIO FM", "duration_s": 5}],
  "rds_af_khz": [98100, 101100],
  "rds_rt": "Persistent programme information",
  "rds_rt_enabled": true,
  "rds_rt_termination": "CR",
  "rds_metadata_to_rt": true
}
```

Scheduling runs only for locally active modulators with RDS enabled, including
ANALOG modulators. M6A restores persistent desired TX state through the safe recovery path described
in [Milestone 6A](milestone6a.md). The GStreamer and mux paths are unchanged.

## Standard and information groups

The classic baseline is [EN 50067:1998](https://poli.cs.vsb.cz/edu/apps/lab/EN50067.pdf),
sections 3.1.5/3.2.1.6, Annex D.2 and Annex E. No conflicting ECC layout was found
in the repository. ECC uses **1A variant 0**, not paging-specific variants.

Every group carries PI in A. B contains type in bits 15..12, version A=0 in bit 11,
TP in bit 10 and PTY in bits 9..5. In 0A, B carries TA=0, M/S=1 and PS address;
DI stereo d0 is sent at address 3, other DI bits zero. D carries two PS characters.
C cycles AF Method A pairs: prefix 224+N then N frequency codes; odd total byte
counts are padded with 205. Empty AF is E0 CD. In 1A, C contains ECC in bits 7..0;
LA, variant and paging OPC are zero; D PIN is zero. In 2A, B bit 4 is Text A/B,
low four bits the segment; C/D carry four text characters. RT termination follows
the per-TX CR/PAD setting below.

Exact-word tests use independently stated expected words rather than a decoder.
All words are passed to the existing RDS_PUSH serializer, which handles little-endian
wire representation; character order inside a word is high-byte first.

## Character policy

`rds/characters.py` implements a deliberately conservative subset of the RDS basic
character table. It accepts ASCII printable positions 0x20..0x7E except 0x24, 0x5E,
0x60 and 0x7E, whose RDS glyphs differ. Unsupported characters become `?` (0x3F).
This includes raw controls, emoji and unsupported scripts; UTF-8 bytes never reach
an information group.

Input is NFC normalized, then NFKD transliterated with combining marks removed.
Explicit mappings cover curly quotes, long dashes, sharp-s to ss, AE/OE ligatures
and stroked O. Conversion occurs before the byte-length limit: an expansion can
be truncated. The original persistent Unicode text is preserved. This policy is
isolated so a fuller RDS character map can be added without changing group scheduling.

PS is truncated to eight encoded characters and space padded to exactly eight.
CR mode reserves one byte for 0x0D: up to 63 encoded text characters followed by
CR, padding only the final four-byte segment with spaces. Only those segments
are transmitted. This makes termination explicit even for a 64-character input
(whose final character is omitted in CR mode). PAD mode keeps up to 64 encoded
characters, space-pads to 64 bytes and transmits all 16 segments, without CR.
With RT enabled, empty CR text sends one CR segment; empty PAD text sends all
16 blank segments. An empty override is not replaced by a lower-priority value.

## Content and independent state

One PS item is static; its duration is ignored. Multiple items form an ordered
carousel. The next item is selected only after the current duration has elapsed
on the monotonic clock. Its first emitted PS segment is zero. Very short durations
can prevent a full PS cycle; normal stable PS identification needs enough airtime.
AF pair cycling is independent of the PS four-segment cycle and preserves list order.

RadioText uses the existing selector: active API override, then current source
metadata when `rds_metadata_to_rt` is enabled, then persistent fallback. Override
TTL expiry reveals the next source without writing the fallback to SQLite. Existing
M1 API compatibility is retained: an omitted/null TTL is runtime-only until clear
or restart; supplied TTLs expire automatically. Shared source metadata is selected
independently for each modulator.

Text A/B is independent per modulator and toggles only when the resulting encoded
RT changes. Repeated metadata, segment wraparound, or a different origin providing
the same content does not toggle it. Changed text restarts at segment zero. Character
conversion yielding identical transmitted bytes also avoids an unnecessary toggle.
RT changes do not reset dynamic PS duration or allocator credits. A/B handling
continues to compare encoded bytes: enabling/disabling unchanged RT alone does
not toggle A/B; a CR/PAD change toggles it if the encoded bytes change.

## Scheduling policy

`rds/allocator.py` derives quotas and selects group types;
`rds/scheduler.py` owns content/segment state per channel.
It has no RP, database, API or GStreamer dependency. A bounded speculative copy
produces a batch and its candidate next state; callers commit that state on ACK.

Allocation is expressed as fractions of all groups, not independent groups/s:

```text
base_ps = 0.60
base_rt = 0.30 if RT enabled else 0
ecc     = 0.02 if ECC enabled else 0
other   = sum(quotas of enabled additional types)
surplus = 1 - base_ps - base_rt - ecc - other

require surplus >= 0
PS = base_ps + (surplus if RT disabled else 0)
RT = base_rt + (surplus if RT enabled else 0)
ECC = ecc
additional types retain their individual quotas
```

With no additional types this **derives**, rather than hardcodes:

| Enabled content | PS (0A) | RT (2A) | ECC (1A) |
|---|---:|---:|---:|
| PS, RT, ECC | 60% | 38% | 2% |
| PS, RT | 60% | 40% | 0% |
| PS, ECC | 98% | 0% | 2% |
| PS only | 100% | 0% | 0% |

`quotas(..., additional={name: fraction, ...})` accepts independently configured
enabled extension quotas; absent/zero entries emit nothing. Reserved names,
negative/nonfinite values and infeasible totals raise `ValueError`. For example,
with RT/ECC enabled, additional quotas totaling 8% leave PS/RT/ECC at 60/30/2%;
anything above 8% is rejected. Minima and ECC are never scaled down. This is an
internal extension point, not a setting for unimplemented on-air group types.
Adding a real type later requires its encoder/segment state and configuration
wiring, but no change to allocation math or the FIFO contract.

Selection uses smooth weighted round robin: add each type's integer weight to
its credit, select the largest credit (stable tie order), subtract total weight
from the selected type. Quotas use exact rational arithmetic during setup;
per-group selection uses only integer arithmetic and O(number of types) work.
Credits survive batch boundaries and RT text changes; no rigid 100-group batch,
wall-clock ECC preemption or RT boost overrides the quota. The existing physical
group interval remains 104/1187.5 seconds. Long-run shares apply to a continuous
stable configuration; CLEAR or transport loss can discard already queued groups.

Tests cover 10,003-group prefixes, a ten-minute scheduler run, inter-group gaps,
arbitrary rational extension quotas and identical results with batch sizes
1/7/31. With RT enabled the basic mix has no run longer than two identical group
types. With RT disabled, long PS runs are mathematically necessary at 98–100% PS;
the allocator spaces ECC rather than clustering it to meet a batch percentage.

The projected FIFO timeline now has 2.0 s lookahead, normally about 23 queued
groups. M8.6B increased the original 0.5 s / six-group horizon to cover the existing
850 ms GET_STATUS wait on the serialized control executor, plus refill phase and
jitter. This Linux buffering margin does not change the 100 ms cadence, total
group rate or CLEAR/lifecycle boundaries. The quota policy above replaces the
original M5 weighting. Dynamic PS switching is decided using current monotonic time, never early
because of lookahead; queued older segments can delay the on-air transition by
that bounded lookahead. Actual RP consumption/timing still needs physical testing.
When RT is enabled, empty RT is transmitted according to CR/PAD mode.

## Refill and failure behavior

`rds/refill.py` runs every 100 ms on the existing serialized control executor. It
queries GET_RDS_FIFO_FREE once for all ready channels, never full GET_STATUS for
refill. Idle/disabled channels cause no normal refill queries. It respects each
fresh capacity, subtracts accepted groups locally, limits each push to 31 and limits
a channel to two batches per tick. Production lookahead normally needs only one
small push. No normal user setting controls this cadence.

BUSY means no acceptance: the exact pending group tuple and candidate scheduler
state are retained. Retry waits for enough newly reported capacity. Neither segment
indices nor accepted counters advance before ACK. New content invalidates a BUSY
batch and establishes a fresh FIFO boundary first.

For a transport failure with uncertain execution, the batch is never blindly
replayed. Only that channel's pending groups are discarded; after the shared
transport/client backoff, a successful RDS_CLEAR is required before restarting
PS/RT/AF segments coherently. CLEAR and refill to the two-second lookahead happen
in the same recovery tick, without an extra one-second or 100 ms delay.
A failed clear blocks new pushes. This deliberately permits a gap and discards
possibly queued old groups; it avoids silent replay/reordering. It cannot retract
anything already transmitted before the failure. Persistent config remains intact.
A capacity-query failure does not consume pending scheduler state or initiate
hardware restoration. A known NOT_SENT PUSH retains its pending batch without
CLEAR. Neither path interrupts RF/audio or resets another channel's scheduler.

Disable/stop drops Linux pending groups and clears the RP FIFO. Re-enable starts
coherently after a successful clear. Content configuration changes also establish
a clean FIFO boundary. RT enable/termination changes participate in that same
target-only boundary: discard pending batches, require acknowledged RDS_CLEAR,
then refill using the new allocation/format. Failed CLEAR blocks that TX's
PUSHes; other TXs are not reset and no RF command is issued. A group already
handed to the QN may finish; CLEAR cannot retract it. While RT is disabled,
metadata changes alone do not clear the remaining PS/ECC stream.
M5 itself introduced no RP reboot detection, RF desired-state restore, systemd
service or firmware. Lifecycle recovery is now described in [Milestone 6A](milestone6a.md).

RDS errors are caught and exposed through existing modulator status fields:
`rds_state`, `rds_last_error`, `rds_groups_accepted`, `rds_text_ab`. `rds_fifo_free`
continues to mean free groups, and the RP's RDS underrun counter keeps its original
meaning. Accepted groups mean acknowledged FIFO writes, not verified RF transmission.
No new REST surface is introduced. The media callback never schedules RDS or waits
for I2C. Polling and lifecycle operations share the existing serialized worker.

## Mock end-to-end smoke

The existing MockRp2354Client now models bounded per-channel RDS FIFOs (64 groups),
atomic BUSY rejection and clock-driven consumption. Its diagnostic history/call
logs are bounded to 256 entries. This is high-level mock behavior, not another
I2C implementation. Fake-clock tests require no sleeps or hardware.

```sh
. "$HOME/venvs/fmheadend/bin/activate"
python -m fmheadendd.rds.smoke
```

The command creates temporary SQLite configuration, starts a mock analog modulator,
selects an override with a two-second TTL, schedules groups for six simulated
seconds, and prints the fallback selected after expiry. Output includes PI/ECC/PS/AF
configuration, group counts, first four-word groups, FIFO query count and push count.
It asserts that 0A, 1A and 2A were all generated. `physical_transmission_tested` is
explicitly false. No permanent database or real hardware is accessed.

## Validation commands

```sh
python -m pytest daemon/tests/test_rds.py -q -ra
MUXD_BINARY="$HOME/build/fmheadend-muxd-m3/fmheadend-muxd" \
  FMHEADEND_TEST_ALSA_DEVICE=plughw:2,0 \
  python -m pytest daemon/tests -q -ra
ctest --test-dir "$HOME/build/fmheadend-muxd-m3" --output-on-failure
```

Windows: use the existing Python environment and run `python -m pytest daemon/tests -q -ra`.
No new Python or system dependencies are required.

Intentionally excluded: Method B, regional AF, EON-AF, automatic AF discovery,
RT+, TMC, ODA, CT, EON, firmware, QN driver, Web UI, and the recovery milestone.
No physical RP/QN RDS transmission has been tested. There is no architectural
component-boundary deviation; AF Method A is included by explicit user instruction.


## Historical M5 validation (before quota allocator)

On Raspberry Pi OS / Python 3.13, the M5 focused suite passed 45 tests; the complete
Python suite passed 294 tests with zero failures and zero skips, including native
GStreamer HTTP/ALSA and mux integration. Native mux CTest passed all 8 tests.
Windows passed 274 tests, failed zero, and skipped 20 platform-dependent tests
(15 GStreamer, 4 Linux mux, 1 Linux I2C unavailable-device test). The focused M5
count is a subset of the complete Python count. Both Python runs reported two
existing dependency deprecation warnings.

The deterministic smoke accepted 80 groups: 31 x 0A, 2 x 1A/ECC and 47 x 2A,
using 55 FIFO queries and 55 pushes. First information words were:

| Group | A | B | C | D |
|---|---|---|---|---|
| 0A | 1234 | 0548 | E188 | 5241 |
| 1A/ECC | 1234 | 1540 | 00E0 | 0000 |
| 2A | 1234 | 2540 | 5465 | 6D70 |

These hexadecimal words prove Linux encoding and mock FIFO delivery only.
RP/I2C protocol unit tests mock low-level calls; no connected RP/QN hardware was
used. Native audio/mux tests do not establish physical RDS transmission.
