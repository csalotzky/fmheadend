"""No-hardware checks for the explicit RF-on bench launcher."""
import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import types
import unittest

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT))
from daemon.rds.encoder import group_0a


class Config:
    def __init__(self, **values): self.__dict__.update(values)


for name, values in {
    "daemon.rp2354.client": {"TxConfig": Config},
    "daemon.rp2354.linux_client": {"Rp2354Client": None},
}.items():
    fake=types.ModuleType(name);fake.__dict__.update(values);sys.modules[name]=fake
spec=importlib.util.spec_from_file_location("launcher",ROOT/"firmware/fmheadend_rp2354/tools/qn_rds_local_probe.py")
launcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(launcher)


class Hardware:
    def __init__(self, version="0.8.6a-probe", stop=60):
        self.version=version;self.stop=stop;self.now=0;self.started=None;self.enabled=False;self.calls=[]
    def clock(self): return self.now
    def sleep(self, seconds): self.now+=seconds
    def get_info(self): return {"rp_firmware_version":self.version}
    def get_status(self):
        if self.started is not None and self.now-self.started>=self.stop: self.enabled=False
        return {"state":"OK","rp_spi_audio_link_error_count":0,"modulators":[
            {"tx_state":"ON" if i==0 and self.enabled else "OFF"} for i in range(8)]}
    def set_tx_config(self, tx, config):
        assert tx==0 and not self.enabled
        assert config.__dict__==dict(frequency_khz=87500,rf_attenuation_db=22.5,
            stereo=True,preemphasis_us=50,audio_mode="DIGITAL",rds_enabled=True,
            gain_mode="AUTO",analog_gain_db=0.0,digital_trim_db=0,soft_clipping=False)
        self.calls.append("config")
    def set_tx_enabled(self, tx, enabled):
        assert tx==0
        self.calls.append("on" if enabled else "off");self.enabled=enabled
        if enabled: self.started=self.now
    def close(self): self.calls.append("close")


class Tests(unittest.TestCase):
    def test_bench_words_match_existing_linux_encoder(self):
        self.assertEqual([group_0a(0xffff,0,False,b" R D S  ",i) for i in range(4)],
            [(0xffff,8,0xe0cd,0x2052),(0xffff,9,0xe0cd,0x2044),
             (0xffff,10,0xe0cd,0x2053),(0xffff,15,0xe0cd,0x2020)])
    def test_normal_run_no_raw_register_or_fifo_methods_exist(self):
        hw=Hardware()
        with contextlib.redirect_stdout(io.StringIO()):
            launcher.run(hw,clock=hw.clock,sleep=hw.sleep)
        self.assertEqual(hw.calls,["off","config","on","off","close"])
        self.assertFalse(hw.enabled);self.assertEqual(hw.now,60)
    def test_wrong_build_never_enables(self):
        hw=Hardware(version="0.8.6a")
        with contextlib.redirect_stdout(io.StringIO()),self.assertRaisesRegex(RuntimeError,"Flash"):
            launcher.run(hw,clock=hw.clock,sleep=hw.sleep)
        self.assertEqual(hw.calls,["close"])
    def test_early_stop_and_missing_stop_clean_up(self):
        for deadline, message in [(10,"early"),(100,"did not finish")]:
            hw=Hardware(stop=deadline)
            with contextlib.redirect_stdout(io.StringIO()),self.assertRaisesRegex(RuntimeError,message):
                launcher.run(hw,clock=hw.clock,sleep=hw.sleep)
            self.assertEqual(hw.calls[-2:],["off","close"])
            self.assertFalse(hw.enabled)


if __name__=="__main__": unittest.main()
