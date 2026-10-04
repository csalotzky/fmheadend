"""Local candidate resolution and transient playback cycles; no player/lifecycle I/O."""
import os
import random
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urlsplit

from ..errors import DomainError


class LocalMediaMode(StrEnum):
    PLAYLIST = "playlist"
    FOLDER = "folder"


class PlaybackOrder(StrEnum):
    SEQUENTIAL = "sequential"
    RANDOM = "random"


@dataclass(frozen=True)
class LocalMediaConfig:
    mode: LocalMediaMode
    path: Path
    recursive: bool = False
    order: PlaybackOrder = PlaybackOrder.SEQUENTIAL

    def __post_init__(self):
        object.__setattr__(self, "mode", LocalMediaMode(self.mode))
        object.__setattr__(self, "order", PlaybackOrder(self.order))
        object.__setattr__(self, "path", Path(self.path))
        if not self.path.is_absolute():
            raise ValueError("Local media configuration requires an absolute host path")
        if type(self.recursive) is not bool:
            raise ValueError("recursive must be a boolean")
        # Existence is checked by resolution, not by configuration construction.


@dataclass(frozen=True)
class MediaItem:
    """A host filesystem Path or an HTTP(S) URL; URI conversion belongs to the player."""
    locator: Path | str

    def __post_init__(self):
        if isinstance(self.locator, Path):
            if not self.locator.is_absolute():
                raise ValueError("Media paths must be absolute")
        elif isinstance(self.locator, str):
            url = urlsplit(self.locator)
            if url.scheme.lower() not in ("http", "https") or not url.hostname:
                raise ValueError("Remote media requires an HTTP(S) URL with a host")
        else:
            raise TypeError("Media locator must be a Path or HTTP(S) string")


# Candidates only, not decoder guarantees. Verified on target GStreamer 1.26.2:
# mpg123audiodec, flacdec, wavparse, oggdemux/vorbisdec/opusdec,
# qtdemux, aacparse/avdec_aac. No per-file codec probing.
AUDIO_EXTENSIONS = frozenset({".mp3", ".flac", ".wav", ".ogg", ".opus", ".m4a", ".aac"})


def _natural_key(path: Path):
    # Sort components naturally; raw spelling breaks case/leading-zero ties.
    components = tuple(tuple((1, int(part)) if part.isdecimal() else (0, part)
                             for part in re.split(r"([0-9]+)", component.casefold()))
                       for component in path.parts)
    return components, path.as_posix()


def _nonempty(items: list[MediaItem], path: Path) -> list[MediaItem]:
    if not items:
        raise DomainError(409, "NO_PLAYABLE_TRACKS", f"No media candidates in {path}")
    return items


class FolderResolver:
    def resolve(self, path: Path, *, recursive: bool = False) -> list[MediaItem]:
        root = Path(path).absolute()  # Preserve configured symlink spelling.
        pending = [root]
        files = []
        try:
            while pending:
                directory = pending.pop()
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if entry.name.startswith("."):
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if recursive:
                                pending.append(Path(entry.path))
                        elif Path(entry.name).suffix.lower() in AUDIO_EXTENSIONS and entry.is_file():
                            files.append(Path(entry.path))
        except OSError as exc:
            # Fail explicitly rather than returning a silently partial scan.
            raise DomainError(503, "FOLDER_UNREADABLE", f"Cannot enumerate {root}: {exc}") from exc
        files.sort(key=lambda candidate: _natural_key(candidate.relative_to(root)))
        return _nonempty([MediaItem(candidate) for candidate in files], root)


class M3uResolver:
    def resolve(self, path: Path) -> list[MediaItem]:
        playlist = Path(path).absolute()
        items = []
        try:
            # Legacy VLC comments may contain non-UTF-8 metadata. Discard them
            # as bytes; actual entries still require UTF-8 (no charset guessing).
            data = playlist.read_bytes().removeprefix(b"\xef\xbb\xbf")
            for line_number, raw in enumerate(data.splitlines(), 1):
                if playlist.suffix.lower() == ".m3u" and raw.strip().startswith(b"#"):
                    continue
                entry = raw.decode("utf-8").strip()
                if not entry or entry.startswith("#"):
                    continue
                try:
                    # Classify before unquoting: remote URI escapes must survive.
                    if not Path(entry).is_absolute() and re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", entry):
                        item = MediaItem(entry)  # Only HTTP(S); no file:// conversion.
                    else:
                        local = Path(unquote(entry, encoding="utf-8", errors="strict"))
                        if "\x00" in str(local):
                            raise ValueError("Local playlist path contains NUL")
                        item = MediaItem(local if local.is_absolute() else playlist.parent / local)
                except ValueError as exc:
                    raise DomainError(422, "INVALID_PLAYLIST_ENTRY",
                                      f"Invalid entry at {playlist}:{line_number}: {exc}") from exc
                items.append(item)
        except (OSError, UnicodeError) as exc:
            raise DomainError(503, "PLAYLIST_UNREADABLE", f"Cannot read {playlist}: {exc}") from exc
        # Preserve missing local paths, URLs, duplicates and author order. Opening
        # media/decoding it is a later playback responsibility, not resolution.
        return _nonempty(items, playlist)


def resolve_media(config: LocalMediaConfig) -> list[MediaItem]:
    if config.mode == LocalMediaMode.FOLDER:
        return FolderResolver().resolve(config.path, recursive=config.recursive)
    return M3uResolver().resolve(config.path)


class QueueBuilder:
    def __init__(self, order: PlaybackOrder = PlaybackOrder.SEQUENTIAL, *, rng: random.Random | None = None):
        self.order = PlaybackOrder(order)
        self.rng = rng if rng is not None else random.Random()

    def build(self, items: Iterable[MediaItem], *, previous: MediaItem | None = None) -> list[MediaItem]:
        cycle = list(items)  # Never mutate the resolver/caller's list.
        if self.order == PlaybackOrder.RANDOM:
            self.rng.shuffle(cycle)
            if cycle and cycle[0] == previous:
                alternatives = [i for i, item in enumerate(cycle) if item != previous]
                if alternatives:
                    index = self.rng.choice(alternatives)
                    cycle[0], cycle[index] = cycle[index], cycle[0]
        return cycle


class PlaybackQueue:
    """One caller-owned cycle. Exhaustion returns None until explicit rebuild().

    The future source owner decides when to rescan and supplies the new candidates.
    rebuild() without items reuses the last list but generates a fresh cycle.
    A fresh instance resets history on a source restart. No threads or persistence.
    """
    def __init__(self, items: Iterable[MediaItem], order: PlaybackOrder = PlaybackOrder.SEQUENTIAL,
                 *, rng: random.Random | None = None):
        self._builder = QueueBuilder(order, rng=rng)
        self._previous: MediaItem | None = None
        self._candidates: tuple[MediaItem, ...] = ()
        self.rebuild(items)

    def rebuild(self, items: Iterable[MediaItem] | None = None) -> None:
        if items is not None:
            self._candidates = tuple(items)
        self._cycle = self._builder.build(self._candidates, previous=self._previous)
        self._position = 0

    @property
    def exhausted(self) -> bool:
        return self._position == len(self._cycle)

    def next_item(self) -> MediaItem | None:
        if self.exhausted:
            return None
        item = self._cycle[self._position]
        self._position += 1
        self._previous = item
        return item
