"""Test the actual native symbol boundary, including a locally loaded client."""
import ctypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from fmheadendd.errors import DependencyUnavailable
from fmheadendd.sources import lv2_lifecycle


def test_guard_installs_before_gstreamer_initialization(monkeypatch):
    from fmheadendd.sources.gst_pipeline import load_gst
    events = []
    gst = SimpleNamespace(init=lambda _: events.append('Gst.init'))
    monkeypatch.setitem(sys.modules, 'gi', SimpleNamespace(require_version=lambda *args: None))
    monkeypatch.setitem(sys.modules, 'gi.repository', SimpleNamespace(Gst=gst))
    monkeypatch.setattr(lv2_lifecycle, 'ensure_lv2_lifecycle', lambda: events.append('guard'))
    assert load_gst() is gst
    assert events == ['guard', 'Gst.init']


def test_guard_load_is_once_and_global(monkeypatch):
    calls = []
    def function():
        return None
    library = SimpleNamespace(fmheadend_lv2_guard_install=function)
    monkeypatch.setattr(lv2_lifecycle, '_library', None)
    monkeypatch.setattr(lv2_lifecycle.sys, 'platform', 'linux')
    monkeypatch.setattr(lv2_lifecycle.ctypes, 'CDLL', lambda path, mode: calls.append((path, mode)) or library)
    lv2_lifecycle.ensure_lv2_lifecycle()
    lv2_lifecycle.ensure_lv2_lifecycle()
    assert len(calls) == 1 and calls[0][1] == ctypes.RTLD_GLOBAL
    assert lv2_lifecycle._library is library


@pytest.mark.parametrize('error', [OSError('missing binary'), b'Lilv already loaded'])
def test_guard_fails_closed(monkeypatch, error):
    def load(*args, **kwargs):
        if isinstance(error, Exception):
            raise error
        def install():
            return error
        return SimpleNamespace(fmheadend_lv2_guard_install=install)
    monkeypatch.setattr(lv2_lifecycle, '_library', None)
    monkeypatch.setattr(lv2_lifecycle.sys, 'platform', 'linux')
    monkeypatch.setattr(lv2_lifecycle.ctypes, 'CDLL', load)
    with pytest.raises(DependencyUnavailable, match='fresh process'):
        lv2_lifecycle.ensure_lv2_lifecycle()
    assert lv2_lifecycle._library is None


@pytest.mark.skipif(sys.platform != 'linux' or not shutil.which('cc'), reason='Linux ELF/C compiler required')
def test_native_lifecycle_serializes_local_plugin_dependency(tmp_path):
    # A fake Lilv detects concurrent library-cache users. The client mimics
    # gst-lv2's RTLD_LOCAL dependency, rather than calling the guard directly.
    fake = tmp_path / 'fake.c'
    fake.write_text('''
        #include <stdatomic.h>
        #include <unistd.h>
        static atomic_int active, overlaps, calls;
        static void operation(void) {
            if (atomic_fetch_add(&active, 1)) atomic_fetch_add(&overlaps, 1);
            usleep(2000);
            atomic_fetch_add(&calls, 1);
            atomic_fetch_sub(&active, 1);
        }
        void *lilv_plugin_instantiate(const void *p, double r, const void *f) {
            operation(); return (void *)1;
        }
        void lilv_instance_free(void *p) { operation(); }
        int overlap_count(void) { return overlaps; }
        int call_count(void) { return calls; }
    ''')
    client = tmp_path / 'client.c'
    client.write_text('''
        extern void *lilv_plugin_instantiate(const void *, double, const void *);
        extern void lilv_instance_free(void *);
        void cycle(void) {
            void *p = lilv_plugin_instantiate(0, 48000, 0);
            lilv_instance_free(p);
        }
    ''')
    guard = Path(lv2_lifecycle.__file__).with_suffix('.c')
    commands = [
        ['cc', '-shared', '-fPIC', '-o', str(tmp_path / 'liblilv-0.so.0'), str(fake), '-Wl,-soname,liblilv-0.so.0'],
        ['cc', '-shared', '-fPIC', '-o', str(tmp_path / 'client.so'), str(client), '-L' + str(tmp_path), '-l:liblilv-0.so.0'],
        ['cc', '-shared', '-fPIC', '-o', str(tmp_path / 'guard.so'), str(guard), '-ldl', '-pthread', '-Wl,-Bsymbolic-functions'],
    ]
    for command in commands:
        subprocess.run(command, check=True, capture_output=True)
    program = '''
import ctypes, json, sys, threading
from concurrent.futures import ThreadPoolExecutor
mode = sys.argv[1]
if mode == 'late':
    client = ctypes.CDLL('./client.so')
if mode != 'unguarded':
    guard = ctypes.CDLL('./guard.so', mode=ctypes.RTLD_GLOBAL)
    guard.fmheadend_lv2_guard_install.restype = ctypes.c_char_p
    error = guard.fmheadend_lv2_guard_install()
    if mode == 'late':
        assert error and b'already loaded' in error
        sys.exit(0)
    assert error is None, error
client = ctypes.CDLL('./client.so')
real = ctypes.CDLL('liblilv-0.so.0')
barrier = threading.Barrier(8)
def run(_):
    barrier.wait()
    for i in range(10): client.cycle()
with ThreadPoolExecutor(max_workers=8) as pool: list(pool.map(run, range(8)))
print(json.dumps([real.overlap_count(), real.call_count()]))
'''
    env = dict(os.environ, LD_LIBRARY_PATH=str(tmp_path))
    for mode in ('unguarded', 'guarded', 'late'):
        result = subprocess.run([sys.executable, '-c', program, mode], cwd=tmp_path,
                                env=env, check=True, text=True, capture_output=True, timeout=20)
        if mode != 'late':
            overlaps, calls = json.loads(result.stdout)
            assert calls == 160
            assert overlaps == 0 if mode == 'guarded' else overlaps > 0
