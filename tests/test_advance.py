"""Auto-advance, from the outside: does the player move on when it should?

The reported symptom was the player jumping to the next song part-way through
the current one while auto-advancing. These drive the real UI with a stub
bucket and a fake mpg123, and assert on which track the player thinks it is on.
"""

from __future__ import annotations

import asyncio
import unittest

from support import DROPS_ONCE, FakeBackendPath

from player.app import MusicPlayer
from player.library import Day, Track

TRACK_SECONDS = 600


class StubLibrary:
    """A one-day bucket that never touches the network."""

    def __init__(self, count: int = 5):
        self.day = Day(
            date="2026-07-31",
            prefix="stub/2026-07-31",
            tracks=[
                Track(
                    artist=f"Artist {n}",
                    song=f"Song {n}",
                    album="Album",
                    airdate="2026-07-31T07:0{}:00-07:00".format(n % 10),
                    filename=f"track-{n}.mp3",
                    key=f"stub/2026-07-31/track-{n}.mp3",
                )
                for n in range(count)
            ],
            loaded=True,
        )

    def list_days(self) -> list[Day]:
        return [self.day]

    def load_day(self, day: Day) -> Day:
        return day

    def url(self, key: str) -> str:
        return f"http://stub/{key}#dur={TRACK_SECONDS}"


class AdvanceTestCase(unittest.IsolatedAsyncioTestCase):
    async def wait_for(self, predicate, timeout: float = 10.0) -> bool:
        """wait_until, but yielding to the UI loop rather than blocking it."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if predicate():
                return True
            await asyncio.sleep(0.02)
        return False


class DroppedStreamDoesNotAdvance(AdvanceTestCase):
    async def test_a_drop_mid_song_does_not_skip_to_the_next_track(self):
        """The reported bug, end to end."""
        with FakeBackendPath(**DROPS_ONCE):
            app = MusicPlayer(library=StubLibrary())
            async with app.run_test() as pilot:
                self.assertTrue(await self.wait_for(lambda: app.shown_day is not None))
                await pilot.press("space")  # play track 0
                self.assertTrue(await self.wait_for(lambda: app.playing_index == 0))

                # Play through the drop at 30s and well past it.
                self.assertTrue(
                    await self.wait_for(lambda: app.backend.position > 150),
                    f"playback stalled at {app.backend.position:.1f}s",
                )
                self.assertEqual(
                    app.playing_index,
                    0,
                    "the player skipped to the next song part-way through the "
                    "current one, because a dropped stream looked like the end "
                    "of the track",
                )
                self.assertEqual(
                    app.backend.reconnects, 1, "the drop never happened"
                )


class GenuineEndAdvances(AdvanceTestCase):
    """Auto-advance must still work — the fix must not stop the music."""

    async def test_finishing_a_track_moves_on_to_the_next(self):
        with FakeBackendPath(FAKE_SPEED="400"):
            app = MusicPlayer(library=StubLibrary())
            async with app.run_test() as pilot:
                self.assertTrue(await self.wait_for(lambda: app.shown_day is not None))
                await pilot.press("space")
                self.assertTrue(await self.wait_for(lambda: app.playing_index == 0))

                self.assertTrue(
                    await self.wait_for(lambda: app.playing_index == 1, timeout=20),
                    "the track ended but the player did not move on",
                )

    async def test_the_last_track_does_not_wrap_around(self):
        with FakeBackendPath(FAKE_SPEED="400"):
            app = MusicPlayer(library=StubLibrary(count=2))
            async with app.run_test() as pilot:
                self.assertTrue(await self.wait_for(lambda: app.shown_day is not None))
                table = app.query_one("#tracks")
                table.move_cursor(row=1)
                await pilot.press("space")
                self.assertTrue(await self.wait_for(lambda: app.playing_index == 1))

                self.assertFalse(
                    await self.wait_for(
                        lambda: app.playing_index != 1, timeout=8
                    ),
                    "the player wrapped past the end of the day",
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
