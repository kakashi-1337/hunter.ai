"""Size and disk limits for ingest. Other modules read these as limits.NAME at call time."""
from __future__ import annotations

import os
import shutil

from .. import config

MAX_FILE_BYTES = 50 * 1024**2
MAX_JOB_BYTES = 2 * 1024**3
MAX_JOB_FILES = 50_000
MAX_ARCHIVE_DEPTH = 3
MAX_RATIO = 200
RATIO_FLOOR_BYTES = 64 * 1024**2
FREE_SPACE_FLOOR = 10 * 1024**3
LIST_TIMEOUT = 30.0
EXTRACT_TIMEOUT = 300.0
MAX_REQUEST_BYTES = 128 * 1024**2


class NoSpace(Exception):
    pass


def free_bytes() -> int:
    fake = os.environ.get("HUNTERAI_FAKE_FREE_BYTES")
    if os.environ.get("HUNTERAI_FAKE_EMBED") == "1" and fake and fake.isdigit():
        return int(fake)       # tests only: the test folder's disk must not decide outcomes
    home = config.home()
    home.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(home).free


def too_large() -> str:
    return f"file too large (over {MAX_FILE_BYTES} bytes)"


def file_cap() -> str:
    return f"job file cap reached ({MAX_JOB_FILES} files)"


def byte_cap() -> str:
    return f"job size cap reached ({MAX_JOB_BYTES} bytes)"


def disk_floor() -> str:
    return f"disk space floor reached ({FREE_SPACE_FLOOR} bytes must stay free)"


def require_space(incoming: int = 0) -> None:
    if free_bytes() - incoming < FREE_SPACE_FLOOR:
        raise NoSpace(disk_floor())


class Budget:
    """What one ingest job has taken so far. `stored` bytes are written under the data folder
    by this app; a folder scan reads its files in place and stores nothing."""

    def __init__(self, files: int = 0, stored_bytes: int = 0):
        self.files, self.stored_bytes = files, stored_bytes

    def refusal(self, size: int, *, stored: bool) -> str | None:
        if self.files >= MAX_JOB_FILES:
            return file_cap()
        if size > MAX_FILE_BYTES:
            return too_large()
        if stored:
            if self.stored_bytes + size > MAX_JOB_BYTES:
                return byte_cap()
            if free_bytes() - size < FREE_SPACE_FLOOR:
                return disk_floor()
        return None

    def room(self) -> tuple[int, str]:
        """For a stored file of unknown size: the most bytes it may have, and why not more."""
        options = [(MAX_FILE_BYTES, too_large()), (MAX_JOB_BYTES - self.stored_bytes, byte_cap()),
                   (free_bytes() - FREE_SPACE_FLOOR, disk_floor())]
        n, why = min(options, key=lambda o: o[0])
        return max(0, n), why

    def add(self, size: int, *, stored: bool) -> None:
        self.files += 1
        if stored:
            self.stored_bytes += size
