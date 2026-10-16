"""Manual M8.2 Pi smoke: plain I2C, real firmware; never run against other versions."""
import argparse
from array import array
import fcntl
import os
from pathlib import Path
import sys
import time

# Reuse the authoritative wire codec without loading the Linux daemon/services.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench_support import protocol as p


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default="/dev/i2c-1")
    parser.add_argument("--address", required=True, type=lambda s: int(s, 0))
    args = parser.parse_args()
    if not 0x08 <= args.address <= 0x77:
        parser.error("address must be 0x08..0x77")

    fd = os.open(args.device, os.O_RDWR | os.O_CLOEXEC)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        capabilities = array("L", [0])
        fcntl.ioctl(fd, 0x0705, capabilities, True)  # I2C_FUNCS
        if not capabilities[0] & 1:
            raise RuntimeError("Adapter lacks plain I2C support")
        fcntl.ioctl(fd, 0x0703, args.address)  # I2C_SLAVE
        fcntl.ioctl(fd, 0x0702, 20)           # I2C_TIMEOUT: 200 ms
        fcntl.ioctl(fd, 0x0701, 0)            # I2C_RETRIES: no kernel replay

        def exchange(packet):
            for attempt in range(2):
                if os.write(fd, packet) != len(packet):
                    raise RuntimeError("Short request write")
                time.sleep(0.002)  # STOP already sent by os.write.
                header = os.read(fd, 2)  # First independent read, then STOP.
                if len(header) != 2:
                    raise RuntimeError("Short response header")
                payload = os.read(fd, header[1] + 2)
                try:
                    p.response(p.Command(packet[0]), header + payload)
                except p.Busy:
                    if attempt == 0:
                        time.sleep(0.01)
                        continue
                except p.RpResponseError:
                    pass  # Valid CRC; caller checks the expected error below.
                return header + payload
            raise AssertionError("Unreachable")

        def call(command, target=p.GLOBAL, payload=b""):
            return p.response(command, exchange(p.request(command, target, payload)))

        info = p.parse_info(call(p.Command.GET_INFO))
        print("GET_INFO:", info)
        if info["rp_firmware_version"] != "0.8.2-stub":
            raise RuntimeError("Refusing stub ON test: expected firmware 0.8.2-stub")

        def read_status():
            status = p.parse_status(call(p.Command.GET_STATUS))
            if any(channel["tx_state"] != "OFF" for channel in status["modulators"]):
                raise RuntimeError("Unexpected TX state: M8.2 must keep every TX OFF")
            print("GET_STATUS:", status)
            return status

        before = read_status()
        config = p.TX_CONFIG.pack(98100, 60, 1, 50, 0, 0, 1, -15, 1, 1)
        call(p.Command.SET_TX_CONFIG, 3, config)
        print("SET_TX_CONFIG TX3: stub OK (not executed)")
        call(p.Command.SET_TX_ENABLED, 3, b"\x01")
        print("SET_TX_ENABLED TX3 ON: stub OK (not executed)")
        after = read_status()
        if after["modulators"] != before["modulators"]:
            raise RuntimeError("Stub commands changed reported QN state")
        call(p.Command.SET_TX_ENABLED, 3, b"\x00")

        cases = [
            (b"\x01\x00\x00", p.Status.INVALID_TARGET),
            (b"\x01\xff\x01", p.Status.INVALID_ARGUMENT),
            (b"\x03\x03\x01\x02", p.Status.INVALID_ARGUMENT),
            (b"\x0a\xff\x00", p.Status.INVALID_COMMAND),
        ]
        for packet, status in cases:
            packet += p.crc16(packet).to_bytes(2, 'little')
            response = exchange(packet)
            if response[:2] != bytes((status, 0)):
                raise RuntimeError(f"{packet.hex()}: got {response.hex()}, expected {status.name}")
            print(f"{packet.hex(' ')} -> {status.name}")
        read_status()
        print("M8.2 host exchange PASS; verify UART, LED and RF separately.")
    finally:
        os.close(fd)


if __name__ == "__main__":
    main()
