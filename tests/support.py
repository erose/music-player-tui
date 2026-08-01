"""Shared test plumbing: put the fake mpg123 on PATH, collect backend events."""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import threading
import time

TESTS = pathlib.Path(__file__).resolve().parent
ROOT = TESTS.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# One drop 30s into a 600s track, then a stream that behaves. Shared so the
# backend and UI tests exercise the same failure.
DROPS_ONCE = {
    "FAKE_SPEED": "100",
    "FAKE_SCENARIO": "drop",
    "FAKE_DROPS": "1",
    "FAKE_DROP_AT": "30",
}


class FakeBackendPath:
    """Context manager putting a `mpg123` shim at the front of PATH.

    The backend resolves the binary with shutil.which at construction time, so
    it is enough to own PATH for as long as the process is spawned.
    """

    def __init__(self, **env: str):
        self.env = {k: str(v) for k, v in env.items()}
        self._tmp: tempfile.TemporaryDirectory | None = None
        self._saved: dict[str, str | None] = {}

    def __enter__(self) -> "FakeBackendPath":
        self._tmp = tempfile.TemporaryDirectory()
        shim = pathlib.Path(self._tmp.name) / "mpg123"
        shim.write_text(
            "#!/bin/sh\n"
            f'exec "{sys.executable}" "{TESTS / "fake_mpg123.py"}" "$@"\n'
        )
        shim.chmod(0o755)

        self._saved["PATH"] = os.environ.get("PATH")
        os.environ["PATH"] = f"{self._tmp.name}{os.pathsep}{os.environ['PATH']}"
        for key, value in self.env.items():
            self._saved[key] = os.environ.get(key)
            os.environ[key] = value
        return self

    def __exit__(self, *exc) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if self._tmp is not None:
            self._tmp.cleanup()


class Events:
    """Thread-safe record of the callbacks a backend fired."""

    def __init__(self) -> None:
        self.states: list[int] = []
        self.errors: list[str] = []
        self._lock = threading.Lock()

    def on_state(self, state: int) -> None:
        with self._lock:
            self.states.append(state)

    def on_error(self, message: str) -> None:
        with self._lock:
            self.errors.append(message)

    def snapshot(self) -> list[int]:
        with self._lock:
            return list(self.states)


def wait_until(predicate, timeout: float = 5.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class PositionSampler:
    """Polls the reported position, so a test can tell "resumed where it died"
    from "started the track over" -- the two look identical once playback has
    caught up again."""

    def __init__(self, backend, interval: float = 0.002):
        self._backend = backend
        self._interval = interval
        self._stop = threading.Event()
        self.samples: list[float] = []
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.samples.append(self._backend.position)
            time.sleep(self._interval)

    def __enter__(self) -> "PositionSampler":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(timeout=1)

    def lowest_after_reaching(self, mark: float) -> float | None:
        """The lowest position seen after the track first got past `mark`."""
        for index, value in enumerate(self.samples):
            if value >= mark:
                rest = self.samples[index:]
                return min(rest) if rest else None
        return None
