"""Playback backend: a pair of `mpg123 -R` processes.

mpg123's remote mode is a line protocol on stdin/stdout. We send commands
(LOAD/LOADPAUSED/PAUSE/JUMP) and it streams status back:

    @S <stream info>            stream opened
    @F <frame> <left> <sec> <sec-left>
    @P <0|1|2|3>                stopped | paused | playing | end-of-track
    @E <message>                error

It decodes straight from a public HTTPS URL with range requests, so audio
never touches disk, and @F gives real elapsed/remaining seconds rather than a
wall-clock estimate.

Why two processes: switching a single process to a new URL costs ~285ms of
silence while it opens a fresh TLS connection to S3 and re-syncs the decoder —
audible as a hiccup every time you change song. `LOADPAUSED` opens a stream
without emitting any audio, so we keep a standby process warmed up with the
track the user is most likely to pick next. Starting a preloaded stream takes
3-6ms instead of ~285ms; the two processes then swap roles.
"""

from __future__ import annotations

import atexit
import shutil
import subprocess
import threading
from collections.abc import Callable

STOPPED, PAUSED, PLAYING, FINISHED = 0, 1, 2, 3

# mpg123's decode loop blocks writing to the audio device, so it only services
# stdin once per device buffer — with the default buffer that's a ~390ms lag
# before PAUSE takes effect. 50ms brings that down to ~115ms. Measured over 25s
# of network streaming with no underruns; there is less slack for network
# jitter below this. (--buffer makes it worse: that's an output cache pause has
# to drain, measured at ~520ms.)
DEVICE_BUFFER = "0.05"


class BackendMissing(RuntimeError):
    pass


class _Stream:
    """One mpg123 process. Callbacks fire on its reader thread."""

    def __init__(
        self,
        on_state: Callable[["_Stream", int], None],
        on_error: Callable[["_Stream", str], None],
    ):
        self._on_state = on_state
        self._on_error = on_error

        self.url: str | None = None
        self.ready = False  # preloaded, connection open, waiting at 0:00
        self.position = 0.0
        self.duration = 0.0
        self.state = STOPPED

        self._preloading = False
        self._lock = threading.Lock()
        self._proc = subprocess.Popen(
            ["mpg123", "-R", "--utf8", "--devbuffer", DEVICE_BUFFER],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        threading.Thread(target=self._read_loop, daemon=True).start()

    def _send(self, command: str) -> None:
        with self._lock:
            if self._proc.poll() is not None or self._proc.stdin is None:
                return
            try:
                self._proc.stdin.write(command + "\n")
                self._proc.stdin.flush()
            except (BrokenPipeError, ValueError):
                pass

    def load(self, url: str) -> None:
        """Open and start playing. LOAD reads to end of line, so the URL must
        be percent-encoded — see Library.url()."""
        self.url, self.ready, self._preloading = url, False, False
        self.position = self.duration = 0.0
        self._send(f"LOAD {url}")

    def load_paused(self, url: str) -> None:
        """Open the stream but emit nothing, so it can be started instantly."""
        self.url, self.ready, self._preloading = url, False, True
        self.position = self.duration = 0.0
        self._send(f"LOADPAUSED {url}")

    def toggle_pause(self) -> None:
        self._send("PAUSE")

    def stop(self) -> None:
        self.url, self.ready, self._preloading = None, False, False
        self._send("STOP")

    def jump(self, seconds: int) -> None:
        self._send(f"JUMP {seconds}s")

    def restart_at(self, seconds: int, stay_paused: bool) -> None:
        """Re-open the current URL and seek forward to `seconds`."""
        if self.url is None:
            return
        self.position = float(seconds)
        self._send(f"LOAD {self.url}")
        self._send(f"JUMP {seconds}s")
        if stay_paused:
            self._send("PAUSE")

    def ask_quit(self) -> None:
        if self._proc.poll() is None:
            self._send("QUIT")

    def reap(self) -> None:
        """Wait for exit, killing if need be. Never raises — this runs from
        atexit, where an exception would skip the remaining cleanup."""
        if self._proc.poll() is not None:
            return
        try:
            self._proc.wait(timeout=2)
            return
        except subprocess.TimeoutExpired:
            pass
        self._proc.kill()
        try:
            self._proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass

    def quit(self) -> None:
        self.ask_quit()
        self.reap()

    def _read_loop(self) -> None:
        stdout = self._proc.stdout
        if stdout is None:
            return
        for line in stdout:
            tag, _, rest = line.rstrip("\n")[1:].partition(" ") if line.startswith("@") else ("", "", "")

            if tag == "F":
                parts = rest.split()
                if len(parts) >= 4:
                    try:
                        elapsed, remaining = float(parts[2]), float(parts[3])
                    except ValueError:
                        continue
                    self.position, self.duration = elapsed, elapsed + remaining

            elif tag == "P":
                try:
                    state = int(rest.split()[0])
                except (ValueError, IndexError):
                    continue
                # A LOADPAUSED reports @P 1 once the connection is open. That's
                # the standby going ready, not a user-visible pause.
                if self._preloading and state == PAUSED:
                    self._preloading, self.ready = False, True
                    self.state = state
                    continue
                # End of track is @P 3 followed by @P 0. Report the 3 and
                # swallow the trailing 0 so callers advance exactly once.
                if state == STOPPED and self.state == FINISHED:
                    continue
                self.state = state
                self._on_state(self, state)

            elif tag == "E":
                self._on_error(self, rest)


class Mpg123:
    """An active stream plus a warm standby, swapped on demand."""

    def __init__(
        self,
        on_progress: Callable[[float, float], None],
        on_state: Callable[[int], None],
        on_error: Callable[[str], None],
    ):
        if shutil.which("mpg123") is None:
            raise BackendMissing("mpg123 not found on PATH (try: brew install mpg123)")

        self.on_progress = on_progress
        self.on_state = on_state
        self.on_error = on_error

        self._streams = [
            _Stream(self._state_event, self._error_event),
            _Stream(self._state_event, self._error_event),
        ]
        self._active_ix = 0
        # Whether a start used the warm standby or had to open a connection.
        self.warm_starts = 0
        self.cold_starts = 0
        # The UI calls quit() on unmount, but if the app dies some other way an
        # orphaned mpg123 keeps playing with no window to stop it. atexit
        # covers a crash or an unhandled exception; nothing can cover SIGKILL.
        atexit.register(self.quit)

    # -- the active stream fronts the public state ------------------------

    @property
    def _active(self) -> _Stream:
        return self._streams[self._active_ix]

    @property
    def _standby(self) -> _Stream:
        return self._streams[1 - self._active_ix]

    @property
    def position(self) -> float:
        return self._active.position

    @property
    def duration(self) -> float:
        return self._active.duration

    @property
    def state(self) -> int:
        return self._active.state

    @state.setter
    def state(self, value: int) -> None:
        self._active.state = value

    def _state_event(self, stream: _Stream, state: int) -> None:
        # The outgoing stream reports @P 0 as it is stopped; ignore anything
        # that isn't the stream currently on the speakers.
        if stream is self._active:
            self.on_state(state)

    def _error_event(self, stream: _Stream, message: str) -> None:
        if stream is self._active:
            self.on_error(message)

    # -- commands ---------------------------------------------------------

    def preload(self, url: str) -> None:
        """Warm the standby with a URL, best effort. No audio is produced."""
        if url in (self._active.url, self._standby.url):
            return
        self._standby.load_paused(url)

    def play(self, url: str) -> None:
        standby = self._standby
        if standby.ready and standby.url == url:
            outgoing = self._active
            self._active_ix = 1 - self._active_ix
            standby.toggle_pause()  # unpause: 3-6ms, connection already open
            outgoing.stop()
            self.warm_starts += 1
        else:
            self._active.load(url)  # ~285ms of silence opening the connection
            self.cold_starts += 1

    def toggle_pause(self) -> None:
        if self.state in (STOPPED, FINISHED):
            return
        # Flip optimistically so the UI can redraw on the keypress instead of
        # waiting for the @P acknowledgement. The reader thread overwrites this
        # with the real value a moment later, so a wrong guess self-corrects.
        self.state = PAUSED if self.state == PLAYING else PLAYING
        self._active.toggle_pause()

    def stop(self) -> None:
        self._active.stop()

    def seek(self, seconds: float, relative: bool = False) -> None:
        """Seek to an absolute position, or by a delta when `relative`.

        mpg123 consumes the HTTP response sequentially, so it can only seek
        forward within a stream — any backward JUMP fails with "Error while
        seeking" and playback just runs on. To go backward we re-open the URL
        (a fresh request from byte 0) and seek forward to the target instead.
        Commands are processed in order, so LOAD then JUMP is safe to pipeline.
        """
        active = self._active
        target = active.position + seconds if relative else seconds
        target = max(0.0, target)
        if active.duration > 0:
            target = min(target, active.duration)

        if target >= active.position:
            active.jump(int(target))
        else:
            active.restart_at(int(target), stay_paused=active.state == PAUSED)

    def quit(self) -> None:
        # Ask both to exit before waiting on either, so teardown costs one
        # timeout rather than one per process.
        for stream in self._streams:
            stream.ask_quit()
        for stream in self._streams:
            stream.reap()
