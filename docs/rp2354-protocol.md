# RP2354 V1 Linux wire protocol

This is the authoritative V1 binary protocol specification and pre-release firmware contract implemented by
`daemon/rp2354/protocol.py` and `daemon/rp2354/linux_client.py`.
The architecture defines command semantics and envelopes, but leaves numeric IDs,
field widths and bus transaction details open in sections 11 and 27. The choices
below resolve those points for V1. Production framing is implemented in
`firmware/fmheadend_rp2354/host_protocol.c`. This document defines RP transport;
Linux RDS construction and refill policy are documented in [Milestone 5](milestone5.md).

## Framing and bus transactions

Pi is the only master. RP uses a configured unreserved 7-bit address, 0x08..0x77;
there is deliberately no default address. The bus/device path is configurable.
All multibyte fields are little-endian; structures are packed without padding.
There are no sequence numbers. Every request and response has a CRC-16/CCITT-FALSE trailer.

Request: `command:u8 target:u8 length:u8 payload[length] crc16_le:u16`.
Response: `status:u8 length:u8 payload[length] crc16_le:u16`.
Lengths count payload bytes only; maximum payload is 255 bytes.
Targets 0..7 are physical modulators; 0xFF is global. Other targets are invalid.
Global commands reject channel targets and channel commands reject global targets.

CRC covers the complete header and payload, excluding the CRC itself. Parameters:
polynomial `0x1021`, initial value `0xFFFF`, no input/output reflection, xorout `0`.
Check vector: ASCII `123456789` produces `0x29B1` (trailer bytes `b1 29`).
The CRC trailer does **not** count toward `length`. Maximum request storage is
260 bytes; the largest valid RDS_PUSH is 254 bytes (249-byte payload, 31 groups).
GET_STATUS remains a 130-byte payload, now a 134-byte response frame.

RP validates complete request length and CRC at write STOP **before command
dispatch or any QN/FIFO side effect**. A missing, truncated or incorrect CRC is
rejected as existing status INVALID_ARGUMENT, with no command execution.
All responses include CRC, including errors, BUSY, startup BUSY and early-read
errors. New requests retain the existing abandoned-response/work semantics.

One request is outstanding at a time. Linux performs:

1. One write containing the complete request, ending with STOP.
2. Wait at least the command-specific response interval below for completion.
3. One read of exactly two header bytes, ending with STOP.
4. One read of exactly `length + 2` bytes (payload and CRC), ending with STOP.
   This read is mandatory even for an empty ACK/error payload: it reads the CRC.
5. Validate complete response length and CRC **before** interpreting status or
   decoding payload. Only a CRC-valid BUSY can authorize the existing single retry.

RP must keep a response cursor across the two read transactions: the second read
continues at the payload, not at the header. A new complete request replaces any
abandoned response and resets the cursor. No repeated START is required. The RP
must have a completed response header ready within the command-specific interval;
when not ready to execute a command it returns BUSY, with no command side effects. Delayed execution after BUSY is
forbidden. Each successful response size is fixed below; error payloads are empty.
Linux uses the unverified u8 length only to bound its second read (at most 257
bytes), then verifies CRC and command-specific length/status. It rejects
short/extra bytes returned by the OS. I2C is master-clocked, so bytes a
slave might offer beyond the requested read cannot be independently detected;
firmware must honor the declared size and new-request cursor reset.

Protocol version remains **1**, changed in place by project decision. Deploy the
Linux client and RP firmware together: old frames without CRC are incompatible
and there is no automatic fallback. CRC detects corruption; it provides neither
transaction identity nor permission to replay commands with uncertain outcomes.

The client locks requests within its process and takes an advisory exclusive lock
on the device while open, preventing other cooperating client instances from
interleaving transactions. Do not run independent bus tools against the RP while
the daemon owns it. This lock does not constrain uncooperative external programs.


### M8.3 synchronous response intervals

The operator approved these longer windows for the unreleased V1 contract. They
replace the former universal 2 ms deadline; update the Linux client together with
the firmware. There is no asynchronous completion command or early OK.

| Command | Wait after write STOP / response deadline |
|---|---:|
| GET_INFO and commands not listed below | 2 ms |
| QN_READ_REGISTER, QN_WRITE_REGISTER | 100 ms |
| SET_TX_ENABLED | 150 ms |
| GET_STATUS | 850 ms |
| SET_TX_CONFIG | 1000 ms |

The RP completes QN operations outside the I2C IRQ. The Linux client waits before
starting the header read; the adapter timeout bounds that later bus transaction,
not the preceding sleep. BUSY alone remains retryable once, with no execution
of that rejected request. A read before foreground execution starts cancels that
request and returns BUSY; a read while it executes returns INTERNAL_ERROR with
unknown outcome, and the completed operation cannot overwrite that error with a
late OK. Early reads violate the contract and are not a polling mechanism.

Only one request may be outstanding. A new request during execution returns BUSY
for the new request and abandons the previous reply; it cannot cancel hardware
work already in progress. Do not replay a command whose outcome is unknown.
See [M8.3](milestone8_3.md) for timing bounds and failure isolation.

## Command and response status IDs

| ID | Command | Target | Request payload bytes | OK payload bytes |
|---:|---|---|---:|---:|
| 0x01 | GET_INFO | 0xFF | 0 | 47 |
| 0x02 | SET_TX_CONFIG | 0..7 | 15 | 0 |
| 0x03 | SET_TX_ENABLED | 0..7 | 1 | 0 |
| 0x04 | GET_STATUS | 0xFF | 0 | 130 |
| 0x05 | RDS_PUSH | 0..7 | 1 + 8*N | 0 |
| 0x06 | RDS_CLEAR | 0..7 | 0 | 0 |
| 0x07 | QN_READ_REGISTER | 0..7 | 1 | 1 |
| 0x08 | QN_WRITE_REGISTER | 0..7 | 2 | 0 |
| 0x09 | RESET_ERROR_COUNTERS | 0xFF | 0 | 0 |
| 0x0A | RP_SOFT_RESET | 0xFF | 0 | 0 |
| 0x0B | GET_RDS_FIFO_FREE | 0xFF | 0 | 16 |

| Status ID | Name | Python exception |
|---:|---|---|
| 0 | OK | none |
| 1 | INVALID_COMMAND | InvalidCommand |
| 2 | INVALID_TARGET | InvalidTarget |
| 3 | INVALID_ARGUMENT | InvalidArgument |
| 4 | BUSY | Busy |
| 5 | QN_ERROR | QnError |
| 6 | INTERNAL_ERROR | InternalError |

All RP error responses are four bytes: `[status, 0, crc_lo, crc_hi]`. Typed status
exceptions derive from `RpResponseError`, itself a `DependencyUnavailable`, and
retain `command` and `rp_status`. Invalid framing/CRC/enums raise `ProtocolError`;
unsupported version/count raises `IncompatibleProtocol`. OS/device/slave access
failures raise `HardwareUnavailable`. Existing manager/API dependency error handling
therefore remains applicable. Local invalid arguments raise `ValueError` before I/O.

The unreleased V1 contract includes the expanded SET_TX_CONFIG and GET_STATUS
payloads below. Protocol version remains 1; command IDs, other payloads, serialized
request handling and no-sequence rule remain unchanged. CRC is mandatory on both
sides; there is no legacy-payload compatibility path.

## GET_INFO (0x01)

Request has no payload: `01 ff 00 53 f8`. OK response starts `00 2f`;
the second read returns 47 payload bytes followed by two CRC bytes.

| Payload offset | Type | Field |
|---:|---|---|
| 0 | u16 | protocol_version, exactly 1 |
| 2 | char[16] | firmware_version |
| 18 | char[16] | hardware_target |
| 34 | u8 | modulator_count, 1..8 |
| 35 | u32 | frequency_min_khz |
| 39 | u32 | frequency_max_khz |
| 43 | u32 | frequency_step_khz |

The current `rp2354-qn8x-v1` firmware reports 76000, 108000 and 50 kHz.
The raster is anchored at the reported minimum. All three values must be positive;
maximum must be at least minimum and the range must be a multiple of the step.
This is the unreleased V1 layout; 35-byte GET_INFO replies are not supported.

Strings are nonempty printable ASCII, maximum 15 characters, NUL terminated and
zero padded to 16 bytes. The public API exposes the keys `controller_protocol_version`,
`controller_firmware_version`, `hardware_target`, `modulator_count`. Unknown versions or
counts block every command except GET_INFO; no best-effort interpretation or mock
fallback is allowed. Successful GET_INFO is required before the first operational
command and after a failed/reopened session or explicit reset.

## SET_TX_CONFIG (0x02)

The payload is exactly 15 bytes. The original first ten bytes retain their order:

| Payload offset | Type | Field |
|---:|---|---|
| 0 | u32 | frequency_khz, positive and explicitly configured |
| 4 | u16 | rf_attenuation_db_x10: 0, 15, ... 225 (relative attenuation) |
| 6 | u8 | stereo: 0 mono, 1 stereo |
| 7 | u8 | preemphasis_us: literal 50 or 75 |
| 8 | u8 | audio_mode: 0 DIGITAL, 1 ANALOG, 2 SINE |
| 9 | u8 | rds_enabled: 0 or 1 |
| 10 | u8 | gain_mode: 0 AUTO, 1 MANUAL |
| 11 | i16 | analog_gain_db_x10, signed tenths of dB |
| 13 | u8 | digital_trim_db: 0 (0 dB), 1 (+1 dB) |
| 14 | u8 | soft_clipping: 0 disabled, 1 enabled |

`gain_mode` maps to QN TAGC_GAIN_SEL. AUTO lets the QN AGC FSM determine both
TXAGC_GVGA and TXAGC_GDB: persisted manual gains are carried but are not effective.
MANUAL applies `analog_gain_db_x10` through TXAGC_GVGA using the board's fixed RIN
mapping, and `digital_trim_db` through TXAGC_GDB. RP firmware owns that mapping and
rejects unrepresentable hardware gains with INVALID_ARGUMENT; Linux must not invent
RIN, round/clamp the requested gain, or transport raw GVGA codes through this field.
`soft_clipping` independently maps to TX_SFTCLPEN in either gain mode.

Linux validates finite signed tenths of dB (-3276.8..3276.7, the wire range only).
Examples: 0.0 becomes `00 00`, +6.0 becomes `3c 00`, -1.5 becomes `f1 ff`.
This range is not a claim of physical QN gain capability. Neutral persisted defaults
are AUTO, analog_gain_db=0.0, digital_trim_db=0, soft_clipping=false; AUTO ignores
both stored gain values until MANUAL is selected. No board RIN default is implied.

This applies configuration as one validated operation and **does not enable an
OFF transmitter**. Reconfiguration of an ON transmitter temporarily disables RF
and restores ON only after success. A hardware failure invalidates configuration
and isolates that QN with CEN LOW; there is no partial active configuration or
rollback claim. Argument rejection leaves the previous configuration unchanged.

SINE selects the same QN digital input as DIGITAL. M8.4 supplies local 1 kHz,
-12 dBFS stereo PCM on enabled SINE lanes; M8.3 has no generator. RF enable does not require audio clocks,
SPI input or PCM availability. M8.5 supplies fresh SPI PCM to configured DIGITAL
lanes even with RF OFF (approved for bench observation); absent/unusable SPI
produces silence without changing RF state. ANALOG I2S DATA stays zero. Linux/API
`rf_attenuation_db` is relative attenuation: 0 is Maximum; 1.5, 3.0, ... 22.5 dB
select the remaining 15 QN output-control steps. The wire carries nonnegative tenths
(0, 15, ... 225), not raw register codes. Other values are invalid.
RP firmware maps this requested attenuation to the corresponding QN output-control
step. IPOW remains board/firmware-owned and MUST NOT be changed by this setting.
No calibrated absolute output or dBÂµV conversion is defined. The former approximate
PA-target formula is not a mapping for these steps and must not be used.

A real SET_TX_CONFIG requires an explicitly selected attenuation. Fresh schema-1 databases initialize relative attenuation to null. The high-level mock still permits unconfigured targets in tests.

Example TX7, 98100 kHz, 6.0 dB attenuation, stereo, 50 us, DIGITAL, RDS disabled,
default audio controls: `02 07 0f 34 7f 01 00 3c 00 01 32 00 00 00 00 00 00 00`.
ACK: `00 00`. Protocol version remains 1; byte sizes, command IDs and offsets are unchanged.

## SET_TX_ENABLED (0x03)

Payload offset 0: `enabled:u8`, exactly 0 or 1. No other fields.
`03 00 01 01` enables TX0; `03 00 01 00` disables it. ACK: `00 00`.
The firmware must reject enabling an unconfigured modulator with INVALID_ARGUMENT.
The Linux manager retains configuration-before-enable and RF-off-before-audio-stop.

## GET_STATUS (0x04)

Request `04 ff 00`. Response `00 82` followed by one coherent 130-byte snapshot.
Global prefix is ten bytes:

| Payload offset | Type | Field |
|---:|---|---|
| 0 | u8 | global_state: 0 OK, 1 ERROR |
| 1 | u32 | uptime_ms, wraps modulo 2^32 |
| 5 | u32 | rp_spi_audio_link_error_count |
| 9 | u8 | global last_error_code, status IDs above; 0 means none |

Eight consecutive 15-byte records follow, ordered TX0..TX7. Channel `c` starts at
payload offset `10 + 15*c`; no repeated channel ID is transmitted.

| Record offset | Type | Field |
|---:|---|---|
| 0 | u8 | tx_state: 0 OFF, 1 ON, 2 ERROR |
| 1 | i16 | rf_attenuation_db_x10: 0, 15, ... 225; -32768 means unavailable |
| 3 | u32 | audio_underrun_count |
| 7 | u32 | rds_underrun_count |
| 11 | u16 | rds_fifo_free, capacity in complete groups |
| 13 | u8 | last_error_code, status IDs above; 0 means none |
| 14 | u8 | input_saturated: 0 false, 1 true, 255 unavailable |

Total payload is `10 + 8 * 15 = 130` bytes; all pre-existing per-TX fields retain
their offsets. In ANALOG mode firmware reads QN STATUS1.INSAT (1 means analog input
level too high/channel saturated). DIGITAL and SINE modes must return 255. Linux exposes
`input_saturated: bool | null`, masks it to null for DIGITAL, and invalidates it
on RP loss. It is a current snapshot, neither a counter nor a latched error.

RF readback is the currently configured relative attenuation, not a calibrated
absolute output or measured RF power. Linux exposes `rf_attenuation_db` as 0..22.5
in 1.5 dB steps, or null for unavailable (-32768). Invalid non-sentinel values are
rejected. No absolute conversion or IPOW change is performed by Linux.
Counters are unsigned cumulative counts; FIFO free capacity is an instantaneous
snapshot. Linux transports these values without running a refill policy.

### Current faults, latched history, and diagnostic layers

`global_state` represents the current RP-wide condition: ERROR means an active
global fault. `last_error_code` records the most recently latched error. Thus
`global_state=OK, last_error_code=QN_ERROR` is valid history, not an active fault.
Per-channel `tx_state` and `last_error_code` use the same distinction. Linux retains
latched codes in diagnostics but does not mark the system degraded solely for
latched history. RESET_ERROR_COUNTERS clears codes and counters; active faults may
immediately assert again.

`rp_spi_audio_link_error_count` counts cumulative RP-detected Pi-to-RP SPI audio
transport/frame errors: missing/unexpected frame counters, invalid frame lengths,
SPI/PIO/DMA receive framing or overflow failures, and other conditions making a
frame unusable. An absent Linux source or generated silent PCM must not increment
it. With no SPI checksum/CRC, same-length undetected bit corruption cannot always
be counted.

Per-channel `audio_underrun_count` counts **detected QN8007B I2S underrun events**.
Firmware monitors the QN underrun flag, increments the counter when observed, and
clears the flag using the QN-supported mechanism. Several physical underruns between
polls may collapse into one detection because the chip exposes a flag, not an event
queue. This is separate from Linux mux underruns (missing PCM before SPI) and RP
SPI audio link errors (transport faults). QN I2S overflow has no dedicated status field; raw register diagnostics remain available.
Analog saturation is the current `input_saturated` snapshot defined above.

## RDS_PUSH (0x05)

Payload offset 0: `group_count:u8`, 1..31. Exactly N groups follow, each eight bytes:
`block_A:u16 block_B:u16 block_C:u16 block_D:u16`, in that order, all little-endian.
Group count 31 produces 249 payload bytes; zero groups and incomplete groups are
invalid. Blocks are already formed RDS data words, not text. No checkwords, RDS
encoding, group selection, FIFO scheduler or carousel is performed by this client.

The caller must respect its latest FIFO free-capacity snapshot. RP must append all
N groups atomically in order, or return BUSY without appending any if capacity is
insufficient. Partial acceptance is not representable in V1. The bounded BUSY retry
reuses exactly the same batch. ACK: `00 00`.

## RDS_CLEAR (0x06)

No payload; target is one modulator. Empties that FIFO. ACK: `00 00`.
It does not change configuration or TX enable. This is a primitive only and is
used by the Milestone 5 service to establish clean content/lifecycle boundaries.

## QN_READ_REGISTER (0x07)

Request payload offset 0: `register:u8`. Response payload offset 0: `value:u8`.
Example: read TX2 register 0x12: `07 02 01 12 cf ba`;
value 0xA5: `00 01 a5 e2 1a` (header read `00 01`, second read `a5 e2 1a`).
Firmware owns QN routing/access. M8.3 permits all u8 register addresses, including
SYSTEM1, and returns the actual bus result; the datasheet still governs register
meaning. Each transaction gets at most two retries before QN_ERROR.

## QN_WRITE_REGISTER (0x08)

Request payload offsets 0 and 1: `register:u8 value:u8`. ACK: `00 00`.
Example: write TX3 register 0x34 to 0x56: `08 03 02 34 56`.
Linux does not implement a QN driver or choose hardware register values.
M8.3 updates reported state after relevant raw writes. A raw address change or
SWRST is executed, then the affected QN is held at CEN LOW to avoid address
collisions; reset the RP to reassign it. Such a successful raw command returns OK
while GET_STATUS reports the resulting unavailable-channel fault. Other raw
configuration overrides invalidate configuration-before-enable eligibility;
SYSTEM1 TXREQ-only changes retain it. Raw SYSTEM1 can enable RF without a preceding
SET_TX_CONFIG, as explicitly permitted for this diagnostic interface.

## RESET_ERROR_COUNTERS (0x09)

Global request `09 ff 00`, no payload. ACK `00 00`. Clears RP global and per-channel
error/underrun counters and latched error codes; current active errors may reassert.
Does not reset uptime, TX configuration, enable state or FIFO content. The Linux
client's cumulative `i2c_errors` and mux counters are separate and are not cleared.

## RP_SOFT_RESET (0x0A)

Global request `0a ff 00`, no payload. Firmware must provide ACK `00 00` before
resetting. After ACK consumption it resets with TX disabled and FIFOs empty.
Linux closes the session, invalidates compatibility information, and waits at
least 250 ms before another attempt. A fresh GET_INFO is mandatory. A lost ACK
leaves outcome unknown; Linux reports failure and never automatically replays the
reset. Automatic desired-state restoration and RDS refill are not added here.

## GET_RDS_FIFO_FREE (0x0B)

Global request `0b ff 00 92 3f`, no payload. Successful header `00 10`, followed by
exactly eight little-endian u16 capacities: TX0 at offset 0, TX1 at 2, TX2 at 4,
TX3 at 6, TX4 at 8, TX5 at 10, TX6 at 12 and TX7 at 14. Total payload: 16 bytes;
the second read is 18 bytes including CRC.
Each capacity counts complete eight-byte RDS groups. The typed client method
`get_rds_fifo_free()` returns eight integers in channel order.

The Milestone 5 Linux refill service uses this primitive for capacity queries;
GET_STATUS remains the full diagnostic snapshot. Scheduling policy is not part
of this RP transport protocol.
RDS_PUSH still acknowledges with an empty payload; `fifo_free_after` is not V1.

Production M8.6B (`0.8.6b`) retains M8.6A's 64-group per-TX FIFO contract and
adds polling-only QN consumption. GET_STATUS remains 130 bytes; FIFO free counts
increase on confirmed QN handoff acceptance (or explicit CLEAR), and genuine
consumer starvation is counted once per episode. RDS_PUSH ACK still means queued,
not proven on air. CLEAR discards queued data without changing RF/configuration;
an already-started QN handoff may finish, but cannot pop post-CLEAR data.
These RAM-only commands retain the 2 ms response window. All other response waits
and protocol V1 framing are unchanged. See [M8.6B](milestone8_6b.md) for consumer
ownership, fault/recovery semantics, Linux buffering and physical validation.

## Linux timing, retry and integration behavior

The real client uses standard-library `os.open/read/write/close` and `fcntl` ioctls,
not SMBus block framing. It requires plain-I2C adapter capability. Default adapter
timeout is requested as 200 ms (configurable 10..1000 ms, rounded up to 10 ms units).
Kernel arbitration retries are set to zero to avoid replaying commands with side
effects. These ioctls configure adapter-level settings; use a dedicated control bus.
The kernel/controller governs actual timeout behavior; Python cannot preempt a
stuck kernel syscall. Hardware timing still requires validation on the real board.

BUSY alone is retried once after 10 ms. All other RP errors are surfaced immediately.
A short write, short read, malformed/CRC-failed response, NACK or OS error closes the session;
there is no blind request replay because execution may already have happened.
Reopening waits at least 250 ms. Existing periodic status polling retries communication
and performs a fresh handshake; no extra recovery daemon or fake bus is introduced.
Reconnection alone never restores hardware configuration. Linux failures carry
command/target/stage and NOT_SENT, AMBIGUOUS, OBSERVATION or valid RESPONSE
certainty; this metadata is Linux-local and does not change any wire bytes.
Read failures retain stale observations and use bounded later retries. Ambiguous
RDS operations establish a target-only CLEAR boundary; ambiguous enable writes
reapply only that TX's desired state. Ambiguous configuration writes reconcile
only their target. See [M6A](milestone6a.md) for current recovery policy.
CRC failures use that same execution-certainty model: read-only commands are
failed observations, sent side-effecting commands remain AMBIGUOUS, and an
implicit GET_INFO failure before a SET leaves that SET NOT_SENT. No global TX
restore is triggered by CRC failure. An uncertain RDS_PUSH is never blindly
replayed; only its target is fenced until a positively acknowledged CLEAR.
The existing once-per-incident raw response log includes the CRC trailer in its
`payload`/`payload_length` fields, exactly as returned by the second Linux read.
An unavailable device does not prevent the HTTP application from starting.

The real CLI is the default. `--mock-controller` explicitly selects the unchanged high-level
`MockRp2354Client`; `--mock` selects all original mocks. `--mock-mux` affects only
mux selection. The embedding `create_app()` keeps its original mock defaults;
pass `rp=Rp2354Client(device, address)` to opt into real control.

Runtime status adds independent RP `hardware_tx_state`, `audio_underrun_count`,
`rds_underrun_count`, `rds_fifo_free`, `hardware_last_error`, plus global
`controller_uptime_ms`, `controller_spi_audio_link_error_count` and latched `controller_last_error_code`. Real reported TX state updates `tx_state`.
On communication failure, the last valid measurements/counters and TX states are
retained with `controller_observation_stale=true`, `controller_observation_age_s` and a separate
`controller_communication_state`. Before the first valid snapshot, hardware values remain
unknown. Transport uncertainty is not a confirmed TX hardware ERROR.
Persistent desired state and the existing PCM streams are retained. A subsequent
snapshot restores observed status; it does not silently enable RF or restore desired
configuration. Explicit start remains available. Mux `underruns`, `overruns` and PCM
counters retain their Milestone 3 meanings. API handlers never handle protocol bytes
or open device nodes. Hardware info is cached through the status service.


## Extension: factory A/B firmware update (FLASH_UPDATE, 0xF0)

Global target 0xFF; unchanged V1 CRC16 framing and two-stage READ. OTA format 2
is incompatible with the previous OTA payloads. Non-OTA commands are unchanged.
All integers are little-endian; image offsets are relative to the inactive slot.

| Subcommand | Code | Payload including subcommand |
|---|---|---|
| BEGIN | 1 | size:u32, SHA256[32] (37 bytes total) |
| WRITE | 2 | offset:u32, data[1..250] |
| STATUS | 3 | no arguments |
| VERIFY | 4 | no arguments |
| ACTIVATE | 5 | no arguments |
| CONFIRM | 6 | no arguments |

There is no upload identifier or ABORT. BEGIN always starts at zero and replaces
incomplete work, but refuses pending trial/uncertain finalization. WRITE offsets
are contiguous; an already accepted identical block may be acknowledged again.
The receiver assembles 256-byte flash pages independently of packet boundaries.

Successful WRITE returns 9 bytes: format:u8=2, accepted:u32, committed:u32.
Other successful OTA commands return this 112-byte STATUS:

| Offset | Size | Field |
|---|---|---|
| 0 | 1 | format = 2 |
| 1 | 1 | state: IDLE=0, RECEIVING=1, VERIFIED=2, ACTIVATING=3, CONFIRMED=4, INTERRUPTED=5, FAILED=6 |
| 2..7 | 6 | boot slot, target slot, TBYB pending, error, maintenance, ready |
| 8,12,16 | 4 each | accepted offset, committed offset, image size |
| 20 | 32 | expected SHA256 |
| 52 | 16 | image build ID (obtained during VERIFY) |
| 68 | 16 | running build ID |
| 84 | 4 | running ROM major:u16, minor:u16 |
| 88 | 16 | running firmware version, NUL padded |
| 104 | 4 | slot capacity = 0xF0000 |
| 108 | 2 | maximum WRITE data = 250 |
| 110 | 1 | inactive slot |
| 111 | 1 | reserved zero |

Unavailable slot/TBYB values are 255. Error codes: 0 none, 1 layout, 2 argument,
4 offset, 5 flash, 6 hash, 7 image, 8 state, 9 quiescence, 10 ROM.
Outer errors have no payload. Every response, including BUSY, carries CRC16.
For OTA only, BUSY is a completion poll: read again after 10 ms, without writing
a new request. The client starts reading after 2 ms and bounds this wait at
3 seconds. Non-OTA BUSY/retry behavior is unchanged. A new complete request
abandons the previous response; after an uncertain mutation use STATUS first.

After reset, incomplete writes require BEGIN, never a remembered host offset.
VERIFY hashes the complete flash image and checks native IMAGE_DEF/identity.
ACTIVATE/CONFIRM use the existing native ROM TBYB lifecycle. Reboots require a
complete successful ACK including CRC and final STOP; partial BUSY reads cannot
release the reboot gate. CONFIRM checks buy success and durable finalization;
the host verifies normal boot and RF OFF, not merely the disappearance of TBYB.
See the [update lifecycle](rp2354-firmware-update.md#update-maintenance-and-confirmation).
