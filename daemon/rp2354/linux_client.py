"""Real Linux RP2354 client. OS calls are patched directly in unit tests."""
from array import array
import os
import sys
import threading
import time

try:
    import fcntl
except ImportError:  # Keep mock mode importable on non-Linux hosts.
    fcntl = None

from . import protocol as p
from .client import TxConfig

I2C_RETRIES = 0x0701
I2C_TIMEOUT = 0x0702
I2C_SLAVE = 0x0703
I2C_FUNCS = 0x0705
I2C_FUNC_I2C = 1


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
            raise ValueError("RP slave address must be an unreserved 7-bit address (0x08..0x77)")
        if type(timeout_ms) is not int or not 10 <= timeout_ms <= 1000:
            raise ValueError("I2C timeout must be 10..1000 ms")
        self.device, self.address, self.timeout_ms = device, address, timeout_ms
        self._fd = None
        self._lock = threading.RLock()
        self._retry_at = 0.0
        self.info: dict | None = None
        self.i2c_errors = 0
        self.last_error: str | None = None

    def _open(self):
        if self._fd is not None:
            return
        if self.address is None:
            raise p.HardwareUnavailable("Configure rp2354_i2c_address or --rp-i2c-address; no default slave address is assumed")
        if fcntl is None or not sys.platform.startswith("linux"):
            raise p.HardwareUnavailable("Real RP2354 I2C requires Linux i2c-dev")
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
        fd, self._fd = self._fd, None
        self.info = None
        if fd is not None:
            os.close(fd)

    def close(self):
        with self._lock:
            self._close()

    def _exchange(self, command: p.Command, target: int, payload: bytes = b"") -> bytes:
        packet = p.request(command, target, payload)
        if time.monotonic() < self._retry_at:
            raise p.HardwareUnavailable(self.last_error or "RP reconnect backoff")
        try:
            self._open()
            for attempt in range(2):
                if os.write(self._fd, packet) != len(packet):
                    raise p.ProtocolError("Short I2C request write; command outcome is unknown")
                # Firmware must prepare a response, or BUSY without side effects,
                # within the command window. Each transaction ends with STOP.
                time.sleep(p.RESPONSE_WAIT_MS[command] / 1000)
                header = os.read(self._fd, 2)
                _, length = p.response_header(command, header)
                payload = os.read(self._fd, length) if length else b""
                try:
                    result = p.response(command, header + payload)
                    self.last_error = None
                    return result
                except p.Busy:
                    if attempt:
                        raise
                    time.sleep(0.01)
        except p.RpResponseError as exc:
            self.last_error = str(exc)
            raise
        except (OSError, p.ProtocolError, p.HardwareUnavailable) as exc:
            self.i2c_errors += 1
            self.last_error = f"RP2354 I2C {self.device} address {self.address}: {exc}"
            self._close()
            self._retry_at = time.monotonic() + 0.25
            if isinstance(exc, p.ProtocolError):
                raise
            raise p.HardwareUnavailable(self.last_error) from exc
        raise AssertionError("Unreachable exchange state")

    def get_info(self) -> dict:
        with self._lock:
            try:
                self.info = p.parse_info(self._exchange(p.Command.GET_INFO, p.GLOBAL))
            except p.ProtocolError as exc:
                self.last_error = str(exc)
                self._close()
                self._retry_at = time.monotonic() + 0.25
                raise
            return dict(self.info)

    def _call(self, command: p.Command, target: int, payload: bytes = b"") -> bytes:
        # Validate before opening a bus or making a startup handshake.
        p.request(command, target, payload)
        with self._lock:
            if self.info is None:
                self.get_info()
            return self._exchange(command, target, payload)

    def set_tx_config(self, modulator_id: int, config: TxConfig) -> None:
        self._call(p.Command.SET_TX_CONFIG, modulator_id, p.tx_config(config))

    def set_tx_enabled(self, modulator_id: int, enabled: bool) -> None:
        self._call(p.Command.SET_TX_ENABLED, modulator_id, bytes((p.boolean(enabled, "enabled"),)))

    def get_status(self) -> dict:
        with self._lock:
            try:
                result = p.parse_status(self._call(p.Command.GET_STATUS, p.GLOBAL))
            except p.ProtocolError as exc:
                self.last_error = str(exc)
                self._close()
                self._retry_at = time.monotonic() + 0.25
                raise
            result["i2c_errors"] = self.i2c_errors
            return result

    def get_rds_fifo_free(self) -> list[int]:
        return p.parse_rds_fifo_free(self._call(p.Command.GET_RDS_FIFO_FREE, p.GLOBAL))

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
        # Linux transport error count remains cumulative; RP counters are separate.

    def reset(self) -> None:
        with self._lock:
            self._call(p.Command.RP_SOFT_RESET, p.GLOBAL)
            self._close()
            self._retry_at = time.monotonic() + 0.25
