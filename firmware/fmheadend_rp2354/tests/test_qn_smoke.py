"""Exercise the no-RF bench command with a fake Linux client, no dependencies."""
import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import types


class InvalidArgument(Exception):
    pass


class Config:
    def __init__(self, **values):
        self.__dict__.update(values)

    def model_copy(self, update):
        return Config(**(self.__dict__ | update))


class Client:
    calls = []

    def __init__(self, device, address):
        assert device == '/dev/i2c-1' and address == 0x62

    def get_info(self):
        return {'rp_firmware_version': '0.8.5'}

    def get_status(self):
        return {'state': 'OK', 'rp_spi_audio_link_error_count': 0,
                'modulators': [{'id': n, 'tx_state': 'OFF', 'rf_attenuation_db': 22.5}
                               for n in range(8)]}

    def qn_read_register(self, tx, reg):
        assert tx in range(8) and reg == 6
        return 0x11

    def set_tx_enabled(self, tx, enabled):
        assert tx == 0 and not enabled  # This command must never turn RF ON.
        self.calls.append(('off', tx))

    def set_tx_config(self, tx, config):
        assert tx == 0 and config.audio_mode == 'DIGITAL'
        if config.frequency_khz == 98101:
            raise InvalidArgument()
        assert config.frequency_khz == 98100
        self.calls.append(('configured', tx))

    def close(self):
        self.calls.append(('closed',))


for name, values in {
    'daemon.rp2354.linux_client': {'Rp2354Client': Client},
    'daemon.rp2354.client': {'TxConfig': Config},
    'daemon.rp2354.protocol': {'InvalidArgument': InvalidArgument},
}.items():
    fake = types.ModuleType(name)
    fake.__dict__.update(values)
    sys.modules[name] = fake
spec = importlib.util.spec_from_file_location('qn_smoke', Path(__file__).parents[1] / 'tools/qn_smoke.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
sys.argv = ['qn_smoke', '--address', '0x62', 'configure', '--tx', '0',
            '--frequency-khz', '98100', '--attenuation-db', '22.5', '--mode', 'DIGITAL']
with contextlib.redirect_stdout(io.StringIO()):
    module.main()
assert Client.calls == [('off', 0), ('configured', 0), ('off', 0), ('closed',)]
print('No-RF bench configure: real client calls, validation, never ON PASS')
