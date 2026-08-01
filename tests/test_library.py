"""playlist.json is required, so the interesting cases are the failures.

Every day in the bucket is uploaded with a playlist, so a day without one is
either mid-upload or upstream breakage. Either way the player says so and lets
you retry, rather than inventing metadata from filenames.
"""

from __future__ import annotations

import json
import unittest
import urllib.error

from player.library import Day, Library, PlaylistMissing

PREFIX = "kexp-morning-show-rolling-downloads/2026-07-31"

PLAYLIST = {
    "date": "2026-07-31",
    "program": "The Morning Show",
    "host": "John Richards",
    "tracks": [
        {
            "artist": "ABBA",
            "song": "SOS",
            "album": "ABBA",
            "airdate": "2026-07-31T07:05:28-07:00",
            "file": "ABBA - SOS.mp3",
        },
        {
            "artist": "The Frames",
            "song": "Santa Maria",
            "album": "Burn the Maps",
            "airdate": "2026-07-31T07:09:02-07:00",
            "file": "The Frames - Santa Maria.mp3",
        },
    ],
}


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="http://stub", code=code, msg="stub", hdrs=None, fp=None
    )


class StubLibrary(Library):
    """A Library whose only network call is stubbed."""

    def __init__(self, response):
        super().__init__()
        self.response = response
        self.fetches = 0

    def _get(self, url: str) -> bytes:
        self.fetches += 1
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def a_day(files=("ABBA - SOS.mp3", "The Frames - Santa Maria.mp3")) -> Day:
    return Day(
        date="2026-07-31",
        prefix=PREFIX,
        mp3_keys=[f"{PREFIX}/{name}" for name in files],
    )


class PlaylistPresent(unittest.TestCase):
    def test_tracks_come_from_the_playlist(self):
        library = StubLibrary(json.dumps(PLAYLIST).encode())
        day = library.load_day(a_day())

        self.assertTrue(day.loaded)
        self.assertEqual(day.host, "John Richards")
        self.assertEqual([t.artist for t in day.tracks], ["ABBA", "The Frames"])
        self.assertEqual(day.tracks[1].album, "Burn the Maps")
        self.assertEqual(day.tracks[0].airdate, "2026-07-31T07:05:28-07:00")

    def test_a_second_load_does_not_refetch(self):
        library = StubLibrary(json.dumps(PLAYLIST).encode())
        day = library.load_day(a_day())
        library.load_day(day)
        self.assertEqual(library.fetches, 1)

    def test_a_track_whose_audio_has_not_landed_is_flagged(self):
        """The mp3s and the playlist go up together but not atomically."""
        library = StubLibrary(json.dumps(PLAYLIST).encode())
        day = library.load_day(a_day(files=("ABBA - SOS.mp3",)))

        self.assertEqual([t.available for t in day.tracks], [True, False])


class PlaylistAbsent(unittest.TestCase):
    """No filename heuristic: say so, and stay retryable."""

    def test_a_missing_playlist_raises(self):
        library = StubLibrary(http_error(404))
        with self.assertRaises(PlaylistMissing) as caught:
            library.load_day(a_day())
        self.assertIn("2026-07-31", str(caught.exception))

    def test_a_missing_playlist_does_not_invent_tracks_from_filenames(self):
        library = StubLibrary(http_error(404))
        day = a_day()
        with self.assertRaises(PlaylistMissing):
            library.load_day(day)

        self.assertEqual(day.tracks, [], "tracks were guessed from filenames")
        self.assertFalse(day.loaded, "a failed load must stay retryable")

    def test_a_failed_load_is_retried_and_can_succeed(self):
        """The upload window closes, so the next attempt should get it."""
        library = StubLibrary(http_error(404))
        day = a_day()
        with self.assertRaises(PlaylistMissing):
            library.load_day(day)

        library.response = json.dumps(PLAYLIST).encode()
        library.load_day(day)
        self.assertEqual([t.artist for t in day.tracks], ["ABBA", "The Frames"])

    def test_unreadable_json_raises_rather_than_loading_nothing(self):
        library = StubLibrary(b"{not json")
        with self.assertRaises(PlaylistMissing):
            library.load_day(a_day())


class FetchFails(unittest.TestCase):
    """A transient failure is not the same as a day without a playlist."""

    def test_a_server_error_propagates(self):
        library = StubLibrary(http_error(500))
        with self.assertRaises(urllib.error.HTTPError):
            library.load_day(a_day())

    def test_a_connection_failure_propagates(self):
        library = StubLibrary(OSError("[Errno 54] Connection reset by peer"))
        with self.assertRaises(OSError):
            library.load_day(a_day())

    def test_a_transient_failure_leaves_the_day_retryable(self):
        library = StubLibrary(OSError("boom"))
        day = a_day()
        with self.assertRaises(OSError):
            library.load_day(day)
        self.assertFalse(day.loaded)
        self.assertEqual(day.tracks, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
