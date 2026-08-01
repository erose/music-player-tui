#!/usr/bin/env python3
"""Tests for the KEXP archive-extraction path in morning_show_playlist.py.

Everything is mocked — no network calls and no real ffmpeg — so these are fast
and safe to run anywhere. Run with the rest of the suite:

    .venv/bin/python -m unittest discover -s tests -t tests

Scope: the archive functionality (_to_utc_z, resolve_stream, download_archive)
plus the filename helper it relies on. (Playlist-fetching / find_airing tests
live elsewhere.)
"""

import io
import os
import json
import types
import tempfile
import unittest
from datetime import datetime
from unittest import mock

import support  # noqa: F401  — puts the repo root on sys.path

import morning_show_playlist as m


def play(artist, song, airdate):
    """Build a minimal trackplay dict like the KEXP API returns."""
    return {"artist": artist, "song": song, "airdate": airdate, "play_type": "trackplay"}


def ffmpeg_arg(cmd, flag):
    """Return the value following `flag` in an ffmpeg argv list."""
    return cmd[cmd.index(flag) + 1]


class TestToUtcZ(unittest.TestCase):
    def test_pacific_to_utc(self):
        self.assertEqual(m._to_utc_z("2026-07-28T07:05:28-07:00"),
                         "2026-07-28T14:05:28Z")

    def test_already_utc_passthrough(self):
        self.assertEqual(m._to_utc_z("2026-07-28T14:05:28+00:00"),
                         "2026-07-28T14:05:28Z")

    def test_drops_subsecond(self):
        self.assertEqual(m._to_utc_z("2026-01-01T00:00:00.500-08:00"),
                         "2026-01-01T08:00:00Z")


class TestSafeFilename(unittest.TestCase):
    def test_slash_becomes_division_slash(self):
        # A real '/' can't appear in a filename; it maps to U+29F8, not collapsed.
        self.assertEqual(m.safe_filename("Man/Woman/Chainsaw - Nosedive"),
                         "Man⧸Woman⧸Chainsaw - Nosedive")

    def test_colon_becomes_dash(self):
        self.assertEqual(m.safe_filename("Artist: The Song"), "Artist- The Song")

    def test_strips_control_chars(self):
        self.assertEqual(m.safe_filename("a\x00b\x1fc"), "abc")

    def test_strips_trailing_dot_and_space(self):
        self.assertEqual(m.safe_filename("Song.  "), "Song")


class TestResolveStream(unittest.TestCase):
    def _fake_urlopen(self, payload):
        """Return a context-manager fake whose body is `payload` JSON bytes,
        and a holder that captures the Request passed to urlopen."""
        captured = {}

        def _urlopen(req, timeout=None):
            captured["req"] = req
            body = io.BytesIO(json.dumps(payload).encode())
            cm = mock.MagicMock()
            cm.__enter__.return_value = body
            cm.__exit__.return_value = False
            return cm

        return _urlopen, captured

    def test_returns_url_and_int_offset(self):
        payload = {"sg-url": "https://cdn/x.mp3", "sg-url-next": "https://cdn/y.mp3",
                   "sg-offset": 821}
        fake, _ = self._fake_urlopen(payload)
        with mock.patch.object(m.urllib.request, "urlopen", fake):
            url, off = m.resolve_stream("2026-07-28T07:05:28-07:00")
        self.assertEqual(url, "https://cdn/x.mp3")
        self.assertEqual(off, 821)
        self.assertIsInstance(off, int)

    def test_sends_referer_and_utc_timestamp(self):
        payload = {"sg-url": "u", "sg-offset": 1}
        fake, captured = self._fake_urlopen(payload)
        with mock.patch.object(m.urllib.request, "urlopen", fake):
            m.resolve_stream("2026-07-28T07:05:28-07:00", bitrate=128, location=3)
        req = captured["req"]
        # Referer header is what stops the endpoint 403ing.
        self.assertEqual(req.headers.get("Referer"), "https://www.kexp.org/")
        # airdate converted to UTC 'Z' form in the query string.
        self.assertIn("timestamp=2026-07-28T14%3A05%3A28Z", req.full_url)
        self.assertIn("bitrate=128", req.full_url)
        self.assertIn("location=3", req.full_url)


class TestDownloadArchive(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # Redirect output into a temp dir so we never touch real downloads/.
        self._dir_patch = mock.patch.object(m, "DOWNLOAD_DIR", self.tmp)
        self._dir_patch.start()
        # ffmpeg "present" without needing it installed.
        self._which_patch = mock.patch.object(m.shutil, "which", return_value="/bin/ffmpeg")
        self._which_patch.start()
        # Record every ffmpeg invocation and fake a successful rip by touching
        # the output file (download_archive checks the file exists afterwards).
        self.calls = []

        def fake_run(cmd, *a, **k):
            self.calls.append(cmd)
            open(cmd[-1], "w").close()
            return types.SimpleNamespace(returncode=0)

        self._run_patch = mock.patch.object(m.subprocess, "run", side_effect=fake_run)
        self.run_mock = self._run_patch.start()

    def tearDown(self):
        self._dir_patch.stop()
        self._which_patch.stop()
        self._run_patch.stop()

    def _patch_resolver(self, mapping):
        """Patch resolve_stream to look up (url, offset) by airdate string."""
        def fake(ts, bitrate=256, location=3):
            return mapping[ts]
        return mock.patch.object(m, "resolve_stream", side_effect=fake)

    def test_durations_from_offset_deltas(self):
        a1, a2, a3 = ("2026-07-28T07:05:28-07:00",
                      "2026-07-28T07:08:01-07:00",
                      "2026-07-28T07:11:38-07:00")
        end = "2026-07-28T09:50:00-07:00"
        mapping = {
            a1: ("show.mp3", 922),
            a2: ("show.mp3", 1075),
            a3: ("show.mp3", 1292),
            end: ("nextshow.mp3", 5),   # boundary → different file
        }
        tracks = [play("R.E.M.", "Fall on Me", a1),
                  play("Quivers", "You're Not Always On My Mind", a2),
                  play("10,000 Maniacs", "These Are Days", a3)]
        with self._patch_resolver(mapping), mock.patch("builtins.print"):
            m.download_archive(tracks, end, shift=0, lead=0, tail=0)   # isolate duration logic

        self.assertEqual(len(self.calls), 3)
        # T1: 1075-922 = 153; T2: 1292-1075 = 217
        self.assertEqual(ffmpeg_arg(self.calls[0], "-ss"), "922.000")
        self.assertEqual(ffmpeg_arg(self.calls[0], "-t"), "153.000")
        self.assertEqual(ffmpeg_arg(self.calls[1], "-ss"), "1075.000")
        self.assertEqual(ffmpeg_arg(self.calls[1], "-t"), "217.000")
        # -i is the show URL, output is Artist - Song.mp3
        self.assertEqual(ffmpeg_arg(self.calls[0], "-i"), "show.mp3")
        self.assertTrue(self.calls[0][-1].endswith("R.E.M. - Fall on Me.mp3"))

    def test_last_track_same_file_uses_end_offset(self):
        a1 = "2026-07-28T09:47:00-07:00"
        end = "2026-07-28T09:50:00-07:00"
        mapping = {a1: ("show.mp3", 10000), end: ("show.mp3", 10180)}
        with self._patch_resolver(mapping), mock.patch("builtins.print"):
            m.download_archive([play("Belly", "Dusted", a1)], end, shift=0, lead=0, tail=0)
        # end resolves to the SAME file → exact length 10180-10000 = 180
        self.assertEqual(ffmpeg_arg(self.calls[0], "-t"), "180.000")

    def test_last_track_diff_file_caps_duration(self):
        a1 = "2026-07-28T09:47:00-07:00"
        end = "2026-07-28T12:00:00-07:00"      # ~2h13m gap
        mapping = {a1: ("show.mp3", 10000), end: ("nextshow.mp3", 3)}
        with self._patch_resolver(mapping), mock.patch("builtins.print"):
            m.download_archive([play("Belly", "Dusted", a1)], end, shift=0, lead=0, tail=0)
        # Different file → falls back to airdate gap, capped at MAX_SONG_SEC.
        self.assertEqual(ffmpeg_arg(self.calls[0], "-t"), f"{float(m.MAX_SONG_SEC):.3f}")

    def test_skips_existing_file(self):
        a1, a2 = "2026-07-28T07:05:28-07:00", "2026-07-28T07:08:01-07:00"
        end = "2026-07-28T09:50:00-07:00"
        mapping = {a1: ("show.mp3", 922), a2: ("show.mp3", 1075),
                   end: ("nextshow.mp3", 5)}
        # Pre-create the second track's output so it should be skipped.
        existing = os.path.join(self.tmp, "Quivers - You're Not Always On My Mind.mp3")
        open(existing, "w").close()
        tracks = [play("R.E.M.", "Fall on Me", a1),
                  play("Quivers", "You're Not Always On My Mind", a2)]
        with self._patch_resolver(mapping), mock.patch("builtins.print"):
            m.download_archive(tracks, end)
        # Only the first track triggers an ffmpeg call.
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.calls[0][-1].endswith("R.E.M. - Fall on Me.mp3"))

    def test_lead_tail_padding_widens_window(self):
        a1, a2 = "2026-07-28T07:05:28-07:00", "2026-07-28T07:08:01-07:00"
        end = "2026-07-28T09:50:00-07:00"
        mapping = {a1: ("show.mp3", 922), a2: ("show.mp3", 1075),
                   end: ("nextshow.mp3", 5)}
        tracks = [play("R.E.M.", "Fall on Me", a1),
                  play("Quivers", "X", a2)]
        with self._patch_resolver(mapping), mock.patch("builtins.print"):
            m.download_archive(tracks, end, shift=0, lead=15, tail=10)
        # start = 922-15 = 907; length = (922+153+10) - 907 = 178
        self.assertEqual(ffmpeg_arg(self.calls[0], "-ss"), "907.000")
        self.assertEqual(ffmpeg_arg(self.calls[0], "-t"), "178.000")

    def test_lead_clamps_at_zero(self):
        a1 = "2026-07-28T07:00:05-07:00"
        end = "2026-07-28T07:03:00-07:00"
        mapping = {a1: ("show.mp3", 8), end: ("show.mp3", 188)}   # offset 8 < lead
        with self._patch_resolver(mapping), mock.patch("builtins.print"):
            m.download_archive([play("First", "Song", a1)], end, shift=0, lead=15, tail=10)
        # start clamps to 0; length = (8+180+10) - 0 = 198
        self.assertEqual(ffmpeg_arg(self.calls[0], "-ss"), "0.000")
        self.assertEqual(ffmpeg_arg(self.calls[0], "-t"), "198.000")

    def test_shift_moves_window_earlier(self):
        a1, a2 = "2026-07-28T07:05:28-07:00", "2026-07-28T07:08:01-07:00"
        end = "2026-07-28T09:50:00-07:00"
        mapping = {a1: ("show.mp3", 922), a2: ("show.mp3", 1075),
                   end: ("nextshow.mp3", 5)}
        tracks = [play("R.E.M.", "Fall on Me", a1), play("Quivers", "X", a2)]
        with self._patch_resolver(mapping), mock.patch("builtins.print"):
            m.download_archive(tracks, end, shift=42, lead=15, tail=8)
        # start = 922 - 42 - 15 = 865; end = 922 + 153 - 42 + 8 = 1041; len = 176
        self.assertEqual(ffmpeg_arg(self.calls[0], "-ss"), "865.000")
        self.assertEqual(ffmpeg_arg(self.calls[0], "-t"), "176.000")

    def test_filename_sanitizes_slash(self):
        a1 = "2026-07-28T07:05:28-07:00"
        end = "2026-07-28T09:50:00-07:00"
        mapping = {a1: ("show.mp3", 100), end: ("nextshow.mp3", 5)}
        with self._patch_resolver(mapping), mock.patch("builtins.print"):
            m.download_archive([play("Man/Woman/Chainsaw", "Nosedive", a1)], end)
        self.assertTrue(self.calls[0][-1].endswith(
            "Man⧸Woman⧸Chainsaw - Nosedive.mp3"))

    def test_skips_tracks_missing_metadata(self):
        a1 = "2026-07-28T07:05:28-07:00"
        end = "2026-07-28T09:50:00-07:00"
        mapping = {a1: ("show.mp3", 100), end: ("nextshow.mp3", 5)}
        tracks = [play("", "", a1), play(None, "Song", a1)]
        with self._patch_resolver(mapping), mock.patch("builtins.print"):
            m.download_archive(tracks, end)
        self.assertEqual(len(self.calls), 0)


class TestDownloadYoutube(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._dir_patch = mock.patch.object(m, "DOWNLOAD_DIR", self.tmp)
        self._dir_patch.start()
        # yt-dlp + ffmpeg both "present".
        self._which_patch = mock.patch.object(m.shutil, "which", return_value="/bin/x")
        self._which_patch.start()
        self.calls = []

    def tearDown(self):
        self._dir_patch.stop()
        self._which_patch.stop()

    def _outpath_from_cmd(self, cmd):
        # -o value is "<base>.%(ext)s"; the mp3 post-processor lands on "<base>.mp3".
        o = cmd[cmd.index("-o") + 1]
        return o.replace("%(ext)s", "mp3")

    def _patch_run(self, succeed=True):
        def fake(cmd, *a, **k):
            self.calls.append(cmd)
            if succeed:
                open(self._outpath_from_cmd(cmd), "w").close()
                return types.SimpleNamespace(returncode=0)
            return types.SimpleNamespace(returncode=1)  # no file, non-zero
        return mock.patch.object(m.subprocess, "run", side_effect=fake)

    def test_success_downloads_all(self):
        tracks = [play("R.E.M.", "Fall on Me", "t1"),
                  play("Belly", "Angel", "t2")]
        with self._patch_run(succeed=True), mock.patch("builtins.print"):
            m.download_youtube(tracks)   # should NOT raise
        self.assertEqual(len(self.calls), 2)
        # ytsearch1 query and our own output naming
        self.assertIn("ytsearch1:R.E.M. Fall on Me", self.calls[0])
        self.assertTrue(self.calls[0][self.calls[0].index("-o") + 1]
                        .endswith("R.E.M. - Fall on Me.%(ext)s"))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "Belly - Angel.mp3")))

    def test_any_failure_exits_nonzero(self):
        tracks = [play("R.E.M.", "Fall on Me", "t1")]
        with self._patch_run(succeed=False), mock.patch("builtins.print"):
            with self.assertRaises(SystemExit) as cm:
                m.download_youtube(tracks)
        self.assertEqual(cm.exception.code, 1)

    def test_skips_existing(self):
        open(os.path.join(self.tmp, "Belly - Angel.mp3"), "w").close()
        tracks = [play("Belly", "Angel", "t1")]
        with self._patch_run(succeed=True), mock.patch("builtins.print"):
            m.download_youtube(tracks)
        self.assertEqual(len(self.calls), 0)   # already had it → no yt-dlp call

    def test_skips_missing_metadata(self):
        tracks = [play("", "", "t1")]
        with self._patch_run(succeed=True), mock.patch("builtins.print"):
            m.download_youtube(tracks)   # no tracks with metadata → success, no calls
        self.assertEqual(len(self.calls), 0)


class TestFindLatestAiring(unittest.TestCase):
    SHOWS = {
        "results": [
            {"program_name": "The Midday Show", "start_time": "2026-07-28T09:50:00-07:00"},
            {"program_name": "The Morning Show", "start_time": "2026-07-28T06:50:00-07:00"},
            {"program_name": "The Morning Show", "start_time": "2026-07-27T06:50:00-07:00"},
            {"program_name": "Weekend Show",     "start_time": "2026-07-26T06:50:00-07:00"},
        ]
    }

    def test_picks_newest_aired_morning_show_and_next(self):
        # "now" is after the 7/28 morning show but before nothing else matters.
        now = datetime.fromisoformat("2026-07-28T12:00:00-07:00")
        with mock.patch.object(m, "get_json", return_value=self.SHOWS):
            show, nxt = m.find_latest_airing(now=now)
        self.assertEqual(show["start_time"], "2026-07-28T06:50:00-07:00")
        # next_show is the chronologically following show (the Midday Show).
        self.assertEqual(nxt["program_name"], "The Midday Show")

    def test_ignores_future_airings(self):
        # "now" is before the 7/28 morning show → newest *aired* is 7/27.
        now = datetime.fromisoformat("2026-07-28T06:00:00-07:00")
        with mock.patch.object(m, "get_json", return_value=self.SHOWS):
            show, _ = m.find_latest_airing(now=now)
        self.assertEqual(show["start_time"], "2026-07-27T06:50:00-07:00")

    def test_none_when_no_morning_show(self):
        shows = {"results": [{"program_name": "Weekend Show",
                              "start_time": "2026-07-26T06:50:00-07:00"}]}
        now = datetime.fromisoformat("2026-07-28T12:00:00-07:00")
        with mock.patch.object(m, "get_json", return_value=shows):
            show, nxt = m.find_latest_airing(now=now)
        self.assertIsNone(show)
        self.assertIsNone(nxt)


class TestWriteManifest(unittest.TestCase):
    def test_writes_expected_shape(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "playlist.json")
        tracks = [
            play("Man/Woman/Chainsaw", "Nosedive", "2026-07-28T07:15:00-07:00"),
            {"artist": "", "song": "", "airdate": "x", "play_type": "trackplay"},  # dropped
        ]
        tracks[0]["album"] = "Modern Job"
        m.write_manifest(path, "The Morning Show", "John Richards", "2026-07-28", tracks)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["date"], "2026-07-28")
        self.assertEqual(data["host"], "John Richards")
        self.assertEqual(len(data["tracks"]), 1)   # blank-metadata track dropped
        t = data["tracks"][0]
        self.assertEqual(t["file"], "Man⧸Woman⧸Chainsaw - Nosedive.mp3")
        self.assertEqual(t["album"], "Modern Job")


if __name__ == "__main__":
    unittest.main(verbosity=2)
