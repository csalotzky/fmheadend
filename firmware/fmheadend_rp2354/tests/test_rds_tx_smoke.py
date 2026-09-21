"""Finite bench tool test; real Linux encoder, fake client and time, no hardware."""
import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import types

class Config:
    def __init__(self, **kw):
        self.__dict__.update(kw)

client_module = types.ModuleType("daemon.rp2354.client")
client_module.TxConfig = Config
linux_module = types.ModuleType("daemon.rp2354.linux_client")
linux_module.Rp2354Client = None
sys.modules[client_module.__name__] = client_module
sys.modules[linux_module.__name__] = linux_module
spec = importlib.util.spec_from_file_location("rds_tx_smoke", Path(__file__).parents[1] / "tools/rds_tx_smoke.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class Client:
    def __init__(self, tx, fail=False):
        self.tx, self.fail = tx, fail
        self.now = self.last = 0.
        self.queue = []
        self.calls = []
        self.on = self.armed = self.starved = False
        self.underruns = self.consumed = self.sent = self.queries = 0

    def clock(self): return self.now
    def sleep(self, seconds): self.now += seconds
    def get_info(self): return {"rp_firmware_version": "0.8.6b"}
    def drain(self):
        while self.on and self.now - self.last >= 104/1187.5:
            self.last += 104/1187.5
            if self.queue:
                self.queue.pop(0)
                self.consumed += 1
                self.armed = True
                self.starved = False
            elif self.armed and not self.starved:
                self.underruns += 1
                self.starved = True
    def get_status(self):
        self.sleep(.850)  # The unchanged serialized GET_STATUS wait.
        self.drain()
        return {"state": "OK", "modulators": [
            {"tx_state": "ON" if self.on and i == self.tx else "OFF",
             "rds_fifo_free": 64-len(self.queue) if i == self.tx else 64,
             "rds_underrun_count": self.underruns if i == self.tx else 0}
            for i in range(8)]}
    def get_rds_fifo_free(self):
        self.queries += 1
        if self.fail and self.queries == 3:
            raise RuntimeError("injected transport failure")
        self.drain()
        return [64-len(self.queue) if i == self.tx else 64 for i in range(8)]
    def set_tx_config(self, tx, config):
        assert tx == self.tx and not self.on
        assert config.frequency_khz == 87500 and config.rf_attenuation_db == 22.5
        assert config.rds_enabled and config.audio_mode == "SINE"
        self.sleep(1)
    def set_tx_enabled(self, tx, on):
        assert tx == self.tx
        self.calls.append(on)
        self.on = on
        self.last = self.now
        self.sleep(.15)
    def clear_rds(self, tx):
        assert tx == self.tx
        self.queue.clear()
        self.armed = self.starved = False
    def push_rds(self, tx, groups):
        assert tx == self.tx and 0 < len(groups) <= 31
        expected_pi = 0x1234 if tx == 0 else 0x5678
        expected_text = b"FMTEST  " if tx == 0 else b"TEST2   "
        for a, b, c, d in groups:
            segment = self.sent % 4
            assert a == expected_pi and b == (8 | segment | (4 if segment == 3 else 0))
            assert c == 0xe0cd and d == int.from_bytes(expected_text[2*segment:2*segment+2], 'big')
            self.sent += 1
        self.queue.extend(groups)
        assert len(self.queue) <= 23
    def qn_read_register(self, tx, reg):
        assert tx == self.tx and reg == 0 and not self.on
        return 7

for tx in (0, 1):
    rp = Client(tx)
    with contextlib.redirect_stdout(io.StringIO()) as output:
        module.exercise(rp, tx, clock=rp.clock, sleep=rp.sleep)
    assert rp.calls == [True, False]  # Stop/resume of RDS never restarts RF.
    assert rp.underruns == 1 and rp.consumed > 800 and not rp.queue
    assert "TXREQ OFF verified" in output.getvalue()
rp = Client(0, fail=True)
try:
    with contextlib.redirect_stdout(io.StringIO()):
        module.exercise(rp, 0, clock=rp.clock, sleep=rp.sleep)
except RuntimeError as exc:
    assert str(exc) == "injected transport failure"
else:
    raise AssertionError("Expected transport failure")
assert rp.calls == [True, False] and not rp.queue
print("M8.6B smoke: encoder bytes, bounded refill, 850 ms status, one underrun, resume without RF restart, cleanup PASS")
