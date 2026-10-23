# Controller protocol

FMheadend Controller protocol, version **1**. The host communicates with the Controller over I²C; PCM audio uses a separate SPI interface.

## 1. Interface and addressing

The host is the I²C master; the Controller is the slave (default 7-bit address **`0x62`**). Only one request may be outstanding at a time.

| Target | Address | Description |
|---|---|---|
| Global | `0xFF` | Commands applying to the Controller |
| Physical TX | `0..N-1` | Individual transmitters; `N` is reported by `GET_INFO` (`1..8`) |

Eight internal TX slots exist regardless of physical population. `GET_STATUS` and `GET_RDS_FIFO_FREE` return eight entries; entries `N..7` are placeholders. Commands directed at absent transmitters are rejected.

## 2. Frame format

All multibyte integers and CRC trailers are **little-endian**. Payload structures are packed, without padding.

**Request**

| Offset | Size | Field |
|---:|---:|---|
| 0 | 1 | Command ID |
| 1 | 1 | Target |
| 2 | 1 | Payload length (`0..255`) |
| 3 | `length` | Payload |
| `3 + length` | 2 | CRC-16 |

**Response**

| Offset | Size | Field |
|---:|---:|---|
| 0 | 1 | Status |
| 1 | 1 | Payload length (`0..255`) |
| 2 | `length` | Payload |
| `2 + length` | 2 | CRC-16 |

The CRC covers the header and payload, excluding its own two bytes: **CRC-16/CCITT-FALSE**, polynomial `0x1021`, init `0xFFFF`, no reflection, XOR-out `0` (check: `123456789` → `0x29B1`). An empty ACK or error response is four bytes.

### 2.1 Transaction sequence

| Step | I²C operation |
|---:|---|
| 1 | Write the entire request, then **STOP** |
| 2 | Wait for the command (see [timing](#5-status-codes-timing-and-failures)) |
| 3 | Read the two-byte response header, then **STOP** |
| 4 | Read `length + 2` bytes (payload and CRC), then **STOP** |
| 5 | Validate length, CRC, status and expected payload size |

The second read is required even when `length = 0`. The Controller preserves the response position between the two reads. A new request discards the previous response.

**Complete wire examples** (hex, including CRC):

| Frame | Bytes |
|---|---|
| `GET_INFO` request | `01 ff 00 53 f8` |
| `GET_RDS_FIFO_FREE` request | `0b ff 00 92 3f` |
| Empty `OK` response | `00 00 0f 1d` |
| `QN_READ_REGISTER`, TX2, register `0x12` | `07 02 01 12 cf ba` |
| `OK` register-read response, value `0xA5` | `00 01 a5 e2 1a` |

## 3. Command list

Payload sizes exclude the I²C header and CRC. All successful response sizes are exact.

| ID | Command | Target | Request (bytes) | OK response (bytes) |
|---|---|---|---:|---:|
| `0x01` | [GET_INFO](#41-get_info-0x01) | Global | 0 | 47 |
| `0x02` | [SET_TX_CONFIG](#42-set_tx_config-0x02) | TX | 15 | 0 |
| `0x03` | [SET_TX_ENABLED](#43-set_tx_enabled-0x03) | TX | 1 | 0 |
| `0x04` | [GET_STATUS](#44-get_status-0x04) | Global | 0 | 130 |
| `0x05` | [RDS_PUSH](#45-rds_push-0x05) | TX | `1 + 8*G` | 0 |
| `0x06` | [RDS_CLEAR](#46-rds_clear-0x06) | TX | 0 | 0 |
| `0x07` | [QN_READ_REGISTER](#47-qn_read_register-0x07) | TX | 1 | 1 |
| `0x08` | [QN_WRITE_REGISTER](#48-qn_write_register-0x08) | TX | 2 | 0 |
| `0x09` | RESET_ERROR_COUNTERS | Global | 0 | — |
| `0x0A` | RP_SOFT_RESET | Global | 0 | — |
| `0x0B` | [GET_RDS_FIFO_FREE](#49-get_rds_fifo_free-0x0b) | Global | 0 | 16 |
| `0x0C` | [GET_SPI_DIAGNOSTICS](#410-get_spi_diagnostics-0x0c) | Global | 0 | 32 |
| `0xF0` | [FLASH_UPDATE](#6-rp2354-firmware-update-extension) | Global | Variable | 9 or 112 |

`0x09` and `0x0A` are defined in the host client but **not implemented by the firmware**; they return `INVALID_COMMAND`. `FLASH_UPDATE` is detailed in [section 6](#6-rp2354-firmware-update-extension).

## 4. Command reference

An empty successful response means status `OK`, payload length `0`, followed by CRC (four bytes total). All offsets below are relative to the **payload**.

### 4.1 GET_INFO (`0x01`)

Global target. Empty request payload. **Response: 47 bytes.**

| Offset | Type | Field | Meaning / allowed values |
|---:|---|---|---|
| 0 | `u16` | `protocol_version` | `1` |
| 2 | `char[16]` | `firmware_version` | NUL-terminated version string (e.g. `1.0.0`) |
| 18 | `char[16]` | `hardware_target` | NUL-terminated hardware family (e.g. `rp2354-qn8x-v1`) |
| 34 | `u8` | `modulator_count` | Physical TX count, `1..8` |
| 35 | `u32` | `frequency_min_khz` | Lowest supported frequency, kHz |
| 39 | `u32` | `frequency_max_khz` | Highest supported frequency, kHz |
| 43 | `u32` | `frequency_step_khz` | Frequency raster step, kHz |

Strings are printable ASCII, at most 15 characters, NUL-terminated and zero-padded. The tuning raster starts at `frequency_min_khz`.

### 4.2 SET_TX_CONFIG (`0x02`)

Physical TX target. **Request: 15 bytes. Response: empty ACK.**

| Offset | Type | Field | Values |
|---:|---|---|---|
| 0 | `u32` | `frequency_khz` | Within GET_INFO limits and raster |
| 4 | `u16` | `rf_attenuation_db_x10` | `0, 15, 30, …, 225` (0–22.5 dB) |
| 6 | `u8` | `stereo` | `0` mono, `1` stereo |
| 7 | `u8` | `preemphasis_us` | `50` or `75` |
| 8 | `u8` | `audio_mode` | `0` DIGITAL, `1` ANALOG, `2` SINE |
| 9 | `u8` | `rds_enabled` | `0` off, `1` on |
| 10 | `u8` | `gain_mode` | `0` AUTO, `1` MANUAL |
| 11 | `i16` | `analog_gain_db_x10` | MANUAL: `-15..150`, step `15` |
| 13 | `u8` | `digital_trim_db` | `0` or `1` |
| 14 | `u8` | `soft_clipping` | `0` off, `1` on |

RF attenuation is relative to the configured maximum, **not an absolute output-power value**. Reconfiguring an ON transmitter temporarily turns it off; it is re-enabled only on success. A failed hardware configuration can invalidate that TX's configured state.

### 4.3 SET_TX_ENABLED (`0x03`)

Physical TX target. **Request: 1 byte. Response: empty ACK.**

| Offset | Type | Field | Values |
|---:|---|---|---|
| 0 | `u8` | `enabled` | `0` OFF, `1` ON |

Enabling an unconfigured TX is rejected. This command controls RF enable; it does not verify audio availability.

### 4.4 GET_STATUS (`0x04`)

Global target. Empty request payload. **Response: 130 bytes.**

**Global fields (10 bytes):**

| Offset | Type | Field | Values / meaning |
|---:|---|---|---|
| 0 | `u8` | `state` | `0` OK, `1` ERROR |
| 1 | `u32` | `uptime_ms` | Milliseconds, wraps at 2³² |
| 5 | `u32` | `rp_spi_audio_link_error_count` | Detected SPI/audio-frame errors |
| 9 | `u8` | `last_error_code` | Latched status/error code |

**TX records:** eight records of 15 bytes each; TX `i` starts at payload offset `10 + 15*i`.

| Record offset | Type | Field | Values / meaning |
|---:|---|---|---|
| 0 | `u8` | `tx_state` | `0` OFF, `1` ON, `2` ERROR |
| 1 | `i16` | `rf_attenuation_db_x10` | `-32768` if unavailable |
| 3 | `u32` | `audio_underrun_count` | Currently reported as `0` |
| 7 | `u32` | `rds_underrun_count` | RDS starvation episodes |
| 11 | `u16` | `rds_fifo_free` | Free capacity in complete groups |
| 13 | `u8` | `last_error_code` | Latched TX error |
| 14 | `u8` | `input_saturated` | `0` no, `1` yes, `255` unavailable |

Latched errors can remain after the current state returns to OK. Absent TX entries are zero-filled. Status is collected sequentially, not as an atomic snapshot across all transmitters.

### 4.5 RDS_PUSH (`0x05`)

Physical TX target. **Request: `1 + 8*G` bytes. Response: empty ACK.**

| Offset | Type | Field | Values / meaning |
|---:|---|---|---|
| 0 | `u8` | `group_count` | `G = 1..31` |
| 1 | `8*G` bytes | `groups` | `G` consecutive RDS groups |

**Each eight-byte group:**

| Group offset | Type | Field |
|---:|---|---|
| 0 | `u16` | Block A |
| 2 | `u16` | Block B |
| 4 | `u16` | Block C |
| 6 | `u16` | Block D |

Blocks are little-endian; **do not include RDS checkwords**. Each physical TX has a 64-group FIFO. A push is atomic: either every group is queued or the Controller returns `BUSY`. `OK` means accepted into the FIFO, not transmitted over RF.

### 4.6 RDS_CLEAR (`0x06`)

Physical TX target.

| Direction | Payload | Result |
|---|---|---|
| Request | Empty | Clear this TX's queued RDS groups |
| Response | Empty | ACK on success |

Does not change RF enable or TX configuration. An already-started device handoff may finish; its late acknowledgment cannot remove subsequently queued groups.

### 4.7 QN_READ_REGISTER (`0x07`)

Physical TX target. Diagnostic command.

| Direction | Offset | Type | Field |
|---|---:|---|---|
| Request | 0 | `u8` | Register address |
| Response | 0 | `u8` | Register value |

### 4.8 QN_WRITE_REGISTER (`0x08`)

Physical TX target. Diagnostic command. **Response: empty ACK.**

| Offset | Type | Field |
|---:|---|---|
| 0 | `u8` | Register address |
| 1 | `u8` | Register value |

Raw register access can change RF state or invalidate TX configuration. It should not be used alongside normal Controller operation.

### 4.9 GET_RDS_FIFO_FREE (`0x0B`)

Global target. Empty request payload. **Response: 16 bytes.**

| Offset | Type | Field | Meaning |
|---:|---|---|---|
| `2*i` | `u16` | `free[i]` | Free RDS group slots for TX `i`, `i=0..7` |

Absent TX entries are zero. Values count **groups**, not bytes.

### 4.10 GET_SPI_DIAGNOSTICS (`0x0C`)

Global target (`0xFF`), empty request payload. The response contains eight
little-endian `u32` counters: **32 payload bytes, 36 bytes including header and CRC**.
Offsets below are relative to the payload.

| Offset | Counter | Counts |
|---:|---|---|
| 0 | `rp_spi_audio_link_error_count` | Existing aggregate, also returned by GET_STATUS |
| 4 | LENGTH | Rejected transaction length or bit count |
| 8 | RECEIVE_ERROR | Receive/DMA, capture initialization or recovery faults |
| 12 | GAP | Forward counter discontinuity; one error per rejected frame, not per missing counter |
| 16 | DUPLICATE | Repeated frame counter |
| 20 | BACKWARD | Backward frame counter |
| 24 | QUEUE_FULL | Frame rejected because no queue slot is free |
| 28 | LOST | Expired queued audio or queue exhaustion during playback |

Each actual aggregate error increment has one corresponding category. A receive
fault takes precedence over a length fault. Companion UART events (including LOST
after another error flushes playback) do not add errors. Never-connected silence
does not count as LOST. Counters work with UART disabled, saturate independently
at `0xFFFFFFFF`, and persist until firmware reinitialization/reset; reads never clear them.

Values are sampled atomically per counter from the audio core, not as one locked
snapshot: an error during sampling can temporarily make the aggregate differ from
the category sum. Poll for diagnosis as needed, using the normal **2 ms** request
STOP-to-read wait and BUSY handling. The command uses foreground execution without
QN register reads; premature reads follow the ordinary cancellation rules below.
During firmware-update maintenance it returns BUSY.

## 5. Status codes, timing and failures

All error responses have an empty payload.

| Code | Status | Meaning |
|---:|---|---|
| 0 | OK | Success |
| 1 | INVALID_COMMAND | Unknown or unsupported command |
| 2 | INVALID_TARGET | Wrong target type or nonexistent TX |
| 3 | INVALID_ARGUMENT | Bad frame, CRC, length, payload or value |
| 4 | BUSY | Not executed yet; special OTA behavior below |
| 5 | QN_ERROR | TX device access/control failure |
| 6 | INTERNAL_ERROR | Internal failure or uncertain execution |

**Host waits after request STOP, before reading the response:**

| Commands | Wait |
|---|---:|
| GET_INFO, RDS_PUSH, RDS_CLEAR, GET_RDS_FIFO_FREE, GET_SPI_DIAGNOSTICS | 2 ms |
| QN_READ_REGISTER, QN_WRITE_REGISTER | 100 ms |
| SET_TX_ENABLED | 150 ms |
| GET_STATUS | 850 ms |
| SET_TX_CONFIG | 1000 ms |

Malformed requests are rejected before dispatch. If the response is read before an ordinary command starts executing, the Controller returns `BUSY` and cancels that attempt. If it is read during execution, it returns `INTERNAL_ERROR`; the hardware operation may still have taken effect. A new request also cancels **unstarted** work, not work already in progress.

For ordinary commands, the reference host retries a CRC-valid `BUSY` **once**, resending the request after 10 ms and waiting again. Other uncertain mutations must not be replayed blindly. `FLASH_UPDATE` handles `BUSY` differently.

## 6. RP2354 firmware update extension

`FLASH_UPDATE` (`0xF0`, global target) uses the same I²C framing. Its first payload byte is the subcommand. The extension uses OTA payload format **2**.

| Subcommand | Byte | Request payload | OK response |
|---|---:|---:|---:|
| BEGIN | `1` | 37 bytes | 112-byte status |
| WRITE | `2` | 6–255 bytes | 9-byte progress |
| STATUS | `3` | 1 byte | 112-byte status |
| VERIFY | `4` | 1 byte | 112-byte status |
| ACTIVATE | `5` | 1 byte | 112-byte status |
| CONFIRM | `6` | 1 byte | 112-byte status |

**BEGIN payload:**

| Offset | Type | Field |
|---:|---|---|
| 0 | `u8` | Subcommand = `1` |
| 1 | `u32` | Image size (`512..0xF0000` bytes) |
| 5 | `u8[32]` | Expected SHA-256 |

**WRITE payload:**

| Offset | Type | Field |
|---:|---|---|
| 0 | `u8` | Subcommand = `2` |
| 1 | `u32` | Image offset, relative to inactive slot |
| 5 | `u8[1..250]` | Data |

WRITE accepts contiguous data. A fully duplicated accepted range is acknowledged only when its bytes match; uploads otherwise begin at offset zero. There is no session, resume or ABORT command.

**WRITE OK response (9 bytes):**

| Offset | Type | Field |
|---:|---|---|
| 0 | `u8` | Format = `2` |
| 1 | `u32` | Accepted bytes (including RAM-buffered data) |
| 5 | `u32` | Committed bytes (programmed and read back) |

**Status response (112 bytes):**

| Offset | Type | Field | Values / meaning |
|---:|---|---|---|
| 0 | `u8` | `format` | `2` |
| 1 | `u8` | `state` | `0` IDLE, `1` RECEIVING, `2` VERIFIED, `3` ACTIVATING, `4` CONFIRMED, `5` INTERRUPTED, `6` FAILED |
| 2 | `u8` | `boot_slot` | `0` A, `1` B, `255` unavailable |
| 3 | `u8` | `target_slot` | `0` A, `1` B, `255` unavailable |
| 4 | `u8` | `tbyb_pending` | `0`, `1`, or `255` unavailable |
| 5 | `u8` | `update_error` | See below |
| 6 | `u8` | `maintenance_requested` | `0` or `1` |
| 7 | `u8` | `ready` | `0` or `1` |
| 8 | `u32` | `accepted_bytes` | Current upload |
| 12 | `u32` | `committed_bytes` | Current upload |
| 16 | `u32` | `image_size` | Bytes |
| 20 | `u8[32]` | `expected_sha256` | Hash |
| 52 | `u8[16]` | `image_build_id` | Available after VERIFY |
| 68 | `u8[16]` | `running_build_id` | Running image |
| 84 | `u16` | `rom_major` | ROM image version |
| 86 | `u16` | `rom_minor` | ROM image version |
| 88 | `char[16]` | `firmware_version` | Running application version |
| 104 | `u32` | `slot_capacity` | `0xF0000` |
| 108 | `u16` | `max_write_data` | `250` |
| 110 | `u8` | `inactive_slot` | `0` A, `1` B, `255` unavailable |
| 111 | `u8` | Reserved | `0` |

| Update error | Meaning |
|---:|---|
| 0 | None |
| 1 | Layout |
| 2 | Argument |
| 3 | Unused |
| 4 | Offset |
| 5 | Flash |
| 6 | Hash |
| 7 | Image |
| 8 | State |
| 9 | Quiescence |
| 10 | ROM |

A failed update operation returns an empty outer error response; `STATUS` reports the retained update error. Flash, ROM and quiescence failures use `INTERNAL_ERROR`; other update-state failures use `INVALID_ARGUMENT`.

**OTA completion:** A CRC-valid `BUSY` means the result is pending. **Read again without resending the request.** The reference host waits 2 ms before the first read, then polls with a 10 ms backoff (and 2 ms pre-read wait), up to a three-second deadline. A timeout or invalid CRC requires `STATUS` reconciliation.

BEGIN enters maintenance, disables RF and stops audio before flash writes. ACTIVATE and CONFIRM reboot only after their full successful response has been read and the final STOP received. For provisioning, activation, rollback and recovery, see [RP2354 firmware updates](rp2354-firmware-update.md).

## 7. SPI audio transport

SPI audio is separate from the I²C command protocol. Each SPI mode-0 transaction carries one packet:

| Offset | Type | Field |
|---:|---|---|
| 0 | `u32` | Frame counter, little-endian |
| 4 | `512*N` bytes | TX0 … TX(N−1) PCM blocks, channel-major |

Each TX block contains **128 stereo frames**, signed 16-bit little-endian PCM at **48 kHz**. For `N = 1..8`, packet size is `4 + 512*N` bytes; rate is **375 packets/s**. There is no additional CRC or acknowledgment. The receiver validates chip-select boundaries, packet length and frame-counter continuity; missing audio is replaced with silence.

The release host uses 128 frames per packet. Different firmware packet sizes require a correspondingly configured sender; the size is not negotiated over I²C.
