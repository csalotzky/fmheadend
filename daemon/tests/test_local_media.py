"""Resolver/queue core tests: no GStreamer, database, network or playback."""
from collections import Counter
from pathlib import Path
import random
from unittest.mock import patch

import pytest

from fmheadendd.errors import DomainError
from fmheadendd.sources.local_media import (
    AUDIO_EXTENSIONS, FolderResolver, LocalMediaConfig, LocalMediaMode, MediaItem,
    M3uResolver, PlaybackOrder, PlaybackQueue, QueueBuilder, resolve_media,
)


def touch(root, *names):
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()


def relative(items, root):
    return [item.locator.relative_to(root).as_posix() for item in items]


def test_folder_direct_and_recursive_natural_order(tmp_path):
    touch(tmp_path, '10 Outro.mp3', '2 Main.MP3', '1 Intro.mp3', 'readme.txt',
          '.hidden.mp3', '.Trash/hidden.mp3', 'Album10/1.flac', 'Album2/10.flac',
          'Album2/2.flac', 'Album2/.ignored/1.mp3', 'Album2/.file.flac')
    resolver = FolderResolver()
    assert relative(resolver.resolve(tmp_path), tmp_path) == ['1 Intro.mp3', '2 Main.MP3', '10 Outro.mp3']
    expected = ['1 Intro.mp3', '2 Main.MP3', '10 Outro.mp3', 'Album2/2.flac',
                'Album2/10.flac', 'Album10/1.flac']
    assert relative(resolver.resolve(tmp_path, recursive=True), tmp_path) == expected
    assert relative(resolver.resolve(tmp_path, recursive=True), tmp_path) == expected


@pytest.mark.parametrize('extension', sorted(AUDIO_EXTENSIONS))
def test_folder_extension_candidates_are_case_insensitive_not_decoded(tmp_path, extension):
    touch(tmp_path, 'candidate'+extension.upper(), 'other.txt', 'playlist.m3u', 'movie.mp4')
    assert relative(FolderResolver().resolve(tmp_path), tmp_path) == ['candidate'+extension.upper()]


def test_natural_sort_ties_do_not_depend_on_enumeration(tmp_path):
    touch(tmp_path, 'a2.mp3', 'a02.mp3', 'a10.mp3')
    import os
    entries = list(os.scandir(tmp_path))
    class Scan:
        def __init__(self, values):
            self.values = values
        def __enter__(self):
            return iter(self.values)
        def __exit__(self, *args):
            pass
    with patch('fmheadendd.sources.local_media.os.scandir', return_value=Scan(entries)):
        first = FolderResolver().resolve(tmp_path)
    with patch('fmheadendd.sources.local_media.os.scandir', return_value=Scan(entries[::-1])):
        assert FolderResolver().resolve(tmp_path) == first
    assert relative(first, tmp_path) == ['a02.mp3', 'a2.mp3', 'a10.mp3']


def symlink(link, target, *, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as exc:
        if getattr(exc, 'winerror', None) == 1314:
            pytest.skip('Windows requires symlink privilege / Developer Mode (WinError 1314)')
        raise


def test_folder_symlink_policy(tmp_path):
    root = tmp_path / 'real'
    touch(root, 'track.mp3', 'sub/other.flac')
    symlink(root / 'loop', root, directory=True)
    symlink(root / 'alias.mp3', root / 'track.mp3')
    symlink(root / 'broken.mp3', root / 'missing.mp3')
    symlink(tmp_path / 'top', root, directory=True)
    top = tmp_path / 'top'
    config = LocalMediaConfig(LocalMediaMode.FOLDER, top, recursive=True)
    assert relative(resolve_media(config), top) == ['alias.mp3', 'sub/other.flac', 'track.mp3']
    assert config.path == top  # Not rewritten to realpath.


@pytest.mark.parametrize('resolver,code', [(FolderResolver(), 'FOLDER_UNREADABLE'),
                                          (M3uResolver(), 'PLAYLIST_UNREADABLE')])
def test_missing_resolver_path(tmp_path, resolver, code):
    with pytest.raises(DomainError) as error:
        resolver.resolve(tmp_path / 'missing')
    assert error.value.error == code


def test_unreadable_folder_and_subfolder_are_not_silent_partial_scans(tmp_path):
    touch(tmp_path, 'valid.mp3', 'sub/other.mp3')
    import os
    scan = os.scandir
    def denied(path):
        if Path(path).name == 'sub':
            raise PermissionError('denied')
        return scan(path)
    with patch('fmheadendd.sources.local_media.os.scandir', side_effect=denied):
        with pytest.raises(DomainError, match='denied') as error:
            FolderResolver().resolve(tmp_path, recursive=True)
    assert error.value.error == 'FOLDER_UNREADABLE'


def test_empty_folder(tmp_path):
    touch(tmp_path, '.hidden.mp3', 'notes.txt')
    with pytest.raises(DomainError) as error:
        FolderResolver().resolve(tmp_path)
    assert error.value.error == 'NO_PLAYABLE_TRACKS'


@pytest.mark.parametrize('suffix', ['.m3u', '.m3u8'])
@pytest.mark.parametrize('bom', [False, True])
def test_playlist_entries_order_duplicates_unicode_bom(tmp_path, suffix, bom):
    playlist = tmp_path / ('playlist'+suffix)
    absolute = tmp_path / 'absolute.flac'
    lines = ['#EXTM3U', '', '  ', '#EXTINF:123,Not metadata', 'relative.mp3',
             str(absolute), 'https://example.org/audio?id=2', 'http://example.org/live',
             '#UNKNOWN directive', 'relative.mp3', 'zene/árvíz.ogg', '../parent.wav']
    playlist.write_text('\n'.join(lines), encoding='utf-8-sig' if bom else 'utf-8')
    items = resolve_media(LocalMediaConfig('playlist', playlist))
    assert [item.locator for item in items] == [tmp_path / 'relative.mp3', absolute,
        'https://example.org/audio?id=2', 'http://example.org/live', tmp_path / 'relative.mp3',
        tmp_path / 'zene/árvíz.ogg', tmp_path / '../parent.wav']
    assert items[0] == items[4] and len(items) == 7


def test_empty_playlist(tmp_path):
    playlist = tmp_path / 'empty.m3u8'
    playlist.write_text('#EXTM3U\n#EXTINF:0,ignore\n\n', encoding='utf-8')
    with pytest.raises(DomainError) as error:
        M3uResolver().resolve(playlist)
    assert error.value.error == 'NO_PLAYABLE_TRACKS'


def test_unreadable_playlist(tmp_path):
    with patch.object(Path, 'open', side_effect=PermissionError('denied')):
        with pytest.raises(DomainError) as error:
            M3uResolver().resolve(tmp_path / 'list.m3u')
    assert error.value.error == 'PLAYLIST_UNREADABLE'


def test_invalid_utf8_is_explicit(tmp_path):
    playlist = tmp_path / 'list.m3u8'
    playlist.write_bytes(b'\xffbad.mp3')
    with pytest.raises(DomainError) as error:
        M3uResolver().resolve(playlist)
    assert error.value.error == 'PLAYLIST_UNREADABLE'


@pytest.mark.parametrize('entry', ['ftp://example.org/track', 'file:///music/track', 'https:///missing-host'])
def test_unsupported_playlist_uris_are_explicit(tmp_path, entry):
    playlist = tmp_path / 'list.m3u'
    playlist.write_text(entry, encoding='utf-8')
    with pytest.raises(DomainError) as error:
        M3uResolver().resolve(playlist)
    assert error.value.error == 'INVALID_PLAYLIST_ENTRY'


def test_config_is_internal_and_does_not_require_existing_path(tmp_path):
    config = LocalMediaConfig('folder', tmp_path / 'offline', order='random')
    assert config.mode is LocalMediaMode.FOLDER
    assert config.order is PlaybackOrder.RANDOM
    with pytest.raises(ValueError, match='absolute'):
        LocalMediaConfig('folder', Path('relative'))
    with pytest.raises(ValueError):
        LocalMediaConfig('podcast', tmp_path)
    with pytest.raises(ValueError):
        LocalMediaConfig('folder', tmp_path, recursive='false')
    with pytest.raises(ValueError):
        LocalMediaConfig('folder', tmp_path, order='invalid')


def media(*names):
    return [MediaItem('https://example.org/'+name) for name in names]


def consume(queue):
    items = []
    while (item := queue.next_item()) is not None:
        items.append(item)
    assert queue.exhausted
    return items


def test_sequential_cycle_duplicates_and_rebuild():
    items = media('a', 'b', 'a')
    queue = PlaybackQueue(items)
    items.clear()  # Queue owns its candidate snapshot.
    assert consume(queue) == media('a', 'b', 'a')
    assert queue.next_item() is None
    queue.rebuild()
    assert consume(queue) == media('a', 'b', 'a')
    queue.rebuild(media('new', 'second'))
    assert consume(queue) == media('new', 'second')
    queue.rebuild([])
    assert queue.exhausted and queue.next_item() is None


def test_shuffle_bag_and_seed_seam():
    items = media('a', 'b', 'c', 'a', 'd')
    queues = [PlaybackQueue(items, 'random', rng=random.Random(42)) for _ in range(2)]
    previous = None
    for _ in range(4):
        cycles = [consume(queue) for queue in queues]
        assert cycles[0] == cycles[1]
        assert Counter(cycles[0]) == Counter(items)
        assert cycles[0][0] != previous
        previous = cycles[0][-1]
        for queue in queues:
            queue.rebuild()
    assert items == media('a', 'b', 'c', 'a', 'd')


class FixedShuffle(random.Random):
    """Force boundary collision without probabilistic expectations."""
    def __init__(self):
        super().__init__(0)
        self.calls = 0
    def shuffle(self, items):
        self.calls += 1
    def choice(self, items):
        return items[-1]


def test_boundary_repeat_avoidance_with_duplicate_candidates():
    rng = FixedShuffle()
    queue = PlaybackQueue(media('a', 'b', 'a'), 'random', rng=rng)
    assert consume(queue) == media('a', 'b', 'a')
    queue.rebuild()
    assert consume(queue) == media('b', 'a', 'a')
    assert rng.calls == 2
    queue.rebuild(media('a', 'new'))
    assert consume(queue) == media('new', 'a')


@pytest.mark.parametrize('names', [(), ('a',), ('a', 'a'), ('a', 'b')])
@pytest.mark.parametrize('order', list(PlaybackOrder))
def test_tiny_lists(names, order):
    items = media(*names)
    queue = PlaybackQueue(items, order, rng=FixedShuffle())
    first = consume(queue)
    queue.rebuild()
    second = consume(queue)
    assert Counter(first) == Counter(second) == Counter(items)
    if order == PlaybackOrder.RANDOM and len(set(items)) > 1:
        assert first[-1] != second[0]


def test_builder_and_early_rebuild_have_no_filesystem_dependency():
    items = media('a', 'b', 'c')
    assert QueueBuilder().build(items) == items
    with patch('fmheadendd.sources.local_media.os.scandir', side_effect=AssertionError('scan')):
        queue = PlaybackQueue(items)
        assert queue.next_item() == items[0]
        queue.rebuild(media('new'))
        assert consume(queue) == media('new')

@pytest.mark.parametrize('bom', [False, True])
def test_vlc_legacy_comments_do_not_require_utf8(tmp_path, bom):
    playlist = tmp_path / 'vlc.m3u'
    metadata = '#EXTINF:199,P\u00e9terfy Bori & Love Band - Sz\u00e1z f\u00e9ny\u00e9v'.encode('latin-1')
    entry = b'ZENE/2025%2011%20November/P%C3%A9terfy%20Bori%20-%20Sz%C3%A1z%20f%C3%A9ny%C3%A9v.mp3'
    data = b'#EXTM3U\r\n' + metadata + b'\r\n  #UNKNOWN:\xff\r\n' + entry + b'\r\n'
    # A broken comment with a concatenated path remains entirely ignored.
    data += metadata + b'broken/path.mp3\r\n' + entry + b'\r\n'
    with pytest.raises(UnicodeDecodeError): data.decode('utf-8')
    playlist.write_bytes((b'\xef\xbb\xbf' if bom else b'') + data)
    expected = tmp_path / 'ZENE/2025 11 November/P\u00e9terfy Bori - Sz\u00e1z f\u00e9ny\u00e9v.mp3'
    assert [i.locator for i in M3uResolver().resolve(playlist)] == [expected, expected]


def test_vlc_m3u8_percent_encoded_paths_are_real_files(tmp_path):
    playlist = tmp_path / 'vlc.m3u8'
    path = 'ZENE/2026/2025 11 November/Detto - Random.mp3'
    touch(tmp_path, path)
    playlist.write_text('#EXTM3U\nZENE/2026/2025%2011%20November/Detto%20-%20Random.mp3\n', encoding='utf-8-sig')
    resolved = M3uResolver().resolve(playlist)[0].locator
    assert resolved == tmp_path / path
    assert resolved.is_file()
    assert '2025%2011%20November/Detto%20-%20Random.mp3' in resolved.as_uri()


@pytest.mark.parametrize('entry,decoded', [
    ('P%C3%A9terfy/J%C3%BAnius/Elef%C3%A1nt.mp3', 'P\u00e9terfy/J\u00fanius/Elef\u00e1nt.mp3'),
    ('a+b%20%2520%23.mp3', 'a+b %20#.mp3'),
])
@pytest.mark.parametrize('absolute', [False, True])
def test_local_percent_decoding_once_preserves_plus_and_absolute_paths(tmp_path, entry, decoded, absolute):
    playlist = tmp_path / 'vlc.m3u8'
    playlist.write_text(str(tmp_path / entry) if absolute else entry, encoding='utf-8')
    assert M3uResolver().resolve(playlist)[0].locator == tmp_path / decoded


@pytest.mark.parametrize('scheme', ['http', 'https'])
def test_percent_encoded_remote_uri_is_unchanged(tmp_path, scheme):
    uri = f'{scheme}://example.org/audio/My%20Programme.mp3?token=a%2Fb+c#part%201'
    playlist = tmp_path / 'remote.m3u'
    playlist.write_text(uri+'\n'+uri, encoding='utf-8')
    assert [item.locator for item in M3uResolver().resolve(playlist)] == [uri, uri]


@pytest.mark.parametrize('suffix,data', [('.m3u8', b'#EXTINF:\xff\nvalid.mp3'), ('.m3u', b'\xffbad.mp3')])
def test_actual_entries_and_m3u8_still_require_utf8(tmp_path, suffix, data):
    playlist = tmp_path / ('bad'+suffix)
    playlist.write_bytes(data)
    with pytest.raises(DomainError) as error:
        M3uResolver().resolve(playlist)
    assert error.value.error == 'PLAYLIST_UNREADABLE'

@pytest.mark.parametrize('entry', ['bad%FF.mp3', 'bad%00.mp3'])
def test_invalid_decoded_local_path_is_explicit(tmp_path, entry):
    playlist = tmp_path / 'bad.m3u8'
    playlist.write_text(entry, encoding='utf-8')
    with pytest.raises(DomainError) as error:
        M3uResolver().resolve(playlist)
    assert error.value.error == 'INVALID_PLAYLIST_ENTRY'
