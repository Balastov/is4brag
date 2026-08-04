"""Crash-safe local file operations and a small cross-process lock."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Iterable, Iterator, Mapping, Optional


def atomic_write_bytes(path: Path, data: bytes, mode: int = 0o644) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(Path(path), text.encode("utf-8"))


def atomic_write_json(path: Path, value: object) -> None:
    atomic_write_text(Path(path), json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def atomic_write_jsonl(path: Path, records: Iterable[Mapping[str, object]]) -> None:
    text = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
    atomic_write_text(Path(path), text)


class FileLock:
    """Portable lock based on exclusive lock-file creation.

    The lock contains the owning PID. A stale lock may be reclaimed after
    ``stale_after`` seconds; callers should choose a value longer than a normal run.
    """

    def __init__(
        self,
        path: Path,
        timeout: float = 0,
        poll_interval: float = 0.1,
        stale_after: Optional[float] = 24 * 60 * 60,
    ):
        self.path = Path(path)
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.stale_after = stale_after
        self._acquired = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                with os.fdopen(fd, "w", encoding="ascii") as handle:
                    handle.write("%s\n" % os.getpid())
                    handle.flush()
                    os.fsync(handle.fileno())
                self._acquired = True
                return
            except FileExistsError:
                if self.stale_after is not None:
                    try:
                        age = time.time() - self.path.stat().st_mtime
                        if age > self.stale_after:
                            self.path.unlink()
                            continue
                    except FileNotFoundError:
                        continue
                if time.monotonic() >= deadline:
                    raise TimeoutError("ingest lock is already held: %s" % self.path)
                time.sleep(self.poll_interval)

    def release(self) -> None:
        if self._acquired:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            self._acquired = False

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()


@contextmanager
def file_lock(path: Path, timeout: float = 0) -> Iterator[None]:
    with FileLock(path, timeout=timeout):
        yield
