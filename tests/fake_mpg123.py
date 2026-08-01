#!/usr/bin/env python3
"""A stand-in for `mpg123 -R`, so playback logic can be tested without audio.

Speaks the same line protocol as the real thing (checked against mpg123
1.32.9), but on a compressed clock: FAKE_SPEED virtual seconds pass per real
second, so a 3-minute track finishes in under a second.

Track length comes from the URL -- `...#dur=200` means 200 seconds --
defaulting to FAKE_DURATION.

Failure injection, via FAKE_SCENARIO:

  normal        every stream plays to its end
  drop          the first FAKE_DROPS streams die at FAKE_DROP_AT virtual
                seconds, reporting end-of-track exactly as a real dropped
                connection does; later streams play through
  drop_always   every stream dies at FAKE_DROP_AT, so recovery never succeeds
  stillborn     streams die before decoding a single frame, so end-of-track
                arrives with no position or duration ever reported
  bogus_state   an @P value outside the documented 0-3 arrives before
                playback starts, standing in for a protocol we don't
                understand

The `drop` shapes are modelled on a real capture: hanging up mid-body made
mpg123 emit `@P 3` then `@P 0` -- indistinguishable from a finished song
except that the last `@F` showed 25.51s elapsed with 555.52s remaining.
"""

from __future__ import annotations

import os
import sys
import threading
import time

SPEED = float(os.environ.get("FAKE_SPEED", "100"))
DURATION = float(os.environ.get("FAKE_DURATION", "180"))
SCENARIO = os.environ.get("FAKE_SCENARIO", "normal")
DROP_AT = float(os.environ.get("FAKE_DROP_AT", "30"))
DROPS = int(os.environ.get("FAKE_DROPS", "1"))
TICK = 0.005  # real seconds between @F frames

STOPPED, PAUSED, PLAYING, FINISHED = 0, 1, 2, 3

out_lock = threading.Lock()


def emit(line: str) -> None:
    with out_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


class Player:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.state = STOPPED
        self.url: str | None = None
        self.position = 0.0
        self.duration = 0.0
        self.frame = 0
        self.silence = False
        self.drops = 0
        self.stillborn = False

    def _should_drop(self) -> bool:
        if SCENARIO == "drop_always":
            return True
        return SCENARIO == "drop" and self.drops < DROPS

    # -- commands ------------------------------------------------------

    def load(self, url: str, paused: bool) -> None:
        with self.lock:
            self.url = url
            self.position = 0.0
            self.frame = 0
            self.stillborn = False  # a fresh stream, until told otherwise
            self.duration = DURATION
            if "#dur=" in url:
                try:
                    self.duration = float(url.split("#dur=", 1)[1])
                except ValueError:
                    pass
            emit("@I {")
            emit("@I }")
            if SCENARIO == "bogus_state":
                emit("@P 9")
            if paused:
                self.state = PAUSED
                emit("@P 1")
                return
            self.state = PLAYING
            emit("@P 2")
            emit("@S 1.0 3 44100 Joint-Stereo 2 418 2 0 0 0 128 0 1")
            if SCENARIO == "stillborn":
                # The connection was already gone: end-of-track before a single
                # frame is decoded, so nothing ever reports a position.
                self.stillborn = True
                self._finish()

    def toggle_pause(self) -> None:
        with self.lock:
            if self.state == PLAYING:
                self.state = PAUSED
                emit("@P 1")
            elif self.state == PAUSED:
                self.state = PLAYING
                emit("@P 2")
                emit("@S 1.0 3 44100 Joint-Stereo 2 418 2 0 0 0 128 0 1")
                if SCENARIO == "stillborn":
                    self.stillborn = True
                    self._finish()

    def stop(self) -> None:
        with self.lock:
            self.state = STOPPED
            self.url = None
            self.position = 0.0
            emit("@P 0")

    def jump(self, spec: str) -> None:
        with self.lock:
            spec = spec.strip().rstrip("s")
            try:
                target = float(spec.lstrip("+-"))
            except ValueError:
                return
            if spec.startswith("+"):
                self.position += target
            elif spec.startswith("-"):
                self.position = max(0.0, self.position - target)
            else:
                self.position = target
            emit(f"@J {int(self.position)}")

    # -- the decode loop -----------------------------------------------

    def _finish(self) -> None:
        """End of track, exactly as mpg123 reports it: @P 3 then @P 0.

        A dropped connection is reported the same way -- that is the whole
        problem this fake exists to reproduce.
        """
        self.state = FINISHED
        emit("@P 3")
        emit("@P 0")
        self.state = STOPPED
        self.url = None

    def run(self) -> None:
        while True:
            time.sleep(TICK)
            with self.lock:
                if self.state != PLAYING or self.stillborn:
                    continue
                self.position += TICK * SPEED
                self.frame += 1
                if self._should_drop() and self.position >= DROP_AT:
                    self.drops += 1
                    self._finish()
                    continue
                if self.position >= self.duration:
                    self.position = self.duration
                    self._finish()
                    continue
                if not self.silence:
                    remaining = max(0.0, self.duration - self.position)
                    emit(
                        f"@F {self.frame} {int(remaining * 38)} "
                        f"{self.position:.2f} {remaining:.2f}"
                    )


def main() -> None:
    player = Player()
    threading.Thread(target=player.run, daemon=True).start()
    emit("@R MPG123 (fake) v11")

    for line in sys.stdin:
        command, _, rest = line.strip().partition(" ")
        command = command.upper()
        if command == "LOAD":
            player.load(rest, paused=False)
        elif command in ("LOADPAUSED", "LP"):
            player.load(rest, paused=True)
        elif command == "PAUSE":
            player.toggle_pause()
        elif command == "STOP":
            player.stop()
        elif command == "JUMP":
            player.jump(rest)
        elif command == "SILENCE":
            player.silence = True
        elif command == "QUIT":
            break
    sys.exit(0)


if __name__ == "__main__":
    main()
