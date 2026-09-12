"""Exact V1 contract tests. Only low-level OS I2C calls are mocked."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock
import struct
import sys

import pytest
from fastapi.testclient import TestClient

from fmheadendd.app import create_app
from fmheadendd.config import Settings
from fmheadendd.rp2354 import protocol as p
from fmheadendd.rp2354 import linux_client as linux
from fmheadendd.rp2354.client import MockRp2354Client, TxConfig

INFO = struct.pack('<H16s16sB', 1, b'0.1.0', b'Pico2', 8)
CONFIG = TxConfig(frequency_khz=98100, rf_output_dbuv=96.0, stereo=True, preemphasis_us=50,
                  audio_mode='DIGITAL', rds_enabled=False)


def status_payload():
    return struct.pack('<BIIB', 0, 123456, 9, 0) + b''.join(
        struct.pack('<BhIIHB', i % 2, -32768 if i == 0 else 875, i + 10, i + 20, i + 30, 0)
        for i in range(8))


@pytest.fixture
def calls(monkeypatch):
    os_calls = SimpleNamespace(open=Mock(return_value=42), close=Mock(),
        write=Mock(side_effect=lambda fd, packet: len(packet)), read=Mock(), O_RDWR=2, O_CLOEXEC=0)
    def ioctl(fd, command, value, *args):
        if command == linux.I2C_FUNCS:
            value[0] = 1
        return 0
    ioctl_calls = SimpleNamespace(ioctl=Mock(side_effect=ioctl), flock=Mock(), LOCK_EX=2, LOCK_NB=4)
    monkeypatch.setattr(linux, 'os', os_calls)
    monkeypatch.setattr(linux, 'fcntl', ioctl_calls)
    monkeypatch.setattr(linux, 'sys', SimpleNamespace(platform='linux'))
    return os_calls, ioctl_calls


def replies(calls, *payloads):
    calls[0].read.side_effect = [part for payload in payloads for part in
        ([bytes((0, len(payload))), payload] if payload else [b'\0\0'])]


def client():
    return linux.Rp2354Client('/dev/i2c-test', 0x42)


def packets(calls):
    return [call.args[1] for call in calls[0].write.call_args_list]


@pytest.mark.parametrize('channel', range(8))
def test_exact_targets_and_enable(calls, channel):
    replies(calls, INFO, b'', b'')
    rp = client()
    rp.set_tx_enabled(channel, True)
    rp.set_tx_enabled(channel, False)
    assert packets(calls) == [b'\x01\xff\x00', bytes((3, channel, 1, 1)), bytes((3, channel, 1, 0))]
    rp.close()
    calls[0].close.assert_called_once_with(42)


@pytest.mark.parametrize('mode,wire_mode', [('DIGITAL', 0), ('ANALOG', 1)])
def test_config_exact_and_never_enables(calls, mode, wire_mode):
    replies(calls, INFO, b'')
    client().set_tx_config(7, CONFIG.model_copy(update={'audio_mode': mode}))
    assert packets(calls) == [b'\x01\xff\x00', b'\x02\x07\x0a\x34\x7f\x01\x00\xc0\x03\x01\x32' + bytes((wire_mode, 0))]
    assert all(packet[0] != p.Command.SET_TX_ENABLED for packet in packets(calls))


def test_get_info_and_linux_setup(calls):
    replies(calls, INFO)
    rp = client()
    assert rp.get_info() == {'rp_protocol_version': 1, 'rp_firmware_version': '0.1.0',
                              'hardware_revision': 'Pico2', 'modulator_count': 8}
    assert packets(calls) == [b'\x01\xff\x00']
    assert [call.args[1] for call in calls[0].read.call_args_list] == [2, 35]
    calls[1].ioctl.assert_any_call(42, linux.I2C_SLAVE, 0x42)
    calls[1].ioctl.assert_any_call(42, linux.I2C_TIMEOUT, 20)
    calls[1].ioctl.assert_any_call(42, linux.I2C_RETRIES, 0)
    assert calls[1].flock.call_count == 1


def test_get_status_exact_single_snapshot(calls):
    replies(calls, INFO, status_payload())
    result = client().get_status()
    assert packets(calls) == [b'\x01\xff\x00', b'\x04\xff\x00']
    assert result['uptime_ms'] == 123456 and result['rp_spi_audio_link_error_count'] == 9
    assert result['modulators'][0]['rf_output_dbuv'] is None
    assert result['modulators'][7] == {'id': 7, 'tx_state': 'ON', 'rf_output_dbuv': 87.5,
        'audio_underrun_count': 17, 'rds_underrun_count': 27, 'rds_fifo_free': 37, 'last_error_code': None}


def test_debug_rds_reset_exact(calls):
    replies(calls, INFO, b'\xa5', b'', b'', b'', b'', b'')
    rp = client()
    assert rp.qn_read_register(2, 0x12) == 0xa5
    rp.qn_write_register(3, 0x34, 0x56)
    rp.push_rds(4, [(0x1234, 0xabcd, 0, 0xffff), (1, 2, 3, 4)])
    rp.clear_rds(4)
    rp.reset_error_counters()
    rp.reset()
    assert packets(calls) == [b'\x01\xff\x00', b'\x07\x02\x01\x12', b'\x08\x03\x02\x34\x56',
        b'\x05\x04\x11\x02\x34\x12\xcd\xab\x00\x00\xff\xff\x01\x00\x02\x00\x03\x00\x04\x00',
        b'\x06\x04\x00', b'\x09\xff\x00', b'\x0a\xff\x00']
    assert rp.info is None
    calls[0].close.assert_called_once()


@pytest.mark.parametrize('status,error_type', list(p.ERROR_TYPES.items()))
def test_all_protocol_status_errors(status, error_type):
    with pytest.raises(error_type, match=status.name) as exc:
        p.response(p.Command.SET_TX_ENABLED, bytes((status, 0)))
    assert exc.value.rp_status == status
    assert exc.value.command == p.Command.SET_TX_ENABLED


@pytest.mark.parametrize('data', [b'', b'\0', b'\0\0x', b'\0\xff', b'\xff\0', b'\x05\x01x'])
def test_bad_response_framing(data):
    with pytest.raises(p.ProtocolError):
        p.response(p.Command.SET_TX_ENABLED, data)


@pytest.mark.parametrize('command', list(p.Command))
def test_each_response_length_is_exact(command):
    length = p.RESPONSE_SIZES[command]
    good = bytes((0, length)) + bytes(length)
    assert p.response(command, good) == bytes(length)
    for bad in (good + b'x', good[:-1], bytes((0, (length + 1) % 256)) + bytes(length + 1)):
        with pytest.raises(p.ProtocolError):
            p.response(command, bad)


@pytest.mark.parametrize('target', [-1, 8, 254, 255, 256, True, 1.0])
def test_invalid_channel_before_any_io(calls, target):
    with pytest.raises(ValueError):
        client().clear_rds(target)
    calls[0].open.assert_not_called()


@pytest.mark.parametrize('command', list(p.GLOBAL_COMMANDS))
@pytest.mark.parametrize('target', [0, 7, 8])
def test_global_command_rejects_channel(command, target):
    with pytest.raises(ValueError):
        p.request(command, target)


@pytest.mark.parametrize('changes', [{'frequency_khz': 0}, {'frequency_khz': None}, {'frequency_khz': 2**32},
    {'rf_output_dbuv': -1}, {'rf_output_dbuv': 6553.6}, {'preemphasis_us': 25}, {'audio_mode': 'bad'},
    {'stereo': 2}, {'rds_enabled': 'yes'}])
def test_invalid_config_does_not_touch_i2c(calls, changes):
    with pytest.raises(ValueError):
        client().set_tx_config(0, CONFIG.model_copy(update=changes))
    calls[0].open.assert_not_called()


@pytest.mark.parametrize('groups', [[], [(1, 2, 3)], [(1, 2, 3, 65536)], [(1, 2, 3, -1)], [(0, 0, 0, 0)] * 32])
def test_invalid_rds_transport_groups(calls, groups):
    with pytest.raises(ValueError):
        client().push_rds(0, groups)
    calls[0].open.assert_not_called()


def test_maximum_rds_payload():
    data = p.rds_groups([(0xffff, 0, 1, 2)] * 31)
    assert len(data) == 249
    assert len(p.request(p.Command.RDS_PUSH, 0, data)) == 252
    with pytest.raises(ValueError):
        p.request(p.Command.RDS_PUSH, 0, b'x' * 256)


@pytest.mark.parametrize('version,count', [(2, 8), (0, 8), (1, 7), (1, 9)])
def test_incompatible_handshake_blocks_commands(calls, version, count):
    replies(calls, struct.pack('<H16s16sB', version, b'fw', b'hw', count))
    rp = client()
    with pytest.raises(p.IncompatibleProtocol):
        rp.set_tx_enabled(0, True)
    assert packets(calls) == [b'\x01\xff\x00']
    assert rp.info is None


@pytest.mark.parametrize('text', [b'x' * 16, b'\0' * 16, b'x\0y' + b'\0' * 13, b'\xff' + b'\0' * 15, b'\n' + b'\0' * 15])
def test_invalid_info_strings(text):
    with pytest.raises(p.ProtocolError):
        p.parse_info(struct.pack('<H16s16sB', 1, text, b'hw', 8))


@pytest.mark.parametrize('offset,value', [(0, 2), (9, 7), (10, 3), (23, 7)])
def test_status_rejects_unknown_enums(offset, value):
    data = bytearray(status_payload()); data[offset] = value
    with pytest.raises(p.ProtocolError):
        p.parse_status(bytes(data))


def test_status_hardware_errors():
    data = bytearray(status_payload()); data[0] = 1; data[9] = 6; data[10] = 2; data[23] = 5
    result = p.parse_status(bytes(data))
    assert result['state'] == 'ERROR' and result['last_error_code'] == 'INTERNAL_ERROR'
    assert result['modulators'][0]['last_error_code'] == 'QN_ERROR'


def test_busy_is_bounded_and_safe_to_retry(calls):
    calls[0].read.side_effect = [b'\0\x23', INFO, b'\x04\0', b'\x04\0']
    rp = client()
    with pytest.raises(p.Busy):
        rp.set_tx_enabled(0, True)
    assert packets(calls) == [b'\x01\xff\0', b'\x03\0\x01\x01', b'\x03\0\x01\x01']
    assert 'BUSY' in rp.last_error


def test_busy_then_success(calls):
    calls[0].read.side_effect = [b'\x04\0', b'\0\x23', INFO]
    assert client().get_info()['rp_protocol_version'] == 1
    assert len(packets(calls)) == 2


@pytest.mark.parametrize('failure', [OSError(121, 'Remote I/O error'), b'', b'\0', b'\0\xff'])
def test_failed_read_never_replays_command(calls, failure):
    replies(calls, INFO)
    rp = client(); rp.get_info()
    calls[0].read.side_effect = [failure]
    with pytest.raises((p.HardwareUnavailable, p.ProtocolError)):
        rp.set_tx_enabled(0, True)
    assert len(packets(calls)) == 2
    assert rp.i2c_errors == 1 and rp.info is None
    with pytest.raises(p.HardwareUnavailable):
        rp.set_tx_enabled(0, True)
    assert len(packets(calls)) == 2


def test_short_write_never_replayed(calls):
    calls[0].write.side_effect = None; calls[0].write.return_value = 1
    with pytest.raises(p.ProtocolError, match='Short'):
        client().get_info()
    assert calls[0].write.call_count == 1
    calls[0].read.assert_not_called()


def test_reconnect_performs_fresh_handshake(calls):
    calls[0].open.side_effect = [FileNotFoundError('missing'), 42]
    rp = client()
    with pytest.raises(p.HardwareUnavailable): rp.get_status()
    rp._retry_at = 0
    replies(calls, INFO, status_payload())
    assert rp.get_status()['i2c_errors'] == 1
    assert packets(calls) == [b'\x01\xff\0', b'\x04\xff\0']


def test_requests_are_serialized(calls):
    replies(calls, INFO, *([b''] * 8))
    rp = client()
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda target: rp.set_tx_enabled(target, False), range(8)))
    assert packets(calls)[0] == b'\x01\xff\0'
    assert len(packets(calls)) == 9
    assert {packet[1] for packet in packets(calls)[1:]} == set(range(8))


def test_missing_slave_address_is_not_invented(calls):
    with pytest.raises(p.HardwareUnavailable, match='no default'):
        linux.Rp2354Client('/dev/i2c-1', None).get_info()
    calls[0].open.assert_not_called()


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Real Linux i2c-dev open failure test')
def test_linux_missing_device_real_os_calls(tmp_path):
    rp = linux.Rp2354Client(str(tmp_path / 'missing-i2c-device'), 0x42)
    with pytest.raises(p.HardwareUnavailable, match='No such file'):
        rp.get_info()
    assert rp.i2c_errors == 1 and rp.info is None


def test_high_level_mock_still_configures_without_enabling():
    rp = MockRp2354Client(); rp.set_tx_config(0, CONFIG)
    assert rp.enabled == [False] * 8


def test_http_stays_available_when_real_i2c_fails(calls, tmp_path):
    calls[0].open.side_effect = FileNotFoundError('I2C missing')
    rp = client()
    with TestClient(create_app(Settings(tmp_path / 'test.db'), rp=rp)) as api:
        assert api.get('/docs').status_code == 200
        status = api.get('/api/v1/status').json()
        assert status['system']['rp2354_state'] == 'UNRESPONSIVE'
        assert status['system']['system_state'] == 'DEGRADED'
        assert status['system']['mocked'] is False
        assert status['modulators'][0]['hardware_tx_state'] is None
        assert status['modulators'][0]['rf_output_dbuv'] is None
        info = api.get('/api/v1/system/info').json()
        assert info['rp_firmware_version'] is None and info['mocked'] is False


def test_native_status_reaches_existing_api(calls, tmp_path):
    diagnostic = [False]
    def read(fd, size):
        command = packets(calls)[-1][0]
        data = bytearray(status_payload())
        if not diagnostic[0]:
            for i in range(8): data[10 + i * 14] = 0
        payload = INFO if command == 1 else bytes(data) if command == 4 else b''
        return bytes((0, len(payload))) if size == 2 else payload
    calls[0].read.side_effect = read
    rp = client()
    with TestClient(create_app(Settings(tmp_path / 'test.db'), rp=rp)) as api:
        diagnostic[0] = True
        api.app.state.control.executor.submit(api.app.state.services.status.refresh).result()
        status = api.get('/api/v1/status').json()
        assert status['modulators'][1]['hardware_tx_state'] == 'ON'
        assert status['modulators'][1]['rf_output_dbuv'] == 87.5
        assert status['modulators'][1]['audio_underrun_count'] == 11
        assert status['modulators'][1]['underruns'] == 0  # Separate mux count.
        assert api.get('/api/v1/system/info').json()['rp_firmware_version'] == '0.1.0'

@pytest.mark.parametrize('code', [1, 2, 3, 5, 6])
def test_client_maps_protocol_failure_without_retry(calls, code):
    calls[0].read.side_effect = [b'\0\x23', INFO, bytes((code, 0))]
    rp = client()
    with pytest.raises(p.ERROR_TYPES[p.Status(code)]):
        rp.clear_rds(0)
    assert len(packets(calls)) == 2
    assert p.Status(code).name in rp.last_error


@pytest.mark.parametrize('command,payload', [(p.Command.SET_TX_ENABLED, b'\x02'),
    (p.Command.SET_TX_CONFIG, struct.pack('<IHBBBB', 98100, 0, 1, 50, 2, 0))])
def test_raw_request_rejects_invalid_enum(command, payload):
    with pytest.raises(ValueError):
        p.request(command, 0, payload)


@pytest.mark.parametrize('read_payload', [INFO[:-1], INFO + b'x'])
def test_client_rejects_truncated_or_oversized_payload(calls, read_payload):
    calls[0].read.side_effect = [b'\0\x23', read_payload]
    rp = client()
    with pytest.raises(p.ProtocolError): rp.get_info()
    assert rp.info is None
    calls[0].close.assert_called_once()


@pytest.mark.parametrize('failure', [PermissionError('permission denied'), OSError(16, 'device busy')])
def test_linux_open_failures_are_typed(calls, failure):
    calls[0].open.side_effect = failure
    with pytest.raises(p.HardwareUnavailable) as exc:
        client().get_info()
    assert str(failure) in str(exc.value)
    calls[0].write.assert_not_called()


def test_adapter_configuration_failure_closes_fd(calls):
    calls[1].ioctl.side_effect = OSError(25, 'unsupported ioctl')
    with pytest.raises(p.HardwareUnavailable): client().get_info()
    calls[0].close.assert_called_once_with(42)
    calls[0].write.assert_not_called()


def test_adapter_without_i2c_is_rejected(calls):
    calls[1].ioctl.side_effect = None
    with pytest.raises(p.HardwareUnavailable, match='plain I2C'):
        client().get_info()
    calls[0].close.assert_called_once_with(42)


@pytest.mark.parametrize('address', [-1, 0, 7, 120, 128, True, 66.0])
def test_invalid_slave_address(address):
    with pytest.raises(ValueError): linux.Rp2354Client('/dev/i2c-1', address)


@pytest.mark.parametrize('value', [-1, 256, True])
def test_invalid_register_args_before_io(calls, value):
    with pytest.raises(ValueError): client().qn_read_register(0, value)
    with pytest.raises(ValueError): client().qn_write_register(0, 0, value)
    calls[0].open.assert_not_called()


def test_api_incompatible_protocol_does_not_enable_or_crash(calls, tmp_path):
    incompatible = struct.pack('<H16s16sB', 2, b'fw', b'hw', 8)
    calls[0].read.side_effect = lambda fd, size: b'\0\x23' if size == 2 else incompatible
    rp = client()
    with TestClient(create_app(Settings(tmp_path / 'unsupported.db'), rp=rp)) as api:
        assert api.get('/docs').status_code == 200
        result = api.get('/api/v1/status').json()
        assert result['system']['system_state'] == 'DEGRADED'
        assert 'Unsupported RP protocol' in result['system']['last_error']
        assert api.get('/api/v1/system/info').json()['mocked'] is False
    assert packets(calls) and all(packet == b'\x01\xff\0' for packet in packets(calls))


def test_config_encoding_failure_releases_source(tmp_path):
    from fmheadendd.database import Database
    from fmheadendd.models.configuration import InputSourceConfig, ModulatorPatch
    from fmheadendd.modulators.manager import ModulatorManager
    from fmheadendd.sources.manager import SourceManager
    from fmheadendd.mux.client import MockMuxClient
    from fmheadendd.errors import DomainError
    db = Database(tmp_path / 'range.db')
    try:
        source = db.save('sources', InputSourceConfig(name='test', type='HTTP', config={'uri':'https://example.org/live'}))
        sources = SourceManager(db)
        rp = client()
        manager = ModulatorManager(db, sources, rp, MockMuxClient(), Settings(tmp_path / 'range.db'))
        manager.update(0, ModulatorPatch(input_source_id=source.id, frequency_khz=98100, rf_output_dbuv=None))
        with pytest.raises(DomainError, match='rf_output_dbuv'):
            manager.start(0)
        assert not manager.active_sources and not sources.pipelines
    finally:
        db.close()

@pytest.mark.parametrize('flags,real_rp', [([], True), (['--mock'], False), (['--mock-rp'], False), (['--mock-mux'], True)])
def test_cli_selects_explicit_hardware_mode(monkeypatch, tmp_path, flags, real_rp):
    import fmheadendd.app as application
    import uvicorn
    factory = Mock(return_value='test-app')
    server = Mock()
    monkeypatch.setattr(application, 'create_app', factory)
    monkeypatch.setattr(uvicorn, 'run', server)
    monkeypatch.setattr(sys, 'argv', ['fmheadendd', '--database', str(tmp_path / 'cli.db'), *flags])
    application.main()
    rp = factory.call_args.kwargs['rp']
    assert isinstance(rp, linux.Rp2354Client) == real_rp
    assert isinstance(rp, MockRp2354Client) != real_rp
    assert server.call_args.args[0] == 'test-app'


def test_all_config_options_exact():
    config = CONFIG.model_copy(update={'stereo': False, 'preemphasis_us': 75, 'rds_enabled': True, 'audio_mode': 'ANALOG'})
    assert p.tx_config(config) == b'\x34\x7f\x01\x00\xc0\x03\0\x4b\x01\x01'

@pytest.mark.parametrize('level,wire', [(0, 0), (96.0, 960), (100, 1000), (96.1, 961), (6553.5, 65535)])
def test_review_rf_physical_units(level, wire):
    config = CONFIG.model_copy(update={'rf_output_dbuv': level})
    data = p.tx_config(config)
    assert len(data) == 10 and data[4:6] == struct.pack('<H', wire)


@pytest.mark.parametrize('level', [-0.1, 6553.6, 96.01, float('nan'), float('inf'), True, '96'])
def test_review_rf_model_validation(level):
    from pydantic import ValidationError
    from fmheadendd.models.configuration import ModulatorConfig
    with pytest.raises(ValidationError): ModulatorConfig(rf_output_dbuv=level)
    with pytest.raises(ValidationError): TxConfig(**(CONFIG.model_dump() | {'rf_output_dbuv': level}))


def test_fifo_free_exact_request_response(calls):
    values = [0, 1, 255, 256, 1024, 4096, 32768, 65535]
    replies(calls, INFO, struct.pack('<8H', *values))
    assert client().get_rds_fifo_free() == values
    assert packets(calls) == [b'\x01\xff\0', b'\x0b\xff\0']
    assert MockRp2354Client().get_rds_fifo_free() == [64] * 8


@pytest.mark.parametrize('data', [b'', bytes(15), bytes(17)])
def test_fifo_free_bad_payload(data):
    with pytest.raises(p.ProtocolError): p.parse_rds_fifo_free(data)


@pytest.mark.parametrize('global_active,tx_active', [(False, False), (True, False), (False, True)])
def test_latched_errors_do_not_imply_current_fault(calls, tmp_path, global_active, tx_active):
    data = bytearray(status_payload())
    for i in range(8): data[10 + i * 14] = 0
    data[9] = 5; data[23] = 5
    def read(fd, size):
        command = packets(calls)[-1][0]
        payload = INFO if command == 1 else bytes(data) if command == 4 else b''
        return bytes((0, len(payload))) if size == 2 else payload
    calls[0].read.side_effect = read
    with TestClient(create_app(Settings(tmp_path / 'latched.db'), rp=client())) as api:
        data[0] = int(global_active); data[10] = 2 if tx_active else 0
        api.app.state.control.executor.submit(api.app.state.services.status.refresh).result()
        status = api.get('/api/v1/status').json()
        assert status['system']['rp_last_error_code'] == 'QN_ERROR'
        assert status['modulators'][0]['hardware_last_error'] == 'QN_ERROR'
        assert status['system']['system_state'] == ('DEGRADED' if global_active or tx_active else 'RUNNING')
        assert status['system']['rp_spi_audio_link_error_count'] == 9


def test_legacy_rf_migration_and_reopen(tmp_path):
    import json
    from fmheadendd.database import Database
    from fmheadendd.models.configuration import ModulatorPatch
    path = tmp_path / 'legacy.db'
    db = Database(path)
    with db.connection:
        for row in db.connection.execute('SELECT id, data FROM modulators').fetchall():
            data = json.loads(row['data']); data.pop('rf_output_dbuv'); data['rf_level'] = 76
            data['frequency_khz'] = 98100
            db.connection.execute('UPDATE modulators SET data=? WHERE id=?', (json.dumps(data), row['id']))
        db.connection.execute('PRAGMA user_version=1')
    db.close()
    db = Database(path)
    assert all(row.rf_output_dbuv is None and row.frequency_khz == 98100 for row in db.list('modulators'))
    assert db.connection.execute('PRAGMA user_version').fetchone()[0] == 6
    assert 'rf_level' not in db.connection.execute('SELECT data FROM modulators').fetchone()[0]
    config = db.get('modulators', 0).model_copy(update={'rf_output_dbuv': 96.0})
    db.save('modulators', config, 0); db.close()
    db = Database(path)
    assert db.get('modulators', 0).rf_output_dbuv == 96.0
    db.close()
