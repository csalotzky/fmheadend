"""Notification invalidation and fresh-on-demand fallback; no timer or worker."""
import json
import errno
import os
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from fmheadendd.profile_changes import ProfileChanges
from fmheadendd.audio_profile_files import AudioProfileFiles
from fmheadendd.tests.test_audio_profile_files import store, document
from fmheadendd.errors import DomainError


@pytest.mark.parametrize('wd,mask,name,changed', [
    (1, 2, 'station.json', True), (1, 4, 'station.json', True),
    (1, 8, 'station.json', True), (1, 0x80, 'station.json', True),
    (1, 0x100, 'station.json', True), (1, 0x200, 'station.json', True),
    (1, 0x800, '', True), (-1, 0x4000, '', True), (1, 0x8000, '', True),
    (2, 0x80, 'custom', True), (2, 0x80, 'unrelated', False),
    (1, 2, '.editor.tmp', False),
])
def test_notification_filter_and_rearm(monkeypatch, wd, mask, name, changed):
    watch = ProfileChanges.__new__(ProfileChanges)
    watch.fd, watch.watches = 42, {1: {None}, 2: {'custom'}}
    watch._arm = Mock()
    name = os.fsencode(name) + b'\0'
    event = watch.HEADER.pack(wd, mask, 0, len(name)) + name
    io = SimpleNamespace(read=Mock(side_effect=[event, BlockingIOError()]),
                         close=Mock(), fsdecode=os.fsdecode)
    monkeypatch.setattr('fmheadendd.profile_changes.os', io)
    assert watch.changed() is changed
    assert watch._arm.call_count == int(changed)
    watch.close()
    io.close.assert_called_once_with(42)
    assert watch.changed()  # No watcher: never trust cached contents.


def test_monitor_failure_falls_back_to_fresh_reads(monkeypatch):
    watch = ProfileChanges.__new__(ProfileChanges)
    watch.fd, watch.watches = 42, {}
    io = SimpleNamespace(read=Mock(side_effect=OSError('watch failed')), close=Mock())
    monkeypatch.setattr('fmheadendd.profile_changes.os', io)
    assert watch.changed()
    assert watch.fd is None and watch.changed()
    io.close.assert_called_once_with(42)


@pytest.mark.parametrize('failure', [None, 'init', 'watch'])
def test_native_setup_watches_ancestors_and_closes_failed_descriptors(tmp_path, monkeypatch, failure):
    roots = (tmp_path / 'default', tmp_path / 'custom')
    libc = Mock()
    libc.inotify_init1.return_value = -1 if failure == 'init' else 42
    libc.inotify_add_watch.side_effect = None if failure == 'watch' else range(1, 100)
    libc.inotify_add_watch.return_value = -1
    io = SimpleNamespace(O_NONBLOCK=2048, O_CLOEXEC=524288,
                         fsencode=os.fsencode, close=Mock())
    monkeypatch.setattr('fmheadendd.profile_changes.sys', SimpleNamespace(platform='linux'))
    monkeypatch.setattr('fmheadendd.profile_changes.os', io)
    monkeypatch.setattr('fmheadendd.profile_changes.ctypes.CDLL', Mock(return_value=libc))
    monkeypatch.setattr('fmheadendd.profile_changes.ctypes.get_errno', lambda: errno.EACCES)
    watch = ProfileChanges(roots)
    if failure:
        assert watch.fd is None
        assert watch.changed()
    else:
        assert watch.fd == 42
        watched = {call.args[1] for call in libc.inotify_add_watch.call_args_list}
        assert watched == {os.fsencode(p) for root in roots for p in (root, *root.parents)}
    watch.close()
    assert io.close.call_count == (0 if failure == 'init' else 1)


def test_notification_drain_is_bounded(monkeypatch):
    watch = ProfileChanges.__new__(ProfileChanges)
    watch.fd, watch.watches = 42, {1: {None}}
    watch._arm = Mock()
    event = watch.HEADER.pack(1, 2, 0, 4) + b'tmp\0'
    io = SimpleNamespace(read=Mock(return_value=event), close=Mock(), fsdecode=os.fsdecode)
    monkeypatch.setattr('fmheadendd.profile_changes.os', io)
    assert watch.changed()
    assert io.read.call_count == 32
    watch._arm.assert_called_once()
    watch.close()


def test_unchanged_inventory_uses_no_filesystem_or_validation(store):
    watch = Mock(changed=Mock(return_value=False))
    with patch('fmheadendd.audio_profile_files.ProfileChanges', return_value=watch):
        rows = store.list()
        rows[0].name = 'Caller mutation'
        store.is_system_default = lambda path: True
        with patch.object(store, '_read_list', side_effect=AssertionError('scan')), \
                patch.object(store, 'get', side_effect=AssertionError('read')), \
                patch.object(store, 'validate', side_effect=AssertionError('validate')):
            for _ in range(20):
                fresh = store.list()
                assert fresh[0].name != 'Caller mutation'
                assert fresh[0].is_system_default
        store.close()
        watch.close.assert_called_once()


def test_api_writes_invalidate_without_waiting_for_notifications(store):
    with patch('fmheadendd.audio_profile_files.ProfileChanges', return_value=Mock(changed=lambda: False)):
        assert len(store.list()) == 1
        created = store.create(document() | {'name': 'Created'})
        assert len(store.list()) == 2
        store.save(created.path, document() | {'name': 'Renamed'})
        assert next(row for row in store.list() if row.path == created.path).name == 'Renamed'
        clone = store.clone(created.path, 'Clone')
        assert len(store.list()) == 3
        store.delete(clone.path)
        assert len(store.list()) == 2


@pytest.mark.parametrize('notifications', [False, True])
def test_external_changes_errors_and_restarts(store, notifications):
    # Model event delivery; native Linux coverage below exercises the actual fd.
    watch = Mock(changed=Mock(return_value=not notifications))
    def changed():
        watch.changed.return_value = True
        try:
            return store.list()
        finally:
            watch.changed.return_value = not notifications
    with patch('fmheadendd.audio_profile_files.ProfileChanges', return_value=watch):
        store.list()
        path = store.roots['default'] / 'test_filters.json'
        path.write_text(json.dumps(document() | {'name': 'Updated default'}), encoding='utf-8')
        assert changed()[0].name == 'Updated default'
        custom = store.roots['custom']
        custom.mkdir()
        (custom / 'external.json').write_text(json.dumps(document()), encoding='utf-8')
        assert len(changed()) == 2
        (custom / 'external.json').rename(custom / 'renamed.json')
        assert changed()[1].path == 'custom/renamed.json'
        (custom / 'renamed.json').write_text('{broken', encoding='utf-8')
        assert len(changed()) == 1
        (custom / 'renamed.json').write_text(json.dumps(document()), encoding='utf-8')
        assert len(changed()) == 2
        (custom / 'renamed.json').unlink()
        assert len(changed()) == 1
        with patch.object(store, 'get', side_effect=DomainError(503, 'PROFILE_STORAGE_ERROR', 'Denied')):
            with pytest.raises(DomainError, match='Denied'):
                changed()
        assert len(store.list()) == 1  # Failed scans aren't published as an empty list.
        store.close()
        assert store.list()[0].name == 'Updated default'


@pytest.mark.skipif(sys.platform != 'linux', reason='requires Linux inotify')
def test_native_notifications_cover_directory_replacement_and_permissions(store):
    try:
        store.list()
        assert store._changes.fd is not None
        with patch.object(store, '_read_list', wraps=store._read_list) as scan:
            for _ in range(10):
                store.list()
            scan.assert_not_called()
        # Missing custom root, editor replace, rename, chmod and root replacement.
        custom = store.roots['custom']
        custom.mkdir()
        path = custom / 'external.json'
        path.write_text(json.dumps(document()), encoding='utf-8')
        assert len(store.list()) == 2
        temporary = custom / '.editor.tmp'
        temporary.write_text(json.dumps(document() | {'name': 'External'}), encoding='utf-8')
        temporary.replace(path)
        assert store.list()[1].name == 'External'
        path.rename(custom / 'renamed.json')
        assert store.list()[1].path == 'custom/renamed.json'
        with patch.object(store, '_read_list', wraps=store._read_list) as scan:
            (custom / 'renamed.json').chmod(0o600)
            store.list()
            scan.assert_called_once()
        custom.rename(custom.with_name('old-custom'))
        custom.mkdir()
        assert len(store.list()) == 1
        defaults = store.roots['default']
        defaults.rename(defaults.with_name('old-default'))
        with pytest.raises(DomainError):
            store.list()
        defaults.mkdir()
        (defaults / 'new.json').write_text(json.dumps(document()), encoding='utf-8')
        assert store.list()[0].path == 'default/new.json'
    finally:
        store.close()
