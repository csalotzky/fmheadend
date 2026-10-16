"""Probe sequencing and safety with real Linux 0A encoder, fake hardware/time."""
import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))


class Config:
    def __init__(self, **values):
        self.__dict__.update(values)


for name, values in {
    "bench_support.client": {"TxConfig": Config},
    "bench_support.linux_client": {"Rp2354Client": None},
}.items():
    fake = types.ModuleType(name)
    fake.__dict__.update(values)
    sys.modules[name] = fake
spec = importlib.util.spec_from_file_location("probe", ROOT / "tools/qn_rds_probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class Hardware:
    def __init__(self, *, on_data=False, on_toggle=True, system2=0x03):
        self.now = 0.0
        self.regs = {0: 5, 1: system2, 7: 0x71, 15: 0x24, 27: 0}
        self.calls = []
        self.on_data, self.on_toggle = on_data, on_toggle
        self.configured = False
        self.failure = None
        self.active = False
        self.bad_readback = False

    def clock(self):
        return self.now

    def sleep(self, seconds):
        assert seconds >= 0
        self.now += seconds

    def get_info(self):
        return {"rp_firmware_version": "0.8.6a"}

    def get_status(self):
        return {"state": "OK", "modulators": [
            {"tx_state": "ON" if self.active and i == 0 else "OFF"} for i in range(8)]}

    def set_tx_config(self, tx, config):
        assert tx == 0 and not self.regs[0] & 0x40
        assert config.__dict__ == dict(frequency_khz=87500, rf_attenuation_db=22.5,
            stereo=True, preemphasis_us=50, audio_mode="DIGITAL", rds_enabled=True,
            gain_mode="AUTO", analog_gain_db=0.0, digital_trim_db=0, soft_clipping=False)
        self.calls.append(("config",))
        self.regs[0] = 7
        self.configured = True

    def set_tx_enabled(self, tx, value):
        assert tx == 0
        if value:
            assert self.configured  # Existing firmware invalidates this on raw writes.
        self.calls.append(("enable", value))
        self.regs[0] = self.regs[0] & ~0x40 | (0x40 if value else 0)

    def qn_read_register(self, tx, reg):
        assert tx == 0
        self.calls.append(("read", reg))
        self.now += 0.1
        value = self.regs.get(reg, 0)
        if self.bad_readback and reg == 1 and any(c[:2] == ("write", 1) for c in self.calls):
            value ^= 4
        return value

    def qn_write_register(self, tx, reg, value):
        assert tx == 0 and reg in (1, *range(16, 24))
        self.calls.append(("write", reg, value))
        self.now += 0.1
        if self.failure == reg:
            raise RuntimeError("injected failure")
        if (reg == 23 and self.on_data) or (reg == 1 and self.on_toggle):
            self.regs[27] ^= 0x80
        self.regs[reg] = value
        self.configured = False

    def close(self):
        self.calls.append(("close",))


def args(order="low-first", mode="handshake"):
    return types.SimpleNamespace(byte_order=order, mode=mode, poll_seconds=0.2,
                                 poll_interval_ms=25, seconds=9)


def run(hw, options=None):
    with contextlib.redirect_stdout(io.StringIO()) as output:
        probe.run(hw, options or args(), clock=hw.clock, sleep=hw.sleep)
    return output.getvalue()


class Tests(unittest.TestCase):
    def test_linux_encoder_and_two_explicit_byte_orders(self):
        expected = [(0x1234, 8, 0xE0CD, 0x464D), (0x1234, 9, 0xE0CD, 0x5445),
                    (0x1234, 10, 0xE0CD, 0x5354), (0x1234, 15, 0xE0CD, 0x2020)]
        self.assertEqual(probe.groups(), expected)
        self.assertEqual(probe.group_bytes(expected[0], "low-first").hex(), "34120800cde04d46")
        self.assertEqual(probe.group_bytes(expected[0], "high-first").hex(), "12340008e0cd464d")
        with self.assertRaises(ValueError):
            probe.group_bytes(expected[0], "guess")

    def test_exact_call_order_and_no_rf_in_handshake(self):
        for order in ("low-first", "high-first"):
            for initial, toggled in ((3, 7), (7, 3), (0x1B, 0x1F)):
                hw = Hardware(system2=initial)
                text = run(hw, args(order))
                first = hw.calls.index(("read", 1))
                sequence = [("read", 1), ("read", 27)]
                sequence += [("write", 16+i, byte) for i, byte in enumerate(
                    probe.group_bytes(probe.groups()[0], order))]
                sequence += [("read", 27), ("read", 1), ("write", 1, toggled),
                             ("read", 1), ("read", 27)]
                self.assertEqual(hw.calls[first:first+len(sequence)], sequence)
                self.assertNotIn(("enable", True), hw.calls)
                self.assertEqual(hw.calls[-3:], [("enable", False), ("read", 0), ("close",)])
                self.assertIn("No acceptance verdict", text)
                self.assertIn("poll=0x80", text)
                self.assertLess(hw.now, 5)

    def test_distinguishes_data_toggle_both_and_no_transition(self):
        for data in (False, True):
            for toggle in (False, True):
                hw = Hardware(on_data=data, on_toggle=toggle)
                text = run(hw)
                self.assertIn(f"baseline->after-RDSD7={data}", text)
                self.assertEqual("transition times=[]" in text, not toggle)

    def test_air_enables_before_raw_writes_repeats_four_segments_then_off(self):
        hw = Hardware()
        text = run(hw, args("high-first", "over-air"))
        writes = [c for c in hw.calls if c[0] == "write"]
        self.assertLess(hw.calls.index(("enable", True)), hw.calls.index(writes[0]))
        self.assertEqual(hw.calls.count(("enable", True)), 1)
        for n, group in enumerate(probe.groups()):
            expected = [("write", 16+i, byte) for i, byte in enumerate(probe.group_bytes(group, "high-first"))]
            self.assertEqual(writes[n*9:n*9+8], expected)
        self.assertIn("RF ON: TX0 only", text)
        self.assertFalse(hw.regs[0] & 0x40)

    def test_no_change_stops_air_repetition(self):
        hw = Hardware(on_toggle=False)
        text = run(hw, args(mode="over-air"))
        self.assertEqual(len([c for c in hw.calls if c[0] == "write"]), 9)
        self.assertIn("STOP repetition", text)
        self.assertFalse(hw.regs[0] & 0x40)

    def test_bus_failure_no_retry_and_cleanup(self):
        for reg in (16, 23, 1):
            hw = Hardware()
            hw.failure = reg
            with self.assertRaisesRegex(RuntimeError, "injected failure"):
                run(hw, args(mode="over-air"))
            self.assertEqual(sum(c[:2] == ("write", reg) for c in hw.calls), 1)
            self.assertEqual(hw.calls[-3:], [("enable", False), ("read", 0), ("close",)])

    def test_readback_mismatch_stops(self):
        hw = Hardware()
        hw.bad_readback = True
        with self.assertRaisesRegex(RuntimeError, "readback mismatch"):
            run(hw)
        self.assertEqual(hw.calls[-3:], [("enable", False), ("read", 0), ("close",)])

    def test_refuse_active_bench_without_mutation(self):
        hw = Hardware()
        hw.active = True
        with self.assertRaisesRegex(RuntimeError, "eight TXs OFF"):
            run(hw)
        self.assertEqual(hw.calls, [("close",)])

    def test_interrupt_enable_is_never_set_or_used(self):
        hw = Hardware()
        hw.regs[15] |= 2
        with self.assertRaisesRegex(RuntimeError, "interrupt enable"):
            run(hw)
        self.assertFalse(any(c[0] == "write" for c in hw.calls))
        self.assertNotIn(("enable", True), hw.calls)


if __name__ == "__main__":
    unittest.main()
