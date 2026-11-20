"""Whether the code on disk is still the code this process runs.

HunterAI is updated where it stands (git pull, an edit to a file) and may be running meanwhile.
Python keeps the modules it has loaded, so the process goes on with the old code while the
interface, built again from the new files, asks it for things it does not know: a dialog that
cannot be drawn, a setting that is refused. This notices that the files are no longer what
they were when the process started, so that the owner is told to restart it.
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent      # the hunterai package: what this process is made of
SUFFIXES = (".py", ".sql")                  # the code and the schema; not what Python or an editor writes for itself
EVERY = 5.0                                 # seconds between two looks at the files, however often it is asked


class Watch:
    """The code under one folder as it was when this was made, and whether it is still that."""

    def __init__(self, root: Path = ROOT, every: float = EVERY):
        self.root, self.every = root, every
        self._was = self._digest()
        self._looked = time.monotonic()
        self._changed = False

    def _digest(self) -> str:
        """One value for every file's name and content. Times are left out: a checkout or a save
        that writes what was there changes nothing that runs."""
        total = hashlib.sha256()
        for path in sorted(p for p in self.root.rglob("*") if p.suffix in SUFFIXES and p.is_file()):
            data = path.read_bytes()
            total.update(f"{path.relative_to(self.root).as_posix()}\0{len(data)}\0".encode())
            total.update(data)
        return total.hexdigest()

    def changed(self) -> bool:
        now = time.monotonic()
        if now - self._looked < self.every:
            return self._changed
        self._looked = now
        try:
            self._changed = self._digest() != self._was
        except OSError:             # a file went away between the listing and the read: an update is under way
            pass
        return self._changed


_own = Watch()      # made when the server loads its code: this is what it runs until it is restarted


def restart_needed() -> bool:
    """Whether HunterAI's code changed on disk after this process started."""
    return _own.changed()
