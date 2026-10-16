"""Real Linux Controller client. OS calls are patched directly in unit tests."""
from __future__ import annotations
from typing import TYPE_CHECKING
from array import array
import logging
import os
import sys
import threading
import time

try:
    import fcntl
except ImportError:  # Keep mock mode importable on non-Linux hosts.
    fcntl = None

from . import protocol as p
if TYPE_CHECKING:
    from .client import TxConfig

I2C_RETRIES = 0x0701
I2C_TIMEOUT = 0x0702
I2C_SLAVE = 0x0703
I2C_FUNCS = 0x0705
I2C_FUNC_I2C = 1
logger = logging.getLogger('fmheadendd.controller.linux_client')


class Rp2354Client:
    """One outstanding request; no replay after ambiguous write/read failures.

    BUSY means the command was not executed and permits one bounded retry.
    Failed sessions are closed and must handshake again before another command.
    """
    mocked = False

    def __init__(self, device: str, address: int | None, *, timeout_ms: int = 200):
        if not isinstance(device, str) or not device:
            raise ValueError("I2C device path is required")
        if address is not None and (type(address) is not int or not 8 <= address <= 119):
            raise ValueError("Controller slave address must be an unreserved 7-bit address (0x08..0x77)")
        if type(timeout_ms) is not int or not 10 <= timeout_ms <= 1000:
            raise ValueError("I2C timeout must be 10..1000 ms")
        self.device, self.address, self.timeout_ms = device, address, timeout_ms
        self._fd = None
        self._retain_bus = False
        self._lock = threading.RLock()
        self._retry_at = 0.0
        self.info: dict | None = None
        self.i2c_errors = 0
        self.last_error: str | None = None
        self._malformed_reported = False

    def _open(self):
        if self._fd is not None:
            return
        if self.address is None:
            raise p.HardwareUnavailable("Configure controller_i2c_address or --rp-i2c-address; no default slave address is assumed")
        if fcntl is None or not sys.platform.startswith("linux"):
            raise p.HardwareUnavailable("Controller I2C requires Linux i2c-dev")
        fd = os.open(self.device, os.O_RDWR | os.O_CLOEXEC)
        try:
            # Cooperating clients must not interleave response reads on this bus.
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            capabilities = array("L", [0])
            fcntl.ioctl(fd, I2C_FUNCS, capabilities, True)
            if not capabilities[0] & I2C_FUNC_I2C:
                raise p.HardwareUnavailable("I2C adapter lacks plain I2C support")
            fcntl.ioctl(fd, I2C_SLAVE, self.address)
            fcntl.ioctl(fd, I2C_TIMEOUT, (self.timeout_ms + 9) // 10)
            # Avoid kernel replay of side-effecting commands after arbitration loss.
            fcntl.ioctl(fd, I2C_RETRIES, 0)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd

    def _close(self):
        self.info = None
        if self._retain_bus:
            return  # Standalone uploader owns the advisory lock across reconciliation.
        fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)

    def reserve_bus(self):
        """CLI-only lifetime ownership. Never stop or compete with the daemon."""
        with self._lock:
            try:
                self._open()
            except BlockingIOError as exc:
                raise p.HardwareUnavailable('I2C device is locked; stop the daemon manually before updating') from exc
            self._retain_bus = True

    def close(self):
        with self._lock:
            self._retain_bus = False
            self._close()

    @property
    def retry_delay(self):
        return max(0.0, self._retry_at - time.monotonic())

    def _exchange(self, command: p.Command, target: int, payload: bytes = b"", *, decode=None):
        packet = p.request(command, target, payload)
        if time.monotonic() < self._retry_at:
            error = p.HardwareUnavailable(self.last_error or "Controller reconnect backoff")
            error.command, error.target, error.stage, error.outcome = command, target, 'backoff', p.Outcome.NOT_SENT
            raise error
        header = received = None
        stage = 'open'
        try:
            self._open()
            ota = command == p.Command.FLASH_UPDATE
            deadline = time.monotonic() + 3
            for attempt in range(301 if ota else 2):
                header = received = None
                stage = 'write'
                if (not ota or attempt == 0) and os.write(self._fd, packet) != len(packet):
                    raise p.ProtocolError("Short I2C request write; command outcome is unknown")
                # Firmware must prepare a response, or BUSY without side effects,
                # within the command window. Each transaction ends with STOP.
                stage = 'wait'
                time.sleep((p.FLASH_WAIT_MS[p.FlashCommand(payload[0])] if command == p.Command.FLASH_UPDATE
                            else p.RESPONSE_WAIT_MS[command]) / 1000)
                stage = 'header'
                header = os.read(self._fd, 2)
                if len(header) != 2:
                    raise p.ProtocolError("Controller response header must be exactly two bytes")
                # Length is untrusted but u8-bounded. Interpret neither status
                # nor payload until the complete frame (including CRC) validates.
                length = header[1]
                stage = 'payload'
                received = os.read(self._fd, length + 2)
                try:
                    result = p.response(command, header + received)
                    if decode is not None:
                        stage = 'decode'
                        result = decode(result)
                    if command == p.Command.GET_STATUS:
                        self._malformed_reported = False
                    self.last_error = None
                    return result
                except p.Busy:
                    if ota:
                        if time.monotonic() >= deadline:
                            raise p.ProtocolError('OTA completion deadline exceeded; reconcile STATUS')
                        time.sleep(0.01)
                        continue
                    if self._retain_bus:
                        logger.warning("Controller BUSY command=%s attempt=%s; %s", command.name,
                                       attempt + 1, "not retrying again" if attempt else "retrying once")
                    if attempt:
                        raise
                    time.sleep(0.01)
        except p.RpResponseError as exc:
            exc.target, exc.stage = target, 'response'
            self.last_error = str(exc)
            raise
        except (OSError, p.ProtocolError, p.HardwareUnavailable) as exc:
            if isinstance(exc, p.ProtocolError) and (not self._malformed_reported or self._retain_bus):
                # Actual read bytes, including the CRC trailer in payload.
                logger.warning("Controller malformed response command=%s header=%s payload=%s payload_length=%s error=%s",
                    command.name, header.hex() if header is not None else "unavailable",
                    received.hex() if received is not None else "unavailable",
                    len(received) if received is not None else "unavailable", exc)
                self._malformed_reported = True
            self.i2c_errors += 1
            self.last_error = f"Controller I2C {self.device} address {self.address}: {exc}"
            self._close()
            self._retry_at = time.monotonic() + (0.02 if command == p.Command.FLASH_UPDATE and self._retain_bus else 0.25)
            error = (exc if isinstance(exc, p.IncompatibleProtocol) else
                     p.MalformedResponse(command, str(exc)) if isinstance(exc, p.ProtocolError) else
                     p.HardwareUnavailable(self.last_error))
            error.command, error.target, error.stage = command, target, stage
            error.outcome = (p.Outcome.NOT_SENT if stage == 'open' else
                             p.Outcome.OBSERVATION if command in p.READ_ONLY else p.Outcome.AMBIGUOUS)
            if error is exc:
                raise
            raise error from exc
        raise AssertionError("Unreachable exchange state")

    def get_info(self) -> dict:
        with self._lock:
            self.info = self._exchange(p.Command.GET_INFO, p.GLOBAL, decode=p.parse_info)
            return dict(self.info)

    def _call(self, command: p.Command, target: int, payload: bytes = b"", *, decode=None):
        # Validate before opening a bus or making a startup handshake.
        p.request(command, target, payload)
        with self._lock:
            if self.info is None:
                self.get_info()
            return self._exchange(command, target, payload, decode=decode)

    def set_tx_config(self, modulator_id: int, config: TxConfig) -> None:
        self._call(p.Command.SET_TX_CONFIG, modulator_id, p.tx_config(config))

    def set_tx_enabled(self, modulator_id: int, enabled: bool) -> None:
        self._call(p.Command.SET_TX_ENABLED, modulator_id, bytes((p.boolean(enabled, "enabled"),)))

    def get_status(self) -> dict:
        with self._lock:
            result = self._call(p.Command.GET_STATUS, p.GLOBAL, decode=p.parse_status)
            result["i2c_errors"] = self.i2c_errors
            return result

    def get_rds_fifo_free(self) -> list[int]:
        return self._call(p.Command.GET_RDS_FIFO_FREE, p.GLOBAL, decode=p.parse_rds_fifo_free)

    def push_rds(self, modulator_id: int, groups: list[tuple[int, int, int, int]]) -> None:
        self._call(p.Command.RDS_PUSH, modulator_id, p.rds_groups(groups))

    def clear_rds(self, modulator_id: int) -> None:
        self._call(p.Command.RDS_CLEAR, modulator_id)

    def qn_read_register(self, modulator_id: int, register: int) -> int:
        return self._call(p.Command.QN_READ_REGISTER, modulator_id, bytes((p.uint(register, 8, "register"),)))[0]

    def qn_write_register(self, modulator_id: int, register: int, value: int) -> None:
        self._call(p.Command.QN_WRITE_REGISTER, modulator_id,
                   bytes((p.uint(register, 8, "register"), p.uint(value, 8, "value"))))

    def reset_error_counters(self) -> None:
        self._call(p.Command.RESET_ERROR_COUNTERS, p.GLOBAL)
        # Linux transport error count remains cumulative; Controller counters are separate.

    def reset(self) -> None:
        with self._lock:
            self._call(p.Command.RP_SOFT_RESET, p.GLOBAL)
            self._close()
            self._retry_at = time.monotonic() + 0.25

    def flash_update(self, payload: bytes) -> dict:
        return self._call(p.Command.FLASH_UPDATE, p.GLOBAL, payload, decode=p.parse_flash_write if payload[0] == p.FlashCommand.WRITE else p.parse_flash_status)
