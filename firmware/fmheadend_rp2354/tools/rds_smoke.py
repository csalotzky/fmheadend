"""M8.6A FIFO-only smoke using Rp2354Client; sends no RF or QN register commands."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from daemon.rp2354.linux_client import Rp2354Client


def check(rp, expected, label):
    free = rp.get_rds_fifo_free()
    print(f"{label}: free={free}", flush=True)
    if free != expected:
        raise RuntimeError(f"Expected {expected}; reset RP and stop other RDS clients")
    status = rp.get_status()
    channels = status["modulators"]
    if status["state"] != "OK":
        raise RuntimeError(f"Active hardware fault: {status}")
    if len(channels) != 8 or [c["rds_fifo_free"] for c in channels] != expected:
        raise RuntimeError("GET_STATUS and GET_RDS_FIFO_FREE disagree")
    if any(c["rds_underrun_count"] != 0 or c["tx_state"] != "OFF" for c in channels):
        raise RuntimeError("Expected all RF OFF and zero RDS underruns")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default="/dev/i2c-1")
    parser.add_argument("--address", required=True, type=lambda s: int(s, 0))
    args = parser.parse_args()
    rp = Rp2354Client(args.device, args.address)
    restore_tx0 = False
    try:
        info = rp.get_info()
        print("GET_INFO:", info, flush=True)
        if info["rp_firmware_version"] not in ("0.8.6a", "0.8.6b"):
            raise RuntimeError("This RF-OFF FIFO smoke requires M8.6A/B firmware")
        check(rp, [64] * 8, "BOOT")
        groups = [(0x1234, 0xABCD, 0x8001, 0x00FF),
                  (0xFFFF, 0x0000, 0x55AA, 0xA55A)]
        restore_tx0 = True
        rp.push_rds(0, groups)
        check(rp, [62] + [64] * 7, "PUSH TX0: 2 known groups")
        rp.clear_rds(0)
        check(rp, [64] * 8, "CLEAR TX0")
        restore_tx0 = False
        print("PASS: FIFO counts/status/isolation; no QN RDS transmission or RF command.")
    finally:
        try:
            if restore_tx0:
                rp.clear_rds(0)
        finally:
            rp.close()


if __name__ == "__main__":
    main()
