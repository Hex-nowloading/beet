__all__ = [
    "MultiCache",
    "Cache",
    "CachePin",
    "CacheTransaction",
]


import json
import logging
import shutil
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from textwrap import indent
from typing import Any, ClassVar, Iterator, Optional, Set, Type, TypeVar
from urllib.request import urlopen

from .container import Container, MatchMixin, Pin
from .utils import (
    FileSystemPath,
    JsonDict,
    dump_json,
    get_import_string,
    import_from_string,
    log_time,
    normalize_string,
)

PinType = TypeVar("PinType", covariant=True)
CacheType = TypeVar("CacheType", bound="Cache")


logger = logging.getLogger("cache")


class CacheTransaction:
    """Shared transaction for flushing automatically."""

    depth: int

    def __init__(self):
        self.depth = 0

    def enter(self):
        self.depth += 1

    def exit(self) -> bool:
        self.depth -= 1
        return not self.depth


class Cache:
    """An expiring filesystem cache that can store serialized json."""

    deleted: bool
    directory: Path
    index_path: Path
    index: JsonDict
    transaction: CacheTransaction

    index_file: ClassVar[str] = "index.json"

    def __init__(
        self,
        directory: FileSystemPath,
        transaction: Optional[CacheTransaction] = None,
    ):
        self.deleted = False
        self.directory = Path(directory).resolve()
        self.index_path = self.directory / self.index_file
        self.index = (
            json.loads(self.index_path.read_text())
            if self.index_path.is_file()
            else self.get_initial_index()
        )
        self.transaction = transaction or CacheTransaction()
        self.flush()

    def get_initial_index(self) -> JsonDict:
        """Return the initial cache index."""
        return {
            "timestamp": datetime.now().isoformat(),
            "expire": None,
            "json": {},
        }

    def add_finalizer(self, obj: Any):
        """Register the given handler as finalizer."""
        finalizers = self.index.setdefault("finalizers", [])
        dotted_path = obj if isinstance(obj, str) else get_import_string(obj)
        if dotted_path not in finalizers:
            finalizers.append(dotted_path)

    @property
    def json(self) -> JsonDict:
        return self.index["json"]

    @json.setter
    def json(self, value: JsonDict):
        self.index["json"] = value

    def get_path(self, key: str) -> Path:
        """Return a unique file path associated with the given key."""
        keys = self.index.setdefault("keys", {})

        if not (path := keys.get(key)):
            _, dot, extension = key[-12:].rpartition(".")
            suffix = normalize_string(extension) if dot else ""
            path = hex(len(keys)) + (suffix and f".{suffix}")
            keys[key] = path

        return self.directory / path

    def download(self, url: str) -> Path:
        """Download and cache a given url."""
        path = self.get_path(url)

        if not path.is_file():
            with log_time("Download %r.", url), urlopen(url) as f:
                path.write_bytes(f.read())

        return path

    def has_changed(self, *filenames: Optional[FileSystemPath]) -> bool:
        """Return whether any of the given files changed since the last check."""
        mtime = self.index.setdefault("mtime", {})
        changed = False

        for filename in filenames:
            if not filename:
                continue

            path = Path(filename)
            key = str(path)
            last_modified = path.stat().st_mtime

            if mtime.get(key) != last_modified:
                mtime[key] = last_modified
                changed = True

        return changed

    def invalidate_changes(self, *filenames: Optional[FileSystemPath]):
        """Reset the modification time of the given files."""
        mtime = self.index.setdefault("mtime", {})
        for filename in filenames:
            if filename:
                mtime.pop(str(Path(filename)), None)

    @property
    def expire(self) -> Optional[datetime]:
        expire = self.index["expire"]
        return expire and datetime.fromisoformat(expire)

    @expire.setter
    def expire(self, value: Optional[datetime]):
        self.index["expire"] = value and value.isoformat()

    def timeout(self, delta: Optional[timedelta] = None, **kwargs: Any) -> "Cache":
        """Invalidate the cache after a given timeout."""
        if not delta:
            delta = timedelta()
        delta += timedelta(**kwargs)
        self.expire = datetime.fromisoformat(self.index["timestamp"]) + delta
        return self

    def restart_timeout(self):
        """Restart the invalidation timeout."""
        now = datetime.now()
        timestamp = datetime.fromisoformat(self.index["timestamp"])

        if self.expire:
            self.expire += now - timestamp

        self.index["timestamp"] = now.isoformat()

    def __enter__(self) -> "Cache":
        self.transaction.enter()
        return self

    def __exit__(self, *_):
        if self.transaction.exit():
            self.flush()

    def delete(self):
        """Delete the entire cache."""
        if not self.deleted:
            for finalizer in self.index.get("finalizers", []):
                import_from_string(finalizer)(self)
            if self.directory.is_dir():
                shutil.rmtree(self.directory)
            self.index = self.get_initial_index()
            self.deleted = True

    def clear(self):
        """Clear the cache by deleting it and creating it again."""
        self.delete()
        self.deleted = False
        self.flush()

    def flush(self):
        """Flush the modifications to the filesystem."""
        if self.deleted:
            return

        if self.expire and self.expire <= datetime.now():
            logger.debug("Cache %r expired.", self.directory.name)
            self.clear()
        else:
            self.directory.mkdir(parents=True, exist_ok=True)
            self.index_path.write_text(dump_json(self.index))

    @contextmanager
    def override(self, **data: Any):
        """Temporarily update the json data."""
        to_restore: JsonDict = {}
        to_remove: Set[str] = set()

        for key, value in data.items():
            if key in self.json:
                to_restore[key] = self.json[key]
            else:
                to_remove.add(key)
            self.json[key] = value

        try:
            yield self
        finally:
            for key in to_remove:
                del self.json[key]
            self.json.update(to_restore)

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({str(self.directory)!r})"

    def __str__(self) -> str:
        formatted_json = indent(dump_json(self.json), "  |  ")[5:]
        contents = indent("\n".join(self._format_directory()), "  |    ")

        return (
            f"Cache {self.index_path.parent.name}:\n"
            f"  |  timestamp = {datetime.fromisoformat(self.index['timestamp']).ctime()}\n"
            f"  |  expire = {self.expire and self.expire.ctime()}\n  |  \n"
            f"  |  directory = {self.directory}\n{contents}\n  |  \n"
            f"  |  json = {formatted_json}"
        )

    def _format_directory(
        self,
        directory: Optional[FileSystemPath] = None,
        prefix: str = "",
    ) -> Iterator[str]:
        max_entries = 8
        crop_entries = 5

        entries = list(sorted(Path(directory or self.directory).iterdir()))

        count = len(entries)
        if count > max_entries:
            del entries[crop_entries:]

        indents = ["├─"] * len(entries)
        if count <= max_entries:
            indents[-1] = "└─"

        for indent, entry in zip(indents, entries):
            yield f"{prefix}{indent} {entry.name}"

            if entry.is_dir():
                indent = "│  " if indent == "├─" else "   "
                yield from self._format_directory(entry, prefix + indent)

        if count > max_entries:
            yield f"{prefix}└─ ... ({count - crop_entries} more entries)"


class CachePin(Pin[str, PinType]):
    """Descriptor that makes cache data accessible through attribute lookup."""

    def forward(self, obj: Any) -> JsonDict:
        return obj.cache.json


class MultiCache(MatchMixin, Container[str, CacheType]):
    """A container of lazily instantiated named caches."""

    path: Path
    default_cache: str
    gitignore: bool
    cache_type: Type[CacheType]
    transaction: CacheTransaction

    def __init__(
        self,
        directory: FileSystemPath,
        default_cache: str = "default",
        gitignore: bool = True,
        cache_type: Type[CacheType] = Cache,
    ):
        super().__init__()
        self.path = Path(directory).resolve()
        self.default_cache = default_cache
        self.gitignore = gitignore
        self.cache_type = cache_type
        self.transaction = CacheTransaction()

    def missing(self, key: str) -> CacheType:
        cache = self.cache_type(self.path / key, self.transaction)
        self[key] = cache
        return cache

    def __delitem__(self, key: str):
        self[key].delete()
        super().__delitem__(key)

    @property
    def directory(self) -> Path:
        return self[self.default_cache].directory

    @property
    def json(self) -> JsonDict:
        return self[self.default_cache].json

    def __enter__(self) -> "MultiCache[CacheType]":
        self.transaction.enter()
        return self

    def __exit__(self, *_):
        if self.transaction.exit():
            self.flush()

    def preload(self):
        """Preload all the named caches."""
        if not self.path.is_dir():
            return
        for directory in self.path.iterdir():
            if (directory / self.cache_type.index_file).is_file():
                assert self[directory.name]

    def clear(self):
        """Clear the entire cache."""
        if self.path.is_dir():
            shutil.rmtree(self.path)
        super().clear()

    def flush(self):
        """Flush the modifications to the filesystem."""
        for cache in self.values():
            cache.flush()

        if (
            self.gitignore
            and self.path.is_dir()
            and not (ignore := self.path / ".gitignore").is_file()
        ):
            ignore.write_text("# Automatically created by beet\n*\n")

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({str(self.path)!r})"
