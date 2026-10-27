"""Linux directory notifications, drained on demand; no worker or polling scan."""
import ctypes
import errno
import os
import struct
import sys


class ProfileChanges:
    # Writes, permissions, atomic replacement, creation, deletion and watch loss.
    MASK = (0x00000002 | 0x00000004 | 0x00000008 |  # modify, attributes, close-write
            0x00000040 | 0x00000080 | 0x00000100 |  # move-from, move-to, create
            0x00000200 | 0x00000400 | 0x00000800)   # delete, delete-self, move-self
    LOST = 0x00002000 | 0x00004000 | 0x00008000  # unmount, overflow, ignored
    HEADER = struct.Struct('iIII')

    def __init__(self, roots):
        self.roots = tuple(roots)
        self.fd = None
        self.watches = {}
        self.libc = None
        if sys.platform == 'linux':
            try:
                self.libc = ctypes.CDLL(None, use_errno=True)
                self.libc.inotify_init1.argtypes = [ctypes.c_int]
                self.libc.inotify_init1.restype = ctypes.c_int
                self.libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
                self.libc.inotify_add_watch.restype = ctypes.c_int
                self._arm()
            except (AttributeError, OSError):
                self.close()

    def _arm(self):
        self.close()
        self.fd = self.libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd < 0:
            self.fd = None
            raise OSError(ctypes.get_errno(), 'Cannot watch Audio Profile directories')
        paths = {}
        for root in self.roots:
            paths.setdefault(root, set()).add(None)
            child = root
            for parent in root.parents:
                paths.setdefault(parent, set()).add(child.name)
                child = parent
        # Watch ancestors too: roots may be absent, renamed or replaced by updates.
        for path in sorted(paths, key=lambda p: len(p.parts)):
            wd = self.libc.inotify_add_watch(self.fd, os.fsencode(path),
                self.MASK | 0x01000000 | 0x02000000)  # ONLYDIR, DONT_FOLLOW
            if wd < 0:
                error = ctypes.get_errno()
                if error == errno.ENOENT:
                    continue
                raise OSError(error, 'Cannot watch Audio Profile directory')
            self.watches.setdefault(wd, set()).update(paths[path])

    def changed(self):
        """True also means monitoring is unavailable: caller must read fresh."""
        if self.fd is None:
            return True
        dirty = False
        try:
            for _ in range(32):
                try:
                    events = os.read(self.fd, 65536)
                except BlockingIOError:
                    break
                if not events:
                    raise OSError('Audio Profile notification stream closed')
                offset = 0
                while offset < len(events):
                    wd, mask, _, length = self.HEADER.unpack_from(events, offset)
                    offset += self.HEADER.size
                    name = os.fsdecode(events[offset:offset + length].split(b'\0', 1)[0])
                    offset += length
                    names = self.watches.get(wd, ())
                    if (mask & self.LOST or not name or name in names
                            or (None in names and name.endswith('.json'))):
                        dirty = True
            else:
                # A noisy ancestor must not monopolize the control executor.
                # Re-arm and conservatively rescan if the drain budget is spent.
                dirty = True
            if dirty:
                self._arm()
            return dirty
        except (OSError, struct.error):
            self.close()
            return True

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        self.watches.clear()

    def __del__(self):
        self.close()
