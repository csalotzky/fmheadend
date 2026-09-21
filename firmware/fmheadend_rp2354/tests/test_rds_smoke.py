"""Validate bench smoke commands without hardware or third-party dependencies."""
import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import types


class Client:
    calls = []
    fail_after_push = False
    version = "0.8.6a"

    def __init__(self, device, address):
        assert device == "/dev/i2c-1" and address == 0x62
        self.free = [64] * 8

    def get_info(self):
        return {"rp_firmware_version": self.version}

    def get_rds_fifo_free(self):
        return self.free.copy()

    def get_status(self):
        if self.fail_after_push and self.free[0] == 62:
            raise RuntimeError("injected read failure")
        return {"state": "OK", "modulators": [
            {"id": tx, "rds_fifo_free": free, "rds_underrun_count": 0, "tx_state": "OFF"}
            for tx, free in enumerate(self.free)]}

    def push_rds(self, tx, groups):
        assert tx == 0
        assert groups == [(0x1234, 0xABCD, 0x8001, 0x00FF),
                          (0xFFFF, 0, 0x55AA, 0xA55A)]
        self.free[0] -= len(groups)
        self.calls.append("push")

    def clear_rds(self, tx):
        assert tx == 0
        self.free[0] = 64
        self.calls.append("clear")

    def close(self):
        self.calls.append("close")


fake = types.ModuleType("daemon.rp2354.linux_client")
fake.Rp2354Client = Client
sys.modules[fake.__name__] = fake
spec = importlib.util.spec_from_file_location("rds_smoke", Path(__file__).parents[1] / "tools/rds_smoke.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
sys.argv = ["rds_smoke.py", "--address", "0x62"]
with contextlib.redirect_stdout(io.StringIO()) as out:
    module.main()
assert "PASS:" in out.getvalue()
assert Client.calls == ["push", "clear", "close"]
Client.calls.clear()
Client.version = "0.8.6b"
with contextlib.redirect_stdout(io.StringIO()):
    module.main()
assert Client.calls == ["push", "clear", "close"]
Client.calls.clear()
Client.fail_after_push = True
try:
    with contextlib.redirect_stdout(io.StringIO()):
        module.main()
except RuntimeError as exc:
    assert str(exc) == "injected read failure"
else:
    raise AssertionError("Expected failure")
assert Client.calls == ["push", "clear", "close"]
print("RDS smoke: existing client, known groups, status/free, cleanup, no RF commands PASS")
