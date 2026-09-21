"""Explicitly enable the one-shot firmware-side TX0 RDS bench probe; RF ON for 60s."""
import argparse
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from daemon.rp2354.client import TxConfig
from daemon.rp2354.linux_client import Rp2354Client


def run(rp, *, clock=time.monotonic, sleep=time.sleep):
    touched = False
    try:
        info = rp.get_info()
        print("GET_INFO:", info, flush=True)
        if info["rp_firmware_version"] != "0.8.6a-probe":
            raise RuntimeError("Flash the explicitly enabled local probe build; reset RP before each run")
        status = rp.get_status()
        if status["state"] != "OK" or len(status["modulators"]) != 8 or any(
                ch["tx_state"] != "OFF" for ch in status["modulators"]):
            raise RuntimeError("Start with all eight TXs OFF and no active hardware fault")
        touched = True
        rp.set_tx_enabled(0, False)
        rp.set_tx_config(0, TxConfig(frequency_khz=87500, rf_attenuation_db=22.5,
            stereo=True, preemphasis_us=50, audio_mode="DIGITAL", rds_enabled=True,
            gain_mode="AUTO", analog_gain_db=0.0, digital_trim_db=0, soft_clipping=False))
        # Diagnostic-only settling after the successful configuration response.
        # The firmware independently delays its first group after RF ON.
        print("Diagnostic settling: 300 ms after SET_TX_CONFIG, before RF ON.", flush=True)
        sleep(0.3)
        rp.set_tx_enabled(0, True)
        started = clock()
        print("TX0 RF ON: 87.5 MHz, attenuation 22.5 dB, PI=FFFF, PS=' R D S  '. "
              "Firmware supplies high-byte-first groups locally. Capture UART.", flush=True)
        # Only status traffic; all group transfer/pacing happens on the RP.
        while clock() - started < 65:
            sleep(2)
            status = rp.get_status()
            elapsed = clock() - started
            tx0 = status["modulators"][0]
            print(f"t={elapsed:.1f}s TX0={tx0['tx_state']} state={status['state']} "
                  f"SPI_errors={status['rp_spi_audio_link_error_count']}", flush=True)
            if status["state"] != "OK" or any(
                    ch["tx_state"] != "OFF" for ch in status["modulators"][1:]):
                raise RuntimeError("Hardware fault or another TX active; inspect UART")
            if tx0["tx_state"] == "OFF":
                if elapsed < 59:
                    raise RuntimeError("Probe stopped early; inspect UART")
                print("Timed RF OFF observed; verify receiver PI/PS and UART accepted-group rate.")
                return
            if tx0["tx_state"] != "ON":
                raise RuntimeError("Unexpected TX0 state")
        raise RuntimeError("Firmware did not finish the probe; inspect UART and reset before retry")
    finally:
        try:
            if touched:
                try:
                    rp.set_tx_enabled(0, False)
                    if rp.get_status()["modulators"][0]["tx_state"] != "OFF":
                        raise RuntimeError("RF OFF readback failed")
                    print("Final TX0 RF OFF verified.", flush=True)
                except Exception:
                    print("RF OFF NOT VERIFIED; stop bench RF manually.", file=sys.stderr)
                    raise
        finally:
            rp.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default="/dev/i2c-1")
    parser.add_argument("--address", required=True, type=lambda s: int(s, 0))
    args = parser.parse_args()
    try:
        run(Rp2354Client(args.device, args.address))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"PROBE STOPPED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
