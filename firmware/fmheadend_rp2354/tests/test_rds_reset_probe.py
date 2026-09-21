"""Candidate RDS-only reset observer: fake hardware, never evidence of a flush."""
import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import types

linux = types.ModuleType("daemon.rp2354.linux_client")
linux.Rp2354Client = None
sys.modules[linux.__name__] = linux
spec = importlib.util.spec_from_file_location("rds_reset_probe", Path(__file__).parents[1] / "tools/rds_reset_probe.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Client:
    def __init__(self, system1=0x43, status2=0, fail=False):
        self.now = 0.
        self.regs = {0: system1, 1: 0x33, 0x1b: status2}
        self.writes = []
        self.off = self.cleared = False
        self.fail = fail

    def clock(self): return self.now
    def sleep(self, duration): self.now += duration
    def get_info(self): return {"rp_firmware_version": "0.8.6b"}
    def get_status(self):
        return {"modulators": [{"tx_state": "ERROR"}] + [{"tx_state": "OFF"}] * 7}
    def qn_read_register(self, tx, reg):
        assert tx == 0
        self.now += .1  # Existing synchronous raw-command delay, not QN timing.
        return self.regs.get(reg, 0)
    def qn_write_register(self, tx, reg, value):
        assert tx == 0
        self.writes.append((reg, value))
        self.now += .1
        if self.fail:
            raise RuntimeError("injected write failure")
        self.regs[reg] = value
        if reg == 1:
            self.regs[0x1b] ^= 0x80  # Artificial ACK; attribution is deliberately unknown.
    def set_tx_enabled(self, tx, on):
        assert tx == 0 and not on  # The tool may NEVER turn RF on.
        self.off = True
        self.regs[0] &= ~0x40
    def clear_rds(self, tx):
        assert tx == 0 and self.off
        self.cleared = True


rp = Client()
with contextlib.redirect_stdout(io.StringIO()) as output:
    module.probe(rp, 0, clock=rp.clock, sleep=rp.sleep)
assert rp.writes[:2] == [(0, 0x41), (0, 0x43)]
assert rp.writes[2:10] == list(enumerate(bytes.fromhex("12340008e0cd464d"), 0x10))
assert rp.writes[10:] == [(1, 0x37)]  # Only RDSTXRDY changes.
assert rp.off and rp.cleared
assert "attribution unproven" in output.getvalue()
assert "NOT proof" in output.getvalue()

# Never run against DIGITAL/SINE: raw overrides invalidate source eligibility.
rp = Client(system1=0x47)
try:
    module.probe(rp, 0, clock=rp.clock, sleep=rp.sleep)
except RuntimeError as exc:
    assert "ANALOG" in str(exc)
else:
    raise AssertionError("DIGITAL input was not rejected")
assert not rp.writes and not rp.off

for rp, message in ((Client(status2=0x80), "Old ACK"), (Client(fail=True), "injected write failure")):
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            module.probe(rp, 0, clock=rp.clock, sleep=rp.sleep)
    except RuntimeError as exc:
        assert message in str(exc)
    else:
        raise AssertionError("Expected diagnostic rejection/failure")
    assert rp.off and rp.cleared
print("RDS reset probe: preserved RF/input bits, exact group, observation-only claims, rejection and cleanup PASS")
