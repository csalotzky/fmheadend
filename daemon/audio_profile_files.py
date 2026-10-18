"""Bounded JSON storage. Saves persist files only; no pipeline/hardware actions."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
import unicodedata

from pydantic import ValidationError

from .errors import DomainError
from .models.audio_profile_file import AudioProfileFile, ProfileFileResult, ProfileFileSummary

MAX_PROFILE_BYTES = 65536


@contextmanager
def storage_errors():
    try:
        yield
    except OSError as exc:
        raise DomainError(503, "PROFILE_STORAGE_ERROR", f"Profile storage unavailable: {exc.strerror}") from exc


class AudioProfileFiles:
    def __init__(self, default_root, custom_root, *, is_referenced=lambda path: False,
                 is_system_default=lambda path: False,
                 clock=lambda: datetime.now(timezone.utc)):
        # Roots are trusted deployment configuration, never supplied by API clients.
        self.roots = {"default": Path(default_root).absolute(), "custom": Path(custom_root).absolute()}
        self.is_referenced = is_referenced
        self.is_system_default = is_system_default
        self.clock = clock

    def _path(self, logical):
        if not re.fullmatch(r"(default|custom)/[a-zA-Z0-9_-]+\.json", logical):
            raise DomainError(422, "INVALID_PROFILE_PATH", "Use default/name.json or custom/name.json")
        kind, filename = logical.split("/")
        root = self.roots[kind]
        path = root / filename
        # Reject symlinks, including ancestor/root links, rather than following them.
        if any(p.is_symlink() or getattr(p, 'is_junction', lambda: False)()
               for p in (path, *path.parents)):
            raise DomainError(422, "INVALID_PROFILE_PATH", "Profile paths must not contain symlinks")
        return path

    @staticmethod
    def validate(data):
        try:
            return AudioProfileFile.model_validate(data, strict=True)
        except ValidationError as exc:
            raise DomainError(422, "INVALID_PROFILE", str(exc)) from exc

    def get(self, logical):
        with storage_errors():
            path = self._path(logical)
            try:
                if path.exists() and not path.is_file():
                    raise DomainError(422, "INVALID_PROFILE", f"Not a regular profile file: {logical}")
                flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
                with os.fdopen(os.open(path, flags), "rb") as stream:
                    raw = stream.read(MAX_PROFILE_BYTES + 1)
            except FileNotFoundError as exc:
                raise DomainError(404, "PROFILE_NOT_FOUND", f"Profile not found: {logical}") from exc
            if len(raw) > MAX_PROFILE_BYTES:
                raise DomainError(422, "INVALID_PROFILE", "Profile exceeds 64 KiB")
            try:
                data = json.loads(raw)
            except (ValueError, UnicodeError, RecursionError) as exc:
                raise DomainError(422, "INVALID_PROFILE", f"Invalid JSON in {logical}: {exc}") from exc
            profile = self.validate(data)
            return ProfileFileResult(name=profile.name, description=profile.description, path=logical,
                kind=logical.split('/')[0], profile=profile, is_system_default=self.is_system_default(logical))

    def list(self):
        result = []
        with storage_errors():
            for kind, root in self.roots.items():
                self._path(f"{kind}/check.json")
                # Custom storage is created lazily; missing installed defaults is an installation error.
                if not root.exists() and kind == "custom":
                    continue
                for path in sorted(root.iterdir()):
                    if path.suffix == '.json':
                        item = self.get(f"{kind}/{path.name}")
                        result.append(ProfileFileSummary(**item.model_dump(exclude={'profile'})))
        return result

    @staticmethod
    def _custom(logical):
        if logical.startswith("default/"):
            raise DomainError(403, "READ_ONLY_PROFILE", "Default profiles are read-only; clone to custom first")

    def _write(self, logical, profile, *, replace):
        with storage_errors():
            path = self._path(logical)
            path.parent.mkdir(parents=True, exist_ok=True)
            encoded = (profile.model_dump_json(indent=2) + '\n').encode('utf-8')
            if len(encoded) > MAX_PROFILE_BYTES:
                raise DomainError(422, "INVALID_PROFILE", "Profile exceeds 64 KiB")
            fd, temporary = tempfile.mkstemp(prefix='.profile-', suffix='.tmp', dir=path.parent)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                self._path(logical)
                if replace:
                    os.replace(temporary, path)
                else:
                    # Atomic publish without overwriting, even if another creator wins the name.
                    try:
                        os.link(temporary, path)
                    except FileExistsError:
                        return False
                return True
            finally:
                Path(temporary).unlink(missing_ok=True)

    def create(self, data):
        profile = self.validate(data)
        ascii_name = unicodedata.normalize('NFKD', profile.name).encode('ascii', 'ignore').decode().lower()
        slug = re.sub(r'[^a-z0-9]+', '_', ascii_name).strip('_')[:80].rstrip('_') or 'profile'
        stem = f"{slug}_{self.clock():%Y%m%d%H%M%S}"
        for collision in range(1000):
            suffix = f"_{collision}" if collision else ''
            logical = f"custom/{stem}{suffix}.json"
            if self._write(logical, profile, replace=False):
                return self.get(logical)
        raise DomainError(409, "PROFILE_NAME_COLLISION", "Too many profiles created with this name; try again later")

    def save(self, logical, data):
        self._custom(logical)
        profile = self.validate(data)
        # Allow repairing invalid JSON, but never create implicitly via save.
        with storage_errors():
            if not self._path(logical).is_file():
                raise DomainError(404, "PROFILE_NOT_FOUND", f"Profile not found: {logical}")
        self._write(logical, profile, replace=True)
        return self.get(logical)

    def clone(self, logical, name):
        data = self.get(logical).profile.model_dump()
        data['name'] = name
        return self.create(data)

    def delete(self, logical):
        self._custom(logical)
        with storage_errors():
            path = self._path(logical)
            if self.is_system_default(logical) or self.is_referenced(logical):
                raise DomainError(409, "PROFILE_IN_USE", "Profile is the system default or referenced by a Source")
            try:
                path.unlink()
            except FileNotFoundError as exc:
                raise DomainError(404, "PROFILE_NOT_FOUND", f"Profile not found: {logical}") from exc
