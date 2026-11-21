"""Comparison keys for native paths. Never use a comparison spelling to open a file."""
import ntpath
import os
import posixpath
import re
import sys
import unicodedata
from pathlib import Path, PurePosixPath, PureWindowsPath


def windows_path(path):
    value = str(path).replace("/", "\\")
    if value.upper().startswith("\\\\?\\UNC\\"):
        return "\\\\" + value[8:]
    if re.match(r"^\\\\\?\\[A-Za-z]:\\", value):
        return value[4:]
    return value


def canonical_system_path(path):
    if sys.platform == "darwin":
        for name in ("tmp", "var", "etc"):
            alias, target = Path("/") / name, Path("/private") / name
            if path == alias or alias in path.parents:
                if alias.is_symlink() and alias.lstat().st_uid == 0 and alias.resolve() == target:
                    return target / path.relative_to(alias)
    return path


def _fold(value):
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", value).casefold())


def path_key(path, platform=None):
    platform = platform or sys.platform
    if platform == "win32":
        return ntpath.normcase(ntpath.normpath(windows_path(path)))
    value = posixpath.normpath(str(path))
    if platform == "darwin":
        if sys.platform == "darwin" and isinstance(native := Path(value), PurePosixPath):
            value = str(canonical_system_path(native))
        return _fold(value)
    return value


def _ancestors(path, stat_path):
    """Yield each existing ancestor identity with the prospective suffix below it."""
    current = PurePosixPath(path)
    suffix = ()
    for parent in (current, *current.parents):
        try:
            info = stat_path(str(parent))
        except (OSError, ValueError):
            pass
        else:
            if info.st_ino:
                yield (info.st_dev, info.st_ino), suffix
        suffix = (_fold(parent.name), *suffix)


def physical_key(path, platform=None, *, stat_path=os.stat):
    if (platform or sys.platform) == "darwin":
        return next(_ancestors(path, stat_path), None)
    return None


def identity_keys(path, platform=None, *, stat_path=os.stat):
    """Tokens for indexing possible aliases before verifying the live paths."""
    yield ("path", path_key(path, platform))
    if (platform or sys.platform) == "darwin":
        for identity, suffix in _ancestors(path, stat_path):
            yield ("filesystem", identity, suffix)


def same_path(left, right, platform=None, *, stat_path=os.stat):
    if path_key(left, platform) == path_key(right, platform):
        return True
    if (platform or sys.platform) != "darwin":
        return False
    # An alias may exist for only one spelling of a prospective child.
    ancestors = dict(_ancestors(right, stat_path))
    return any(ancestors.get(identity) == suffix for identity, suffix in _ancestors(left, stat_path))


def same_object(left, right, platform=None, *, stat_path=os.stat):
    """Positive authorization for existing objects also checks their actual identity."""
    if (platform or sys.platform) == "win32" and path_key(left, "win32") != path_key(right, "win32"):
        return False
    try:
        a, b = stat_path(left), stat_path(right)
        if a.st_ino and b.st_ino:
            return os.path.samestat(a, b)
    except OSError:
        pass
    return same_path(left, right, platform, stat_path=stat_path)


def within(path, root, platform=None, *, stat_path=os.stat):
    platform = platform or sys.platform
    flavour = PureWindowsPath if platform == "win32" else PurePosixPath
    if flavour(path_key(path, platform)).is_relative_to(flavour(path_key(root, platform))):
        return True
    if platform == "darwin":
        ancestors = dict(_ancestors(root, stat_path))
        for identity, suffix in _ancestors(path, stat_path):
            prefix = ancestors.get(identity)
            if prefix is not None and suffix[:len(prefix)] == prefix:
                return True
    return False


def overlaps(left, right, platform=None, *, stat_path=os.stat):
    return within(left, right, platform, stat_path=stat_path) or within(right, left, platform, stat_path=stat_path)
