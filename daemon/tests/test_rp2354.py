"""Exact current protocol contract tests. Only low-level OS I2C calls are mocked."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock
import struct
from binascii import crc_hqx
import sys

import pytest
from fastapi.testclient import TestClient

from fmheadendd.app import create_app
from fmheadendd.config import Settings
from fmheadendd.rp2354 import protocol as p
from fmheadendd.rp2354 import linux_client as linux
from fmheadendd.rp2354.client import MockRp2354Client, TxConfig

INFO = struct.pack('<H16s16sB', 1, b'0.1.0', b'Pico2', 8)
CONFIG = TxConfig(frequency_khz=98100, rf_attenuation_db=6.0, stereo=True, preemphasis_us=50,
                  audio_mode='DIGITAL', rds_enabled=False)


def status_payload():
    return struct.pack('<BIIB', 0, 123456, 9, 0) + b''.join(
        struct.pack('<BhIIHBB', i % 2, -32768 if i == 0 else 60, i + 10, i + 20, i + 30, 0, 255)
        for i in range(8))


@pytest.fixture
def calls(monkeypatch):
    monkeypatch.setattr(linux.time, "sleep", Mock())
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


def frame(body):
    return body + struct.pack('<H', crc_hqx(body, 0xFFFF))


def reads(header, payload=b''):
    return [header, frame(header + payload)[2:]]


def replies(calls, *payloads):
    calls[0].read.side_effect = [part for payload in payloads
        for part in reads(bytes((0, len(payload))), payload)]


def client():
    return linux.Rp2354Client('/dev/i2c-test', 0x42)


def packets(calls):
    wire = [call.args[1] for call in calls[0].write.call_args_list]
    assert all(packet == frame(packet[:-2]) for packet in wire)
    return [packet[:-2] for packet in wire]  # Semantic assertions below; CRC checked above.


@pytest.mark.parametrize('channel', range(8))
def test_exact_targets_and_enable(calls, channel):
    replies(calls, INFO, b'', b'')
    rp = client()
    rp.set_tx_enabled(channel, True)
    rp.set_tx_enabled(channel, False)
    assert packets(calls) == [b'\x01\xff\x00', bytes((3, channel, 1, 1)), bytes((3, channel, 1, 0))]
    rp.close()
    calls[0].close.assert_called_once_with(42)


@pytest.mark.parametrize('mode,wire_mode', [('DIGITAL', 0), ('ANALOG', 1), ('SINE', 2)])
def test_config_exact_and_never_enables(calls, mode, wire_mode):
    replies(calls, INFO, b'')
    client().set_tx_config(7, CONFIG.model_copy(update={'audio_mode': mode}))
    assert packets(calls) == [b'\x01\xff\x00', b'\x02\x07\x0f\x34\x7f\x01\x00\x3c\x00\x01\x32' + bytes((wire_mode, 0, 0, 0, 0, 0, 0))]
    assert all(packet[0] != p.Command.SET_TX_ENABLED for packet in packets(calls))


def test_get_info_and_linux_setup(calls):
    replies(calls, INFO)
    rp = client()
    assert rp.get_info() == {'rp_protocol_version': 1, 'rp_firmware_version': '0.1.0',
                              'hardware_revision': 'Pico2', 'modulator_count': 8}
    assert packets(calls) == [b'\x01\xff\x00']
    assert [call.args[1] for call in calls[0].read.call_args_list] == [2, 37]
    calls[1].ioctl.assert_any_call(42, linux.I2C_SLAVE, 0x42)
    calls[1].ioctl.assert_any_call(42, linux.I2C_TIMEOUT, 20)
    calls[1].ioctl.assert_any_call(42, linux.I2C_RETRIES, 0)
    assert calls[1].flock.call_count == 1


def test_get_status_exact_single_snapshot(calls):
    replies(calls, INFO, status_payload())
    result = client().get_status()
    assert packets(calls) == [b'\x01\xff\x00', b'\x04\xff\x00']
    assert result['uptime_ms'] == 123456 and result['rp_spi_audio_link_error_count'] == 9
    assert result['modulators'][0]['rf_attenuation_db'] is None
    assert result['modulators'][7] == {'id': 7, 'tx_state': 'ON', 'rf_attenuation_db': 6.0,
        'audio_underrun_count': 17, 'rds_underrun_count': 27, 'rds_fifo_free': 37, 'last_error_code': None, 'input_saturated': None}


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
        p.response(p.Command.SET_TX_ENABLED, frame(bytes((status, 0))))
    assert exc.value.rp_status == status
    assert exc.value.command == p.Command.SET_TX_ENABLED


@pytest.mark.parametrize('data', [b'', b'\0', b'\0\0x', b'\0\xff', b'\xff\0', b'\x05\x01x'])
def test_bad_response_framing(data):
    with pytest.raises(p.ProtocolError):
        p.response(p.Command.SET_TX_ENABLED, data)


@pytest.mark.parametrize('command', list(p.Command))
def test_each_response_length_is_exact(command):
    length = p.RESPONSE_SIZES[command]
    good = frame(bytes((0, length)) + bytes(length))
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
    {'rf_attenuation_db': -1}, {'rf_attenuation_db': 6553.6}, {'preemphasis_us': 25}, {'audio_mode': 'bad'},
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
    assert len(p.request(p.Command.RDS_PUSH, 0, data)) == 254
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
    calls[0].read.side_effect = reads(b'\0\x23', INFO) + reads(b'\x04\0') * 2
    rp = client()
    with pytest.raises(p.Busy):
        rp.set_tx_enabled(0, True)
    assert packets(calls) == [b'\x01\xff\0', b'\x03\0\x01\x01', b'\x03\0\x01\x01']
    assert 'BUSY' in rp.last_error


def test_busy_then_success(calls):
    calls[0].read.side_effect = reads(b'\x04\0') + reads(b'\0\x23', INFO)
    assert client().get_info()['rp_protocol_version'] == 1
    assert len(packets(calls)) == 2


@pytest.mark.parametrize('failure', [OSError(121, 'Remote I/O error'), b'', b'\0', b'\0\xff'])
def test_failed_read_never_replays_command(calls, failure):
    replies(calls, INFO)
    rp = client(); rp.get_info()
    calls[0].read.side_effect = [failure, b'']
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
        assert status['system']['rp2354_state'] == 'UNKNOWN'
        assert status['system']['rp_communication_state'] == 'UNAVAILABLE'
        assert status['system']['rp_observation_stale']
        assert status['system']['system_state'] == 'DEGRADED'
        assert status['system']['mocked'] is False
        assert status['modulators'][0]['hardware_tx_state'] is None
        assert status['modulators'][0]['rf_attenuation_db'] is None
        info = api.get('/api/v1/system/info').json()
        assert info['rp_firmware_version'] is None and info['mocked'] is False


def test_native_status_reaches_existing_api(calls, tmp_path):
    diagnostic = [False]
    def read(fd, size):
        command = packets(calls)[-1][0]
        data = bytearray(status_payload())
        if not diagnostic[0]:
            for i in range(8): data[10 + i * 15] = 0
        payload = INFO if command == 1 else bytes(data) if command == 4 else b''
        header = bytes((0, len(payload)))
        return header if calls[0].read.call_count % 2 else frame(header + payload)[2:]
    calls[0].read.side_effect = read
    rp = client()
    with TestClient(create_app(Settings(tmp_path / 'test.db'), rp=rp)) as api:
        diagnostic[0] = True
        api.app.state.control.executor.submit(api.app.state.services.status.refresh).result()
        status = api.get('/api/v1/status').json()
        assert status['modulators'][1]['hardware_tx_state'] == 'ON'
        assert status['modulators'][1]['rf_attenuation_db'] == 6.0
        assert status['modulators'][1]['audio_underrun_count'] == 11
        assert status['modulators'][1]['underruns'] == 0  # Separate mux count.
        assert api.get('/api/v1/system/info').json()['rp_firmware_version'] == '0.1.0'

@pytest.mark.parametrize('code', [1, 2, 3, 5, 6])
def test_client_maps_protocol_failure_without_retry(calls, code):
    calls[0].read.side_effect = reads(b'\0\x23', INFO) + reads(bytes((code, 0)))
    rp = client()
    with pytest.raises(p.ERROR_TYPES[p.Status(code)]):
        rp.clear_rds(0)
    assert len(packets(calls)) == 2
    assert p.Status(code).name in rp.last_error


@pytest.mark.parametrize('command,payload', [(p.Command.SET_TX_ENABLED, b'\x02'),
    (p.Command.SET_TX_CONFIG, struct.pack('<IHBBBBBhBB', 98100, 0, 1, 50, 3, 0, 0, 0, 0, 0))])
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
    calls[0].read.side_effect = lambda fd, size: b'\0\x23' if size == 2 else frame(b'\0\x23' + incompatible)[2:]
    rp = client()
    with TestClient(create_app(Settings(tmp_path / 'unsupported.db'), rp=rp)) as api:
        assert api.get('/docs').status_code == 200
        result = api.get('/api/v1/status').json()
        assert result['system']['system_state'] == 'DEGRADED'
        assert 'Unsupported Controller protocol' in result['system']['last_error']
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
        manager.update(0, ModulatorPatch(input_source_id=source.id, frequency_khz=98100, rf_attenuation_db=None))
        with pytest.raises(DomainError, match='rf_attenuation_db'):
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
    assert p.tx_config(config) == b'\x34\x7f\x01\x00\x3c\x00\0\x4b\x01\x01' + bytes(5)

@pytest.mark.parametrize('level,wire', [(i * 1.5, i * 15) for i in range(16)])
def test_rf_attenuation_steps(level, wire):
    config = CONFIG.model_copy(update={'rf_attenuation_db': level})
    data = p.tx_config(config)
    assert len(data) == 15 and data[4:6] == struct.pack('<H', wire)


@pytest.mark.parametrize('level', [-0.1, 24, 1, float('nan'), float('inf'), True, '6'])
def test_review_rf_model_validation(level):
    from pydantic import ValidationError
    from fmheadendd.models.configuration import ModulatorConfig
    with pytest.raises(ValidationError): ModulatorConfig(rf_attenuation_db=level)
    with pytest.raises(ValidationError): TxConfig(**(CONFIG.model_dump() | {'rf_attenuation_db': level}))


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
    for i in range(8): data[10 + i * 15] = 0
    data[9] = 5; data[23] = 5
    def read(fd, size):
        command = packets(calls)[-1][0]
        payload = INFO if command == 1 else bytes(data) if command == 4 else b''
        header = bytes((0, len(payload)))
        return header if calls[0].read.call_count % 2 else frame(header + payload)[2:]
    calls[0].read.side_effect = read
    with TestClient(create_app(Settings(tmp_path / 'latched.db'), rp=client())) as api:
        data[0] = int(global_active); data[10] = 2 if tx_active else 0
        api.app.state.control.executor.submit(api.app.state.services.status.refresh).result()
        status = api.get('/api/v1/status').json()
        assert status['system']['controller_last_error_code'] == 'QN_ERROR'
        assert status['modulators'][0]['hardware_last_error'] == 'QN_ERROR'
        assert status['system']['system_state'] == ('DEGRADED' if global_active or tx_active else 'RUNNING')
        assert status['system']['rp_spi_audio_link_error_count'] == 9


def test_m83_command_waits_and_two_read_cursor(calls):
    replies(calls, INFO, b'\x11', b'', b'', status_payload())
    rp = client()
    rp.get_info()
    assert rp.qn_read_register(7, 6) == 0x11
    rp.set_tx_config(0, CONFIG.model_copy(update={"audio_mode": "SINE"}))
    rp.set_tx_enabled(0, True)
    rp.get_status()
    assert [c.args[0] for c in linux.time.sleep.call_args_list] == [0.002, 0.1, 1.0, 0.15, 0.85]
    assert [c.args[1] for c in calls[0].read.call_args_list] == [2, 37, 2, 3, 2, 2, 2, 2, 2, 132]


def test_m83_sine_is_enum_only_and_invalid_modes_rejected():
    sine = TxConfig(**{**CONFIG.model_dump(), "audio_mode": "SINE"})
    payload = p.tx_config(sine)
    assert len(payload) == 15 and payload[8] == 2
    assert p.request(p.Command.SET_TX_CONFIG, 0, payload)[11] == 2
    with pytest.raises(ValueError):
        p.request(p.Command.SET_TX_CONFIG, 0, payload[:8] + b'\x03' + payload[9:])


@pytest.fixture
def running_wire_backend(calls, tmp_path):
    """Real client/coordinator/refill, eight desired ON TXs, only OS I/O faked."""
    from fmheadendd.database import Database
    from fmheadendd.models.configuration import ModulatorConfig
    path = tmp_path / 'wire-recovery.db'
    db = Database(path)
    for i in range(8):
        old = db.get('modulators', i)
        db.save('modulators', ModulatorConfig.model_validate(old.model_dump(exclude={'id', 'timestamp', 'rds_override_profile_id'}) | {
            'audio_mode': 'ANALOG', 'frequency_khz': 87500, 'rf_attenuation_db': 22.5,
            'enabled': True, 'rds_enabled': True}), i)
    db.close()
    wire = SimpleNamespace(enabled=[False]*8, uptime=100000, info=INFO, state=0, faults=[], payload=b'', now=0., body_pending=False, corrupt_crc=False)

    def snapshot():
        return p.GLOBAL_STATUS.pack(wire.state, wire.uptime, 0, 0) + b''.join(
            p.CHANNEL_STATUS.pack(int(on), 225, 0, 0, 41, 0, 255) for on in wire.enabled)

    def write(fd, packet):
        wire.body_pending = False
        if packet[0] == p.Command.SET_TX_ENABLED:
            wire.enabled[packet[1]] = bool(packet[3])
        return len(packet)

    def read(fd, size):
        if wire.body_pending:
            wire.body_pending = False
            return wire.payload
        wire.body_pending = True
        command = packets(calls)[-1][0]
        wire.payload = (wire.info if command == p.Command.GET_INFO else snapshot() if command == p.Command.GET_STATUS
                        else struct.pack('<8H', *([41]*8)) if command == p.Command.GET_RDS_FIFO_FREE else b'')
        if wire.faults and command == wire.faults[0][0]:
            _, header, wire.payload = wire.faults.pop(0)
        else:
            header = bytes((0, len(wire.payload)))
        wire.payload = frame(header + wire.payload)[2:]
        if wire.corrupt_crc == command:
            wire.payload = wire.payload[:-1] + bytes((wire.payload[-1] ^ 1,))
            wire.corrupt_crc = False
        return header

    calls[0].write.side_effect = write
    calls[0].read.side_effect = read
    app = create_app(Settings(path), rp=client(), clock=lambda: wire.now)
    with TestClient(app):
        def prime():
            wire.now += 1
            app.state.services.rds.tick()
            assert app.state.services.recovery.ready and wire.enabled == [True]*8
        app.state.control.executor.submit(prime).result()
        yield app, wire, snapshot


@pytest.mark.parametrize('kind', ['global128', 'tx255', 'status128', 'attenuation80e1', 'fifo_header', 'fifo_short'])
def test_single_malformed_read_retains_all_tx_and_rds(calls, running_wire_backend, caplog, kind):
    app, wire, snapshot = running_wire_backend
    def check():
        services = app.state.services
        original_channels = list(services.rds.channels)
        data = bytearray(snapshot())
        command, header, field = p.Command.GET_STATUS, b'\x00\x82', ''
        if kind == 'global128': data[9] = 128; field = 'global.last_error_code'
        if kind == 'tx255': data[23] = 255; field = 'TX0.last_error_code'
        if kind == 'status128': header = b'\x80\x82'; field = 'Unknown Controller status: 128'
        if kind == 'attenuation80e1': data[11:13] = b'\xe1\x80'; field = 'TX0.rf_attenuation_db_x10'
        if kind.startswith('fifo'):
            command = p.Command.GET_RDS_FIFO_FREE
            header = b'\0\x0f' if kind == 'fifo_header' else b'\0\x10'
            data = bytearray(15); field = 'GET_RDS_FIFO_FREE'
        wire.faults = [(command, header, bytes(data))]
        calls[0].write.reset_mock()
        if command == p.Command.GET_STATUS:
            services.status.refresh()
        else:
            wire.now += 1
            services.rds.tick()
        sent = packets(calls)
        assert [packet[0] for packet in sent] == [command]
        assert all(packet[0] not in (2, 3, 6) for packet in sent)
        assert services.recovery.ready and not services.rds.suspended
        assert all(a is b for a, b in zip(original_channels, services.rds.channels))
        assert wire.enabled == [True]*8 and all(services.modulators.desired_on(i) for i in range(8))
        # Refill continues in the same lifecycle, including after a bad status read.
        wire.now += 1
        services.rp._retry_at = 0
        services.rds.tick()
        assert any(packet[0] == p.Command.RDS_PUSH for packet in packets(calls))
        logs = [r.getMessage() for r in caplog.records if 'Controller malformed response' in r.getMessage()]
        assert len(logs) == 1
        assert any(r.name == 'fmheadendd.controller.linux_client' for r in caplog.records)
        assert f'command={command.name}' in logs[0] and f'header={header.hex()}' in logs[0] and field in logs[0]
        if kind == 'global128': assert 'Invalid hardware error code: 128' in logs[0]
        if kind == 'tx255': assert 'Invalid hardware error code: 255' in logs[0]
        if kind == 'attenuation80e1': assert 'Invalid RF attenuation: -32543' in logs[0]
        assert f'payload={frame(header + bytes(data))[2:].hex()}' in logs[0]
        assert f'payload_length={len(data)+2}' in logs[0]
        assert services.rp.i2c_errors == 1
    app.state.control.executor.submit(check).result()


def test_crc_vectors_and_maximum_push():
    assert p.crc16(b'123456789') == 0x29B1
    assert p.request(p.Command.GET_INFO, 255) == bytes.fromhex('01 ff 00 53 f8')
    payload = bytes((31,)) + bytes(range(248))
    packet = p.request(p.Command.RDS_PUSH, 7, payload)
    assert len(packet) == 254 and packet[-2:] == bytes.fromhex('f7 75')
    assert packet[:-2] == bytes((5,7,249)) + payload
    assert p.response(p.Command.RDS_PUSH, bytes.fromhex('00 00 0f 1d')) == b''


@pytest.mark.parametrize('command', list(p.Command))
def test_crc_rejects_every_single_bit_in_response(command):
    good = frame(bytes((0, p.RESPONSE_SIZES[command])) + bytes(p.RESPONSE_SIZES[command]))
    for bit in range(len(good) * 8):
        bad = bytearray(good); bad[bit // 8] ^= 1 << (bit % 8)
        with pytest.raises(p.ProtocolError):
            p.response(command, bytes(bad))


@pytest.mark.parametrize('command', [p.Command.GET_STATUS, p.Command.RDS_PUSH, p.Command.SET_TX_ENABLED])
@pytest.mark.parametrize('status', [p.Status.OK, p.Status.BUSY, p.Status.QN_ERROR, p.Status.INTERNAL_ERROR])
def test_crc_failure_keeps_certainty_and_never_accepts_status_or_retries(calls, command, status, caplog):
    rp = client(); rp.info = p.parse_info(INFO)
    payload = status_payload() if command == p.Command.GET_STATUS and status == p.Status.OK else b''
    wire = frame(bytes((status,len(payload))) + payload)
    bad = wire[:-1] + bytes((wire[-1] ^ 0x80,))
    calls[0].read.side_effect = [bad[:2], bad[2:]]
    with pytest.raises(p.MalformedResponse, match='CRC mismatch') as caught:
        if command == p.Command.GET_STATUS: rp.get_status()
        elif command == p.Command.RDS_PUSH: rp.push_rds(3, [(1,2,3,4)])
        else: rp.set_tx_enabled(3, True)
    assert caught.value.command == command and caught.value.stage == 'payload'
    assert caught.value.outcome == (p.Outcome.OBSERVATION if command == p.Command.GET_STATUS else p.Outcome.AMBIGUOUS)
    assert len(packets(calls)) == 1  # Even corrupted BUSY cannot authorize replay.
    record, = [r for r in caplog.records if 'Controller malformed response' in r.getMessage()]
    assert record.name == 'fmheadendd.controller.linux_client'
    assert record.levelname == 'WARNING'
    message = record.getMessage()
    assert f'command={command.name}' in message and f'header={bad[:2].hex()}' in message
    assert f'payload={bad[2:].hex()}' in message
    assert 'received 0x' in message and 'expected 0x' in message
    assert f'Controller I2C /dev/i2c-test address 66:' in rp.last_error


@pytest.mark.parametrize('command', [p.Command.GET_STATUS, p.Command.GET_RDS_FIFO_FREE, p.Command.RDS_PUSH])
def test_crc_failure_preserves_all_rf_and_existing_scoped_recovery(calls, running_wire_backend, command):
    app, wire, _ = running_wire_backend
    def check():
        s = app.state.services
        wire.corrupt_crc = command
        calls[0].write.reset_mock()
        wire.now += 1
        if command == p.Command.GET_STATUS: s.status.refresh()
        else: s.rds.tick()
        assert wire.enabled == [True]*8 and s.recovery.ready and not s.recovery.pending
        assert not s.rds.suspended
        assert all(x[0] not in (2,3,6) for x in packets(calls))
        if command == p.Command.RDS_PUSH:
            assert [c.needs_clear for c in s.rds.channels] == [True] + [False]*7
        else:
            assert not any(c.needs_clear for c in s.rds.channels)
        if command == p.Command.GET_STATUS:
            assert s.status.snapshot().system.rp_observation_stale
        wire.now += .3; s.rp._retry_at = 0
        s.rds.tick()
        sent = packets(calls)
        assert all(x[0] not in (2,3) for x in sent)
        if command == p.Command.RDS_PUSH:
            clears = [i for i,x in enumerate(sent) if x[0] == 6]
            pushes = [i for i,x in enumerate(sent) if x[0] == 5 and x[1] == 0]
            assert len(clears) == 1 and sent[clears[0]][1] == 0
            assert pushes[0] < clears[0] < pushes[1]  # No replay before confirmed CLEAR.
        else:
            assert all(x[0] != 6 for x in sent)
    app.state.control.executor.submit(check).result()


@pytest.mark.parametrize('failure', ['sustained', 'reset', 'incompatible', 'mismatch', 'attenuation', 'qn_fault', 'qn_response'])
def test_read_failures_and_observations_never_trigger_global_restore(calls, running_wire_backend, caplog, failure):
    app, wire, snapshot = running_wire_backend
    def check():
        services = app.state.services
        wire.faults = [(p.Command.GET_STATUS, b'\x80\x82', b'')]
        if failure == 'sustained': wire.faults *= 2
        if failure == 'reset': wire.uptime = 1
        if failure == 'incompatible': wire.info = p.INFO.pack(2, b'bad', b'hw', 8)
        if failure == 'mismatch': wire.enabled[3] = False
        if failure == 'attenuation':
            mismatched = bytearray(snapshot()); mismatched[11:13] = b'\x3c\0'
            wire.faults.append((p.Command.GET_STATUS, b'\0\x82', bytes(mismatched)))
        if failure == 'qn_fault': wire.state = 1
        if failure == 'qn_response': wire.faults.append((p.Command.GET_STATUS, b'\x05\0', b''))
        calls[0].write.reset_mock()
        services.status.refresh()
        assert services.recovery.ready and not services.rds.suspended
        wire.now += 1; services.rp._retry_at = 0
        services.status.refresh()
        assert services.recovery.ready and not services.rds.suspended
        assert all(packet[0] in (1, 4) for packet in packets(calls))
        if failure in ('qn_fault', 'mismatch', 'attenuation', 'qn_response'):
            assert services.status.snapshot().system.system_state == 'DEGRADED'
        if failure == 'qn_response':
            assert services.status.snapshot().system.rp_communication_state == 'AVAILABLE'
            assert services.status.snapshot().system.rp_observation_stale
        # Only the first malformed frame in the incident is dumped, including
        # a second corruption/incompatible response during verification.
        assert len([r for r in caplog.records if 'Controller malformed response' in r.getMessage()]) == 1
        wire.info = INFO; wire.state = 0; wire.now += 3
        services.rp._retry_at = 0  # Advance the real-client backoff without sleeping in this test.
        calls[0].write.reset_mock()
        services.status.refresh()
        sent = packets(calls)
        assert all(packet[0] in (1, 4) for packet in sent)
        assert services.recovery.ready
        assert wire.enabled == ([True]*3 + [False] + [True]*4 if failure == 'mismatch' else [True]*8)
    app.state.control.executor.submit(check).result()


def test_malformed_push_is_never_verified_or_replayed(calls, running_wire_backend):
    app, wire, _ = running_wire_backend
    def check():
        services = app.state.services
        wire.faults = [(p.Command.RDS_PUSH, b'\x80\0', b'')]
        calls[0].write.reset_mock()
        wire.now += 1
        services.rds.tick()
        assert [packet[0] for packet in packets(calls)] == [p.Command.GET_RDS_FIFO_FREE, p.Command.RDS_PUSH]
        assert not services.rds.suspended and services.recovery.ready
        assert services.rds.channels[0].needs_clear
        assert not any(c.needs_clear for c in services.rds.channels[1:])
        services.rds.tick()
        assert len(packets(calls)) == 2
        wire.now += .25; services.rp._retry_at = 0
        services.rds.next_poll = wire.now + 100  # Fenced recovery bypasses normal cadence.
        services.rds.tick()
        sent = packets(calls)
        clear = [i for i,x in enumerate(sent) if x[0] == p.Command.RDS_CLEAR]
        assert len(clear) == 1 and sent[clear[0]][1] == 0
        pushes = [i for i,x in enumerate(sent) if x[0] == p.Command.RDS_PUSH and x[1] == 0]
        assert pushes[0] < clear[0] < pushes[1]
        assert all(x[0] not in (2,3) for x in sent)
        assert wire.enabled == [True]*8
    app.state.control.executor.submit(check).result()


@pytest.mark.parametrize('pause_at', ['wait', 'header', 'payload'])
def test_complete_two_read_transaction_cannot_interleave(calls, monkeypatch, pause_at):
    import threading
    entered, release, attempted = threading.Event(), threading.Event(), threading.Event()
    rp = client(); rp.info = p.parse_info(INFO)
    def pause(stage):
        if pause_at == stage and packets(calls)[-1][0] == p.Command.GET_STATUS:
            entered.set()
            assert release.wait(2)
    def read(fd, size):
        if packets(calls)[-1][0] != p.Command.GET_STATUS:
            return b'\0\0' if calls[0].read.call_count % 2 else frame(b'\0\0')[2:]
        pause('header' if size == 2 else 'payload')
        return b'\0\x82' if size == 2 else frame(b'\0\x82' + status_payload())[2:]
    monkeypatch.setattr(linux.time, 'sleep', lambda seconds: pause('wait'))
    calls[0].read.side_effect = read
    def competing_write():
        attempted.set(); rp.set_tx_enabled(0, False)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(rp.get_status)
        try:
            assert entered.wait(2)
            second = executor.submit(competing_write)
            assert attempted.wait(2)
            assert not second.done() and len(packets(calls)) == 1
        finally:
            release.set()
        assert first.result()['uptime_ms'] == 123456
        second.result()
    assert [call.args[1] for call in calls[0].read.call_args_list] == [2, 132, 2, 2]


@pytest.mark.parametrize('stage', ['open', 'write', 'header', 'payload', 'decode'])
def test_failure_execution_metadata(calls, stage):
    rp = client(); rp.info = p.parse_info(INFO)
    command = p.Command.GET_STATUS if stage in ('payload', 'decode') else p.Command.SET_TX_ENABLED
    if stage == 'open': calls[0].open.side_effect = OSError('open failed')
    if stage == 'write': calls[0].write.side_effect = OSError('uncertain write')
    if stage == 'header': calls[0].read.side_effect = [b'\x80']
    if stage == 'payload': calls[0].read.side_effect = [b'\0\x82', b'\0']
    if stage == 'decode':
        data = bytearray(status_payload()); data[9] = 128
        calls[0].read.side_effect = reads(b'\0\x82', bytes(data))
    with pytest.raises((p.HardwareUnavailable, p.MalformedResponse)) as caught:
        rp.get_status() if command == p.Command.GET_STATUS else rp.set_tx_enabled(3, True)
    exc = caught.value
    assert (exc.command, exc.target, exc.stage) == (command, 255 if command == p.Command.GET_STATUS else 3, stage)
    assert exc.outcome == (p.Outcome.NOT_SENT if stage == 'open' else
                           p.Outcome.OBSERVATION if command == p.Command.GET_STATUS else p.Outcome.AMBIGUOUS)


def test_implicit_info_failure_does_not_mean_set_was_sent(calls):
    calls[0].read.side_effect = reads(b'\x80\0')
    rp = client()
    with pytest.raises(p.MalformedResponse) as caught:
        rp.set_tx_enabled(3, True)
    assert caught.value.command == p.Command.GET_INFO
    assert p.execution_outcome(caught.value, p.Command.SET_TX_ENABLED) == p.Outcome.NOT_SENT
    assert packets(calls) == [b'\x01\xff\0']
    assert p.execution_outcome(ValueError('encoding'), p.Command.SET_TX_CONFIG) == p.Outcome.NOT_SENT


@pytest.mark.parametrize('status', [p.Status.QN_ERROR, p.Status.INTERNAL_ERROR])
def test_explicit_rp_error_retains_identity_and_target(calls, status):
    rp = client(); rp.info = p.parse_info(INFO)
    calls[0].read.side_effect = reads(bytes((status, 0)))
    with pytest.raises(p.RpResponseError) as caught:
        rp.set_tx_enabled(3, True)
    assert caught.value.rp_status == status and caught.value.target == 3
    assert str(caught.value) == f'Controller SET_TX_ENABLED: {status.name}'
    assert caught.value.outcome == p.Outcome.RESPONSE and caught.value.stage == 'response'
    calls[0].close.assert_not_called()


@pytest.mark.parametrize('command', [p.Command.GET_INFO, p.Command.GET_STATUS, p.Command.GET_RDS_FIFO_FREE])
def test_repeated_os_read_failures_do_not_restore(calls, running_wire_backend, command):
    app, wire, _ = running_wire_backend
    def check():
        s = app.state.services
        original = calls[0].read.side_effect
        channels = list(s.rds.channels)
        last = s.status.snapshot()
        def fail(fd, size):
            if packets(calls)[-1][0] == command:
                raise OSError(121, 'NACK/timeout')
            return original(fd, size)
        calls[0].read.side_effect = fail
        calls[0].write.reset_mock()
        for _ in range(4):
            wire.now += .3; s.rp._retry_at = 0
            if command == p.Command.GET_INFO:
                s.rp.close()  # Session reopen must not mean hardware reset.
            if command == p.Command.GET_RDS_FIFO_FREE:
                s.rds.tick()
            else:
                s.status.refresh()
            n = len(packets(calls))
            s.rds.tick(); s.status.refresh()  # Same shared backoff: no bus storm.
            assert len(packets(calls)) == n
        assert s.recovery.ready and not s.rds.suspended and not s.recovery.pending
        assert all(a is b for a,b in zip(channels, s.rds.channels))
        stale = s.status.snapshot()
        assert stale.system.rp_observation_stale
        assert stale.system.rp_communication_state == 'UNAVAILABLE'
        assert stale.system.rp_observation_age_s >= 1
        assert [r.hardware_tx_state for r in stale.modulators] == ['ON']*8
        assert stale.system.rp_uptime_ms == last.system.rp_uptime_ms
        calls[0].read.side_effect = original
        wire.now += .3; s.rp._retry_at = 0
        s.status.refresh()
        assert not s.status.snapshot().system.rp_observation_stale
        assert all(packet[0] in (1,4,11) for packet in packets(calls))
        assert wire.enabled == [True]*8
    app.state.control.executor.submit(check).result()


@pytest.mark.parametrize('enabled', [False, True])
def test_ambiguous_enable_reapplies_only_same_target_and_state(calls, running_wire_backend, enabled):
    from fmheadendd.errors import DomainError
    app, wire, _ = running_wire_backend
    def check():
        s = app.state.services
        if enabled:
            s.modulators._stop(3)
        wire.faults = [(p.Command.SET_TX_ENABLED, b'\x80\0', b'')]
        with pytest.raises(DomainError):
            s.modulators.start(3) if enabled else s.modulators.stop(3)
        assert s.recovery.pending == {3: 'enable'}
        assert wire.enabled[3] == enabled  # Original uncertain write did execute.
        if enabled:
            count = len(packets(calls))
            with pytest.raises(DomainError): s.modulators.start(3)
            assert len(packets(calls)) == count  # No accidental OFF on repeated Start.
        calls[0].write.reset_mock()
        wire.now += .3; s.rp._retry_at = 0
        s.recovery.service()
        side_effects = [x for x in packets(calls) if x[0] != p.Command.GET_INFO]
        assert side_effects[0] == bytes((3,3,1,int(enabled)))
        assert all(x[1] == 3 and x[0] in (3,6) for x in side_effects)
        assert sum(x[0] == 3 for x in side_effects) == 1
        assert not s.recovery.pending
        assert all(on for i,on in enumerate(wire.enabled) if i != 3)
    app.state.control.executor.submit(check).result()


def test_ambiguous_clear_blocks_only_target_push_until_ack(calls, running_wire_backend):
    app, wire, _ = running_wire_backend
    def check():
        s = app.state.services
        s.rds.fence(3)
        wire.faults = [(p.Command.RDS_CLEAR, b'\x80\0', b'')]
        calls[0].write.reset_mock()
        wire.now += .1; s.rds.tick()
        assert s.rds.channels[3].needs_clear and not s.recovery.pending
        assert s.recovery.ready and not s.rds.suspended
        assert all(x[0] != 5 for x in packets(calls))
        wire.now += .25; s.rp._retry_at = 0
        s.rds.next_poll = wire.now + 100
        s.rds.tick()
        sent = packets(calls)
        clears = [i for i,x in enumerate(sent) if x[0] == 6]
        assert len(clears) == 2 and all(sent[i][1] == 3 for i in clears)
        assert not any(x[0] == 5 and x[1] == 3 for x in sent[:clears[1]])
        assert any(x[0] == 5 and x[1] == 3 for x in sent[clears[1]+1:])
        assert any(x[0] == 5 and x[1] != 3 for x in sent)
        assert all(x[0] not in (2,3) for x in sent) and wire.enabled == [True]*8
    app.state.control.executor.submit(check).result()


def test_ambiguous_config_reconciles_only_target(calls, running_wire_backend):
    from fmheadendd.errors import DomainError
    app, wire, _ = running_wire_backend
    def check():
        s = app.state.services; s.modulators._stop(3)
        wire.faults = [(p.Command.SET_TX_CONFIG, b'\x80\0', b'')]
        with pytest.raises(DomainError): s.modulators.start(3)
        assert s.recovery.pending == {3: 'off'}
        calls[0].write.reset_mock()
        wire.now += .3; s.rp._retry_at = 0
        s.recovery.service()
        commands = [x for x in packets(calls) if x[0] != p.Command.GET_INFO]
        assert [x[0] for x in commands] == [3,2,6,3]
        assert all(x[1] == 3 for x in commands)
        assert wire.enabled == [True]*8 and not s.recovery.pending
    app.state.control.executor.submit(check).result()


def test_stop_all_hold_overrides_pending_on_without_touching_other_tx(calls, running_wire_backend):
    from fmheadendd.errors import DomainError
    app, wire, _ = running_wire_backend
    def check():
        s = app.state.services; s.modulators._stop(3)
        wire.faults = [(p.Command.SET_TX_ENABLED, b'\x80\0', b'')]
        with pytest.raises(DomainError): s.modulators.start(3)
        assert s.recovery.pending == {3:'enable'}
        s.modulators.bulk_stopped.update(range(8))
        wire.now += .3; s.rp._retry_at = 0; calls[0].write.reset_mock()
        s.recovery.service()
        writes = [x for x in packets(calls) if x[0] not in (1,6)]
        assert writes == [bytes((3,3,1,0))]
        assert not wire.enabled[3] and not s.recovery.pending
    app.state.control.executor.submit(check).result()


@pytest.mark.parametrize('failure', ['info', 'validation', 'QN_ERROR', 'INTERNAL_ERROR'])
def test_unsent_and_explicit_config_errors_do_not_schedule_reconciliation(calls, running_wire_backend, failure):
    from fmheadendd.errors import DomainError
    app, wire, _ = running_wire_backend
    def check():
        s = app.state.services; s.modulators._stop(3)
        if failure == 'info':
            s.rp.close(); wire.faults = [(p.Command.GET_INFO, b'\x80\0', b'')]
        elif failure == 'validation':
            config = s.modulators.get(3).model_copy(update={'rf_attenuation_db': None})
            s.database.save('modulators', config, 3)
        else:
            wire.faults = [(p.Command.SET_TX_CONFIG, bytes((p.Status[failure],0)), b'')]
        calls[0].write.reset_mock()
        with pytest.raises(DomainError): s.modulators.start(3)
        assert not s.recovery.pending and s.recovery.ready
        if failure in ('info', 'validation'):
            assert all(x[0] == 1 for x in packets(calls))
        n = len(packets(calls)); wire.now += 1; s.rp._retry_at = 0
        s.recovery.service()
        assert len(packets(calls)) == n
    app.state.control.executor.submit(check).result()


def lifecycle_wait(monkeypatch, services, wire):
    """Advance only the lifecycle wait; normal wire command waits stay mocked."""
    from fmheadendd.modulators import manager
    waits = []
    def wait(delay):
        assert 0 < delay <= .25
        waits.append(delay)
        assert services.rp.retry_delay > 0 or services.recovery.retry_delay > 0
        wire.now += delay
        services.rp._retry_at = 0
        services.recovery.retry_at = wire.now
    monkeypatch.setattr(manager, 'time', SimpleNamespace(sleep=wait))
    return waits


def test_status_crc_failure_then_stop_reacquires_once(calls, running_wire_backend, monkeypatch):
    app, wire, _ = running_wire_backend
    s = app.state.services
    waits = lifecycle_wait(monkeypatch, s, wire)
    def fail_poll():
        wire.corrupt_crc = p.Command.GET_STATUS
        s.status.refresh()
        assert s.status.snapshot().system.rp_observation_stale
        calls[0].write.reset_mock()
    app.state.control.executor.submit(fail_poll).result()
    response = TestClient(app).post('/api/v1/modulators/3/stop')
    assert response.status_code == 200 and response.json()['tx_state'] == 'OFF'
    assert len(waits) == 1
    sent = packets(calls)
    assert sent[0][0] == p.Command.GET_INFO
    assert [x for x in sent if x[0] == 3] == [bytes((3,3,1,0))]
    assert all(x[0] in (1,3,6) and (x[0] == 1 or x[1] == 3) for x in sent)
    assert not s.recovery.pending and wire.enabled == [True]*3 + [False] + [True]*4
    # A later corrupt observation cannot change the completed HTTP result/OFF.
    wire.corrupt_crc = p.Command.GET_STATUS
    app.state.control.executor.submit(s.status.refresh).result()
    assert response.status_code == 200 and s.modulators.statuses[3].tx_state == 'OFF'
    assert s.status.snapshot().system.rp_observation_stale


@pytest.mark.parametrize('action', ['start', 'stop'])
@pytest.mark.parametrize('failures', [1, 2])
def test_lifecycle_implicit_info_retry_is_bounded(calls, running_wire_backend, monkeypatch, action, failures):
    app, wire, _ = running_wire_backend; s = app.state.services
    if action == 'start': app.state.control.executor.submit(s.modulators.stop, 3).result()
    waits = lifecycle_wait(monkeypatch, s, wire)
    s.rp.close()
    wire.faults = [(p.Command.GET_INFO, b'\x80\0', b'')] * failures
    calls[0].write.reset_mock()
    response = TestClient(app).post(f'/api/v1/modulators/3/{action}')
    assert response.status_code == (200 if failures == 1 else 503)
    assert len(waits) == 1
    sent = packets(calls)
    assert sum(x[0] == 1 for x in sent) == 2
    assert app.state.control.executor.submit(s.modulators.get, 3).result().enabled == (action == 'start')
    assert not s.recovery.pending
    if failures == 2:
        assert all(x[0] == 1 for x in sent)
        assert 'not sent' in response.json()['message']
        assert 'outcome unknown' not in response.json()['message']
    else:
        assert [x[0] for x in sent if x[0] != 1] == ([2,6,3] if action == 'start' else [3,6])
        assert all(x[0] == 1 or x[1] == 3 for x in sent)


@pytest.mark.parametrize('failure', ['crc', 'write', 'QN_ERROR', 'INTERNAL_ERROR', 'info_rejection'])
def test_lifecycle_never_retries_uncertain_or_rejected_set(calls, running_wire_backend, monkeypatch, failure):
    app, wire, _ = running_wire_backend; s = app.state.services
    waits = lifecycle_wait(monkeypatch, s, wire)
    if failure == 'crc': wire.corrupt_crc = p.Command.SET_TX_ENABLED
    elif failure == 'write': calls[0].write.side_effect = OSError('write may have begun')
    elif failure == 'info_rejection':
        s.rp.close(); wire.faults = [(p.Command.GET_INFO, b'\x06\0', b'')]
    else: wire.faults = [(p.Command.SET_TX_ENABLED, bytes((p.Status[failure],0)), b'')]
    calls[0].write.reset_mock()
    response = TestClient(app).post('/api/v1/modulators/3/stop')
    assert response.status_code == 503 and not waits
    assert len(packets(calls)) == 1
    ambiguous = failure in ('crc', 'write')
    assert s.recovery.pending == ({3: 'enable'} if ambiguous else {})
    if ambiguous: assert 'outcome unknown (AMBIGUOUS)' in response.json()['message']
    if failure == 'info_rejection':
        assert 'not sent' in response.json()['message']
        assert s.modulators.statuses[3].tx_state == 'ON'
    assert not app.state.control.executor.submit(s.modulators.get, 3).result().enabled
    calls[0].write.side_effect = lambda fd, packet: len(packet)


@pytest.mark.parametrize('start', [False, True])
def test_bulk_unavailable_has_one_shared_reacquisition(calls, running_wire_backend, monkeypatch, start):
    app, wire, _ = running_wire_backend; s = app.state.services
    if start:
        app.state.control.executor.submit(s.modulators.all, False).result()
    waits = lifecycle_wait(monkeypatch, s, wire)
    s.rp.close()
    calls[0].open.reset_mock()
    calls[0].open.side_effect = OSError('device unavailable')
    calls[0].write.reset_mock()
    results = app.state.control.executor.submit(s.modulators.all, start).result()
    assert len(results) == 8 and all(r['error'] == 'DEPENDENCY_UNAVAILABLE' for r in results)
    assert len(waits) == 1 and calls[0].open.call_count == 2
    assert not packets(calls) and not s.recovery.pending


def test_reacquired_session_does_not_retry_ambiguous_set(calls, running_wire_backend, monkeypatch):
    app, wire, _ = running_wire_backend; s = app.state.services
    waits = lifecycle_wait(monkeypatch, s, wire)
    s.rp.close()
    wire.faults = [(p.Command.GET_INFO, b'\x80\0', b'')]
    wire.corrupt_crc = p.Command.SET_TX_ENABLED
    calls[0].write.reset_mock()
    response = TestClient(app).post('/api/v1/modulators/3/stop')
    assert response.status_code == 503 and len(waits) == 1
    assert [x[0] for x in packets(calls)] == [1,1,3]
    assert 'AMBIGUOUS' in response.json()['message']
    assert s.recovery.pending == {3: 'enable'}


def test_transient_device_open_failure_retries_only_unsent_stop(calls, running_wire_backend, monkeypatch):
    app, wire, _ = running_wire_backend; s = app.state.services
    waits = lifecycle_wait(monkeypatch, s, wire)
    s.rp.close()
    calls[0].open.side_effect = [OSError('transient open failure'), 42]
    calls[0].write.reset_mock()
    response = TestClient(app).post('/api/v1/modulators/3/stop')
    assert response.status_code == 200 and len(waits) == 1
    assert [x[0] for x in packets(calls)] == [1,3,6]
    assert not s.recovery.pending


def test_confirmed_off_survives_ambiguous_rds_clear(calls, running_wire_backend):
    app, wire, _ = running_wire_backend; s = app.state.services
    wire.corrupt_crc = p.Command.RDS_CLEAR
    calls[0].write.reset_mock()
    response = TestClient(app).post('/api/v1/modulators/3/stop')
    assert response.status_code == 200 and response.json()['tx_state'] == 'OFF'
    assert response.json()['rds_last_error'] and s.rds.channels[3].needs_clear
    assert not s.recovery.pending and not wire.enabled[3]
    assert [x[0] for x in packets(calls)] == [3,6]
    assert all(x[1] == 3 for x in packets(calls))


def test_card_diagnostics_preserve_global_session_and_target_error_scope(calls, running_wire_backend):
    app, wire, _ = running_wire_backend; s = app.state.services
    def check():
        s.rp.close()
        calls[0].open.side_effect = OSError('shared control unavailable')
        s.modulators.all(False)
        rows = s.status.snapshot().modulators
        assert all(r.last_error and r.last_error_scope == 'GLOBAL' for r in rows)
        assert wire.enabled == [True]*8
        calls[0].open.side_effect = None
        s.rp._retry_at = 0
        wire.faults = [(p.Command.SET_TX_ENABLED, b'\x05\0', b'')]
        from fmheadendd.errors import DomainError
        with pytest.raises(DomainError): s.modulators.stop(3)
        rows = s.status.snapshot().modulators
        assert rows[3].last_error_scope == 'TX' and 'QN_ERROR' in rows[3].last_error
        assert all(r.last_error_scope == 'GLOBAL' for r in rows if r.id != 3)
        error = p.MalformedResponse(p.Command.GET_INFO, 'global handshake error')
        s.rds._error(2, error)
        assert s.modulators.statuses[2].rds_last_error_scope == 'GLOBAL'
    app.state.control.executor.submit(check).result()
