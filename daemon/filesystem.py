"""Read-only, allowed-root filesystem navigation for the LOCAL source editor."""
from __future__ import annotations

import os
import re
from pathlib import Path

from .errors import DomainError

DEFAULT_ROOTS = (
    ("Mounted storage", "/mnt"),
    ("Removable media", "/media"),
    ("Local media", "/srv"),
)
PLAYLIST_SUFFIXES = {".m3u", ".m3u8"}


def _natural_key(name: str) -> list[tuple[int, int | str]]:
    return [(0, int(part)) if part.isdecimal() else (1, part.casefold())
            for part in re.split(r"(\d+)", name)]


class FilesystemBrowser:
    """Authorize resolved paths before returning directory entry names only."""

    def __init__(self, extra_roots=()):
        configured = [(name, path) for name, path in DEFAULT_ROOTS]
        seen = {path for _, path in configured}
        for root in extra_roots:
            path = str(Path(root))
            if path not in seen:
                configured.append((Path(path).name or path, path))
                seen.add(path)
        self.roots = tuple(configured)

    def locations(self):
        return {
            "locations": [
                {"name": name, "path": path, "available": Path(path).is_dir()}
                for name, path in self.roots
            ]
        }

    def browse(self, path: str, mode: str):
        if mode not in ("folder", "playlist"):
            raise DomainError(422, "INVALID_BROWSER_MODE", "Browser mode must be folder or playlist.")
        requested = Path(path)
        if not requested.is_absolute():
            raise DomainError(422, "INVALID_PATH", "Path must be an absolute host filesystem path.")
        try:
            resolved = requested.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise DomainError(404, "PATH_UNAVAILABLE", "This location is no longer available.") from exc
        if not resolved.is_dir():
            raise DomainError(422, "NOT_A_DIRECTORY", "Path must identify a readable directory.") from None

        root_path, resolved_root = self._containing_root(resolved)
        if root_path is None:
            raise DomainError(403, "PATH_NOT_ALLOWED",
                              "Path is outside the configured media browser roots.")
        entries = self._entries(resolved, resolved_root, mode)
        return {
            "path": str(resolved),
            "root": str(resolved_root),
            "can_go_up": resolved != resolved_root,
            "entries": entries,
        }

    def _containing_root(self, resolved: Path):
        matches = []
        for _, root_path in self.roots:
            root = Path(root_path)
            try:
                resolved_root = root.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if not resolved_root.is_dir():
                continue
            try:
                resolved.relative_to(resolved_root)
            except ValueError:
                continue
            matches.append((root, resolved_root))
        return max(matches, key=lambda item: len(item[1].parts), default=(None, None))

    def _entries(self, directory: Path, root: Path, mode: str):
        entries = []
        try:
            with os.scandir(directory) as scan:
                for entry in scan:
                    if entry.name.startswith("."):
                        continue
                    try:
                        target = Path(entry.path).resolve(strict=True)
                        target.relative_to(root)
                        if entry.is_dir():
                            entries.append({"name": entry.name, "type": "directory"})
                        elif mode == "playlist" and entry.is_file() and target.suffix.casefold() in PLAYLIST_SUFFIXES:
                            entries.append({"name": entry.name, "type": "file"})
                    except (OSError, RuntimeError, ValueError):
                        continue
        except PermissionError as exc:
            raise DomainError(403, "PATH_UNREADABLE", "This location is unavailable or cannot be read.") from exc
        except OSError as exc:
            raise DomainError(404, "PATH_UNAVAILABLE", "This location is no longer available.") from exc
        return sorted(entries, key=lambda entry: (entry["type"] != "directory", _natural_key(entry["name"])))
