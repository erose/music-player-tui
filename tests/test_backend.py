"""A stream that dies must not be mistaken for a song that ended.

mpg123 reports a dropped connection exactly as it reports a finished track:
`@P 3` then `@P 0`, no `@E`. Captured from mpg123 1.32.9 by serving a real mp3
and hanging up mid-body -- it announced end-of-track 25.51s into a 581s track.
Taken at face value that means "song over, play the next one", which is the
skipping this file exists to prevent.
"""

from __future__ import annotations

import unittest

from support import DROPS_ONCE, Events, FakeBackendPath, PositionSampler, wait_until

from player.app import NowPlaying
from player.backend import FINISHED, PLAYING, STATES, Mpg123

LONG_TRACK = "http://stub/track-one#dur=600"
NEXT_TRACK = "http://stub/track-two#dur=600"


class BackendTestCase(unittest.TestCase):
    def make_backend(self, **env: str) -> Mpg123:
        self.enterContext(FakeBackendPath(**env))
        self.events = Events()
        backend = Mpg123(
            on_progress=lambda position, duration: None,
            on_state=self.events.on_state,
            on_error=self.events.on_error,
        )
        self.addCleanup(backend.quit)
        return backend


class GenuineEndOfTrack(BackendTestCase):
    """The behaviour that must survive the fix."""

    def test_playing_to_the_end_reports_finished_exactly_once(self):
        backend = self.make_backend(FAKE_SPEED="200", FAKE_DURATION="180")
        backend.play("http://stub/short#dur=180")

        self.assertTrue(
            wait_until(lambda: FINISHED in self.events.snapshot(), timeout=10),
            "a track that plays to its end must still report end-of-track",
        )
        self.assertEqual(
            self.events.snapshot().count(FINISHED),
            1,
            "end-of-track must be reported once, not once per @P line",
        )

    def test_reaching_the_end_is_not_treated_as_a_drop(self):
        backend = self.make_backend(FAKE_SPEED="200", FAKE_DURATION="180")
        backend.play("http://stub/short#dur=180")
        wait_until(lambda: FINISHED in self.events.snapshot(), timeout=10)

        self.assertEqual(
            backend.reconnects,
            0,
            "a clean ending must not be mistaken for a dropped connection",
        )


class DroppedConnection(BackendTestCase):
    """The bug: end-of-track arriving long before the end of the track."""

    def test_a_drop_is_not_reported_as_end_of_track(self):
        backend = self.make_backend(**DROPS_ONCE)
        backend.play(LONG_TRACK)

        # Let it die at 30s and get well past that point again.
        self.assertTrue(
            wait_until(lambda: backend.position > 150, timeout=15),
            f"playback never recovered; stalled at {backend.position:.1f}s",
        )
        self.assertNotIn(
            FINISHED,
            self.events.snapshot(),
            "a stream that dropped 30s into a 600s track was reported as "
            "finished, so the app advances to the next song",
        )

    def test_a_drop_resumes_where_it_died(self):
        backend = self.make_backend(**DROPS_ONCE)
        with PositionSampler(backend) as sampler:
            backend.play(LONG_TRACK)
            wait_until(lambda: backend.position > 150, timeout=15)

        lowest = sampler.lowest_after_reaching(30)
        self.assertIsNotNone(lowest, "never reached the point where the stream died")
        self.assertGreater(
            lowest,
            20,
            "recovery restarted the track from the beginning instead of "
            f"picking up where it dropped (fell back to {lowest:.1f}s)",
        )

    def test_a_drop_is_counted_for_auditing(self):
        backend = self.make_backend(**DROPS_ONCE)
        backend.play(LONG_TRACK)
        wait_until(lambda: backend.position > 150, timeout=15)

        self.assertEqual(backend.reconnects, 1)

    def test_a_stream_that_never_starts_is_retried_then_reported(self):
        """No @F ever arrives, so there is no position to compare against.

        Moving on is the right end state here — a track that yields no audio
        cannot be played — but it must be tried again first and then said out
        loud, rather than passing silently as a song the listener heard.
        """
        backend = self.make_backend(FAKE_SPEED="100", FAKE_SCENARIO="stillborn")
        backend.play(LONG_TRACK)

        self.assertTrue(wait_until(lambda: FINISHED in self.events.snapshot(), timeout=10))
        self.assertGreaterEqual(
            backend.reconnects, 1, "gave up without trying to reconnect"
        )
        self.assertTrue(
            self.events.errors,
            "a track that produced no audio was passed off as a finished song",
        )

    def test_a_track_that_keeps_dropping_eventually_gives_up(self):
        """Recovery must not become an infinite reconnect loop."""
        backend = self.make_backend(
            FAKE_SPEED="100", FAKE_SCENARIO="drop_always", FAKE_DROP_AT="30"
        )
        backend.play(LONG_TRACK)

        self.assertTrue(
            wait_until(lambda: FINISHED in self.events.snapshot(), timeout=15),
            "a track that can never play must eventually let the app move on",
        )
        self.assertTrue(
            self.events.errors,
            "giving up on a track should be reported, not silent",
        )
        self.assertLessEqual(
            backend.reconnects,
            10,
            "gave up only after an unreasonable number of reconnects",
        )


class UnexpectedState(BackendTestCase):
    """@P is parsed from a subprocess, so its value is not to be trusted."""

    def test_an_undocumented_state_never_reaches_the_app(self):
        """The four states are exhaustive downstream only because they are
        enforced here, at the one place an @P value gets in."""
        backend = self.make_backend(FAKE_SPEED="100", FAKE_SCENARIO="bogus_state")
        backend.play(LONG_TRACK)

        self.assertTrue(
            wait_until(lambda: backend.position > 20, timeout=10),
            "an unknown @P value stopped playback instead of being ignored",
        )
        self.assertEqual(
            [s for s in self.events.snapshot() if s not in STATES],
            [],
            "a state outside the documented 0-3 was passed on to the UI, "
            "which has no glyph for it",
        )

    def test_the_ui_has_a_glyph_for_every_state_the_backend_can_report(self):
        self.assertEqual(set(NowPlaying.GLYPHS), set(STATES))


class WarmStandby(BackendTestCase):
    """Existing behaviour that the fix must not disturb."""

    def test_a_preloaded_track_starts_from_the_standby(self):
        backend = self.make_backend(FAKE_SPEED="100")
        backend.play(LONG_TRACK)
        wait_until(lambda: backend.state == PLAYING, timeout=5)

        backend.preload(NEXT_TRACK)
        self.assertTrue(wait_until(lambda: backend._standby.ready, timeout=5))

        backend.play(NEXT_TRACK)
        self.assertEqual(backend.warm_starts, 1)
        self.assertTrue(wait_until(lambda: backend.position > 5, timeout=5))


if __name__ == "__main__":
    unittest.main(verbosity=2)
