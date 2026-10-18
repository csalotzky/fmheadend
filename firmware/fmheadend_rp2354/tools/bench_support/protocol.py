"""RP2354 V1 wire contract. See the local firmware wire implementation; no device access here."""
from enum import IntEnum, StrEnum
import struct
from decimal import Decimal
from binascii import crc_hqx

from .errors import DependencyUnavailable

PROTOCOL_VERSION = 1
GLOBAL = 0xFF


class Command(IntEnum):
    GET_INFO = 1
    SET_TX_CONFIG = 2
    SET_TX_ENABLED = 3
    GET_STATUS = 4
    RDS_PUSH = 5
    RDS_CLEAR = 6
    QN_READ_REGISTER = 7
    QN_WRITE_REGISTER = 8
    RESET_ERROR_COUNTERS = 9
    RP_SOFT_RESET = 10
    GET_RDS_FIFO_FREE = 11
    FLASH_UPDATE = 0xF0


# Approved M8.3 synchronous completion windows, milliseconds after write STOP.
# Must match host_response_wait_ms() in production firmware.
RESPONSE_WAIT_MS = {command: 2 for command in Command}
RESPONSE_WAIT_MS.update({Command.SET_TX_CONFIG: 1000, Command.GET_STATUS: 850,
    Command.SET_TX_ENABLED: 150, Command.QN_READ_REGISTER: 100, Command.QN_WRITE_REGISTER: 100})


class Status(IntEnum):
    OK = 0
    INVALID_COMMAND = 1
    INVALID_TARGET = 2
    INVALID_ARGUMENT = 3
    BUSY = 4
    QN_ERROR = 5
    INTERNAL_ERROR = 6


class ProtocolError(DependencyUnavailable):
    """Invalid framing, payload or enum received from Controller."""


class IncompatibleProtocol(ProtocolError):
    pass


class MalformedResponse(ProtocolError):
    """An actual Linux response failed framing/decoding; command is not replayed."""
    def __init__(self, command: Command, message: str):
        self.command = command
        super().__init__(message)


class HardwareUnavailable(DependencyUnavailable):
    pass


class Outcome(StrEnum):
    NOT_SENT = 'NOT_SENT'
    AMBIGUOUS = 'AMBIGUOUS'
    OBSERVATION = 'OBSERVATION'
    RESPONSE = 'RESPONSE'


READ_ONLY = {Command.GET_INFO, Command.GET_STATUS, Command.GET_RDS_FIFO_FREE, Command.QN_READ_REGISTER}


def execution_outcome(error, command):
    """Certainty for the caller's command, including an implicit INFO failure."""
    if isinstance(error, ValueError):
        return Outcome.NOT_SENT
    if getattr(error, 'command', command) != command:
        return Outcome.NOT_SENT
    if isinstance(error, RpResponseError):
        return Outcome.RESPONSE
    return getattr(error, 'outcome', Outcome.OBSERVATION if command in READ_ONLY else Outcome.AMBIGUOUS)


class RpResponseError(DependencyUnavailable):
    def __init__(self, command: Command, status: Status):
        self.command, self.rp_status = command, status
        self.outcome = Outcome.RESPONSE
        super().__init__(f"Controller {command.name}: {status.name}")


class InvalidCommand(RpResponseError): pass
class InvalidTarget(RpResponseError): pass
class InvalidArgument(RpResponseError): pass
class Busy(RpResponseError): pass
class QnError(RpResponseError): pass
class InternalError(RpResponseError): pass

ERROR_TYPES = {Status.INVALID_COMMAND: InvalidCommand, Status.INVALID_TARGET: InvalidTarget,
               Status.INVALID_ARGUMENT: InvalidArgument, Status.BUSY: Busy,
               Status.QN_ERROR: QnError, Status.INTERNAL_ERROR: InternalError}
GLOBAL_COMMANDS = {Command.FLASH_UPDATE, Command.GET_INFO, Command.GET_STATUS, Command.RESET_ERROR_COUNTERS, Command.RP_SOFT_RESET, Command.GET_RDS_FIFO_FREE}
# All successful responses have exact fixed sizes in V1.
INFO = struct.Struct("<H16s16sB")
GLOBAL_STATUS = struct.Struct("<BIIB")
TX_CONFIG = struct.Struct("<IHBBBBBhBB")
CHANNEL_STATUS = struct.Struct("<BhIIHBB")
RESPONSE_SIZES = {command: 0 for command in Command}
RESPONSE_SIZES.update({Command.GET_INFO: INFO.size, Command.GET_STATUS: GLOBAL_STATUS.size + 8 * CHANNEL_STATUS.size,
                       Command.QN_READ_REGISTER: 1, Command.GET_RDS_FIFO_FREE: 16, Command.FLASH_UPDATE: 112})


def uint(value: int, bits: int, name: str) -> int:
    if type(value) is not int or not 0 <= value < 1 << bits:
        raise ValueError(f"{name} must be an unsigned {bits}-bit integer")
    return value


def boolean(value: bool, name: str) -> int:
    if type(value) is not bool:
        raise ValueError(f"{name} must be boolean")
    return int(value)


def crc16(data: bytes) -> int:
    """CRC-16/CCITT-FALSE: 1021/FFFF, no reflection, xorout zero."""
    return crc_hqx(data, 0xFFFF)


def request(command: Command, target: int, payload: bytes = b"") -> bytes:
    if not isinstance(command, Command):
        raise ValueError("command must be a V1 Command")
    uint(target, 8, "target")
    if (command in GLOBAL_COMMANDS and target != GLOBAL) or (command not in GLOBAL_COMMANDS and target > 7):
        raise ValueError(f"Invalid target for {command.name}")
    if not isinstance(payload, bytes) or len(payload) > 255:
        raise ValueError("payload must be at most 255 bytes")
    lengths = {Command.SET_TX_CONFIG: TX_CONFIG.size, Command.SET_TX_ENABLED: 1,
               Command.QN_READ_REGISTER: 1, Command.QN_WRITE_REGISTER: 2}
    if command == Command.FLASH_UPDATE:
        validate_flash_payload(payload)
    elif command == Command.RDS_PUSH:
        if not payload or not 1 <= payload[0] <= 31 or len(payload) != 1 + 8 * payload[0]:
            raise ValueError("RDS_PUSH must contain 1..31 complete four-block groups")
    elif len(payload) != lengths.get(command, 0):
        raise ValueError(f"Invalid {command.name} request length")
    if command == Command.SET_TX_ENABLED and payload[0] not in (0, 1):
        raise ValueError("SET_TX_ENABLED must be 0 or 1")
    if command == Command.SET_TX_CONFIG:
        frequency, level, stereo, emphasis, mode, rds, gain, analog, trim, clipping = TX_CONFIG.unpack(payload)
        if level > 225 or level % 15 != 0 or frequency == 0 or stereo not in (0, 1) or emphasis not in (50, 75) or mode not in (0, 1, 2) or rds not in (0, 1) or gain not in (0, 1) or trim not in (0, 1) or clipping not in (0, 1):
            raise ValueError("Invalid SET_TX_CONFIG field value")
    body = bytes((command, target, len(payload))) + payload
    return body + struct.pack('<H', crc16(body))


def response_header(command: Command, data: bytes) -> tuple[Status, int]:
    """Validate header semantics only after response() verifies the frame CRC."""
    if len(data) != 2:
        raise ProtocolError("Controller response header must be exactly two bytes")
    try:
        status = Status(data[0])
    except ValueError as exc:
        raise ProtocolError(f"Unknown Controller status: {data[0]}") from exc
    expected = RESPONSE_SIZES[command] if status == Status.OK else 0
    if command == Command.FLASH_UPDATE and status == Status.OK and data[1] == 9:
        expected = 9
    if data[1] != expected:
        raise ProtocolError(f"{command.name} response length {data[1]}, expected {expected}")
    return status, expected


def response(command: Command, data: bytes) -> bytes:
    if len(data) < 4 or len(data) != 4 + data[1]:
        raise ProtocolError(f"{command.name} truncated or oversized response")
    expected, received = crc16(data[:-2]), int.from_bytes(data[-2:], 'little')
    if received != expected:
        raise ProtocolError(f"{command.name} response CRC mismatch: received 0x{received:04X}, expected 0x{expected:04X}")
    status, length = response_header(command, data[:2])
    if status != Status.OK:
        raise ERROR_TYPES[status](command, status)
    return data[2:2 + length]


def rf_attenuation_x10(value: float | None) -> int:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Configure rf_attenuation_db explicitly")
    scaled = Decimal(str(value)) * 10
    if not scaled.is_finite() or not 0 <= scaled <= 225 or scaled % 15 != 0:
        raise ValueError("rf_attenuation_db must be 0..22.5 in steps of 1.5 dB")
    return int(scaled)


def parse_rds_fifo_free(payload: bytes) -> list[int]:
    if len(payload) != 16:
        raise ProtocolError("GET_RDS_FIFO_FREE requires exactly 16 payload bytes")
    return list(struct.unpack("<8H", payload))


def analog_gain_x10(value: float) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("analog_gain_db must be a number")
    scaled = Decimal(str(value)) * 10
    if not scaled.is_finite() or not -32768 <= scaled <= 32767 or scaled != scaled.to_integral_value():
        raise ValueError("analog_gain_db must be -3276.8..3276.7 in steps of 0.1 dB")
    return int(scaled)


def tx_config(config) -> bytes:
    frequency = uint(config.frequency_khz, 32, "frequency_khz")
    if frequency == 0:
        raise ValueError("frequency_khz must be explicitly configured and positive")
    level = rf_attenuation_x10(config.rf_attenuation_db)
    if config.preemphasis_us not in (50, 75) or type(config.preemphasis_us) is not int:
        raise ValueError("preemphasis_us must be 50 or 75")
    if config.audio_mode not in ("DIGITAL", "ANALOG", "SINE"):
        raise ValueError("audio_mode must be DIGITAL, ANALOG or SINE")
    if config.gain_mode not in ("AUTO", "MANUAL"):
        raise ValueError("gain_mode must be AUTO or MANUAL")
    trim = uint(config.digital_trim_db, 1, "digital_trim_db")
    return TX_CONFIG.pack(frequency, level, boolean(config.stereo, "stereo"),
        config.preemphasis_us, {"DIGITAL": 0, "ANALOG": 1, "SINE": 2}[config.audio_mode], boolean(config.rds_enabled, "rds_enabled"),
        int(config.gain_mode == "MANUAL"), analog_gain_x10(config.analog_gain_db), trim,
        boolean(config.soft_clipping, "soft_clipping"))


def rds_groups(groups) -> bytes:
    # Transport already formed 16-bit blocks; no RDS encoding or FIFO policy.
    if not isinstance(groups, (list, tuple)) or not 1 <= len(groups) <= 31:
        raise ValueError("Supply 1..31 RDS groups")
    payload = bytearray((len(groups),))
    for group in groups:
        if not isinstance(group, (list, tuple)) or len(group) != 4:
            raise ValueError("Each RDS group must contain four uint16 blocks")
        payload.extend(struct.pack("<4H", *(uint(block, 16, "RDS block") for block in group)))
    return bytes(payload)


def _text(data: bytes, field: str) -> str:
    raw, separator, padding = data.partition(b"\0")
    if not separator or any(padding) or not raw:
        raise ProtocolError(f"{field}: version text must be nonempty, NUL-terminated and zero padded")
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ProtocolError(f"{field}: version text is not ASCII") from exc
    if not text.isprintable():
        raise ProtocolError(f"{field}: version text contains control characters")
    return text


def parse_info(payload: bytes) -> dict:
    if len(payload) != INFO.size:
        raise ProtocolError("Invalid GET_INFO payload size")
    version, firmware, hardware, count = INFO.unpack(payload)
    info = {"rp_protocol_version": version, "rp_firmware_version": _text(firmware, "rp_firmware_version"),
            "hardware_target": _text(hardware, "hardware_target"), "modulator_count": count}
    if version != PROTOCOL_VERSION or count != 8:
        raise IncompatibleProtocol(f"Unsupported Controller protocol/count: {version}/{count}; expected {PROTOCOL_VERSION}/8")
    return info


def _enum(value: int, values: tuple, name: str):
    if value >= len(values):
        raise ProtocolError(f"Invalid {name}: {value}")
    return values[value]


def _error(value: int, field: str):
    try:
        code = Status(value)
    except ValueError as exc:
        raise ProtocolError(f"Invalid hardware error code: {value} ({field})") from exc
    return None if code == Status.OK else code.name


def parse_status(payload: bytes) -> dict:
    if len(payload) != RESPONSE_SIZES[Command.GET_STATUS]:
        raise ProtocolError("Invalid GET_STATUS payload size")
    # Current state and latched error are independent. SPI count excludes silence.
    state, uptime, spi_errors, error = GLOBAL_STATUS.unpack_from(payload)
    result = {"state": _enum(state, ("OK", "ERROR"), "Controller state"), "uptime_ms": uptime,
              "rp_spi_audio_link_error_count": spi_errors, "last_error_code": _error(error, "global.last_error_code"), "modulators": []}
    for channel in range(8):
        # audio counts detected QN I2S underrun flags, not missing Linux PCM.
        tx, level, audio, rds, free, error, saturated = CHANNEL_STATUS.unpack_from(payload, GLOBAL_STATUS.size + channel * CHANNEL_STATUS.size)
        if level != -32768 and (level < 0 or level > 225 or level % 15 != 0):
            raise ProtocolError(f"Invalid RF attenuation: {level} (TX{channel}.rf_attenuation_db_x10)")
        if saturated not in (0, 1, 255):
            raise ProtocolError(f"Invalid input_saturated: {saturated} (TX{channel}.input_saturated)")
        result["modulators"].append({"id": channel, "tx_state": _enum(tx, ("OFF", "ON", "ERROR"), f"TX{channel} state"),
            "rf_attenuation_db": None if level == -32768 else level / 10,
            "audio_underrun_count": audio, "rds_underrun_count": rds,
            "input_saturated": None if saturated == 255 else bool(saturated),
            "rds_fifo_free": free, "last_error_code": _error(error, f"TX{channel}.last_error_code")})
    return result


class FlashCommand(IntEnum):
    BEGIN = 1
    WRITE = 2
    STATUS = 3
    VERIFY = 4
    ACTIVATE = 5
    CONFIRM = 6


# First read after foreground dispatch. Further reads poll the immutable BUSY
# frame every 10 ms, with a bounded deadline; flash completion publishes the ACK.
FLASH_WAIT_MS = {command: 2 for command in FlashCommand}
RESPONSE_WAIT_MS[Command.FLASH_UPDATE] = 2
FLASH_CHUNK = 250


def validate_flash_payload(payload):
    if not payload:
        raise ValueError('Missing FLASH_UPDATE subcommand')
    sub = FlashCommand(payload[0])
    if sub == FlashCommand.BEGIN:
        if len(payload) != 37:
            raise ValueError('BEGIN requires size and SHA256 (OTA format 2)')
        if not 512 <= struct.unpack_from('<I', payload, 1)[0] <= 0xf0000:
            raise ValueError('Invalid image size')
    elif sub == FlashCommand.WRITE:
        if not 6 <= len(payload) <= 255:
            raise ValueError('WRITE requires offset and 1..250 bytes')
    elif len(payload) != 1:
        raise ValueError('Invalid FLASH_UPDATE payload length')


def parse_flash_write(data):
    if len(data) != 9 or data[0] != 2:
        raise ProtocolError('Invalid OTA format 2 WRITE acknowledgement')
    accepted, committed = struct.unpack_from('<II', data, 1)
    if committed > accepted or accepted > 0xf0000:
        raise ProtocolError('Invalid WRITE offsets')
    return dict(accepted=accepted, committed=committed)


def parse_flash_status(data):
    if (len(data) != 112 or data[0] != 2 or data[1] > 6 or data[4] not in (0, 1, 255) or
            any(data[i] not in (0, 1, 255) for i in (2, 3, 110)) or
            data[5] > 10 or data[6] > 1 or data[7] > 1 or data[111]):
        raise IncompatibleProtocol('Expected session-free OTA format 2 status')
    accepted, committed, size = struct.unpack_from('<III', data, 8)
    if committed > accepted or accepted > size or size > 0xf0000:
        raise ProtocolError('Invalid FLASH_UPDATE offsets')
    return dict(state=data[1], boot=data[2], target=data[3], tbyb=data[4], error=data[5],
                maintenance=bool(data[6]), ready=bool(data[7]), accepted=accepted,
                committed=committed, size=size, sha256=data[20:52],
                build=data[52:68], running_build=data[68:84],
                version=struct.unpack_from('<HH', data, 84),
                firmware_version=_text(data[88:104], 'firmware_version'),
                capacity=struct.unpack_from('<I',data,104)[0],
                chunk=struct.unpack_from('<H',data,108)[0], inactive=data[110])
