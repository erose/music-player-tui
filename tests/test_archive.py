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


class TestComputeCuts(unittest.TestCase):
    """The partition invariants — these are the whole point of the design.

    Assertions are stated as properties (contiguity, coverage) rather than magic
    numbers, so they keep holding if the boundary *placement* is ever retuned.
    """

    def test_windows_are_contiguous(self):
        cuts = m.compute_cuts([100, 300, 500], 0.0, 700.0, delay=10)
        for a, b in zip(cuts, cuts[1:]):
            self.assertEqual(a["end"], b["start"])   # one number, used twice

    def test_no_overlap_and_no_gap(self):
        # The regression this whole change exists for: the old code emitted a
        # constant lead+tail (23s) of duplicated audio at every boundary.
        cuts = m.compute_cuts([100, 300, 500], 0.0, 700.0, delay=37)
        for a, b in zip(cuts, cuts[1:]):
            self.assertEqual(b["start"] - a["end"], 0.0)

    def test_covers_the_whole_show(self):
        cuts = m.compute_cuts([100, 300, 500], 0.0, 700.0, delay=37)
        self.assertEqual(cuts[0]["start"], 0.0)      # show open folded into T1
        self.assertEqual(cuts[-1]["end"], 700.0)     # close folded into the last
        total = sum(c["end"] - c["start"] for c in cuts)
        self.assertAlmostEqual(total, 700.0)         # every second accounted for

    def test_boundaries_use_delay(self):
        cuts = m.compute_cuts([100, 300], 0.0, 700.0, delay=37)
        self.assertEqual(cuts[1]["start"], 300 + 37)

    def test_out_of_order_airdate_merges_instead_of_dropping(self):
        # Second offset lands before the first boundary. The old code hit
        # `dur <= 0` and skipped the track, losing that audio entirely.
        cuts = m.compute_cuts([100, 300, 200], 0.0, 700.0, delay=37)
        self.assertEqual(cuts[1]["end"], cuts[1]["start"])       # collapsed
        for a, b in zip(cuts, cuts[1:]):
            self.assertEqual(a["end"], b["start"])               # still contiguous
        self.assertAlmostEqual(sum(c["end"] - c["start"] for c in cuts), 700.0)

    def test_boundaries_clamped_inside_the_show(self):
        # A wild offset can't push a window outside the show's span.
        cuts = m.compute_cuts([100, 99999], 0.0, 700.0, delay=37)
        for c in cuts:
            self.assertGreaterEqual(c["start"], 0.0)
            self.assertLessEqual(c["end"], 700.0)

    def test_empty(self):
        self.assertEqual(m.compute_cuts([], 0.0, 700.0, delay=37), [])


class TestDownloadArchive(unittest.TestCase):
    # Chosen so the show sits comfortably inside the chunk: the chunk begins at
    # A1-922 = 06:50:06, so the 06:50:30 show start is 24s into it.
    START = "2026-07-28T06:50:30-07:00"
    END = "2026-07-28T07:30:00-07:00"
    A1 = "2026-07-28T07:05:28-07:00"
    A2 = "2026-07-28T07:08:01-07:00"
    A3 = "2026-07-28T07:11:38-07:00"
    FILE_DUR = 2500.0
    # Expected boundaries as offsets in the chunk: show start, A2+delay,
    # A3+delay, show end.
    B0, B1, B2, B3 = 24.0, 1112.0, 1329.0, 2394.0

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # Redirect output into a temp dir so we never touch real downloads/;
        # ffmpeg/ffprobe "present" without needing them installed; chunk
        # durations answered without touching the network.
        self._patches = [
            mock.patch.object(m, "DOWNLOAD_DIR", self.tmp),
            mock.patch.object(m.shutil, "which", return_value="/bin/ffmpeg"),
            mock.patch.object(m, "probe_duration", side_effect=lambda t: self.FILE_DUR),
        ]
        for p in self._patches:
            p.start()

        # Record every ffmpeg invocation and fake a successful rip by touching
        # the output file (download_archive checks the file exists afterwards).
        self.calls = []

        def fake_run(cmd, *a, **k):
            self.calls.append(cmd)
            open(cmd[-1], "w").close()
            return types.SimpleNamespace(returncode=0)

        self._run_patch = mock.patch.object(m.subprocess, "run", side_effect=fake_run)
        self._run_patch.start()

    def tearDown(self):
        self._run_patch.stop()
        for p in self._patches:
            p.stop()

    def _patch_resolver(self, mapping):
        """Patch resolve_stream to look up (url, offset) by timestamp string."""
        def fake(ts, bitrate=256, location=3):
            return mapping[ts]
        return mock.patch.object(m, "resolve_stream", side_effect=fake)

    def _standard(self):
        """Three tracks in one show file; the show's end is in the next file."""
        mapping = {
            self.START: ("show.mp3", 0),
            self.A1: ("show.mp3", 922),
            self.A2: ("show.mp3", 1075),
            self.A3: ("show.mp3", 1292),
            self.END: ("nextshow.mp3", 5),
        }
        tracks = [play("R.E.M.", "Fall on Me", self.A1),
                  play("Quivers", "You're Not Always On My Mind", self.A2),
                  play("10,000 Maniacs", "These Are Days", self.A3)]
        return mapping, tracks

    def _windows(self):
        """(start, end) actually handed to ffmpeg, in call order."""
        out = []
        for c in self.calls:
            start = float(ffmpeg_arg(c, "-ss"))
            out.append((start, start + float(ffmpeg_arg(c, "-t"))))
        return out

    def test_emitted_windows_are_contiguous(self):
        mapping, tracks = self._standard()
        with self._patch_resolver(mapping), mock.patch("builtins.print"):
            m.download_archive(tracks, self.START, self.END)
        wins = self._windows()
        self.assertEqual(len(wins), 3)
        for (_, end), (start, _) in zip(wins, wins[1:]):
            self.assertAlmostEqual(end, start, places=3)   # no repeat, no gap

    def test_covers_the_whole_show(self):
        mapping, tracks = self._standard()
        with self._patch_resolver(mapping), mock.patch("builtins.print"):
            m.download_archive(tracks, self.START, self.END)
        wins = self._windows()
        self.assertAlmostEqual(wins[0][0], self.B0, places=3)    # show open
        self.assertAlmostEqual(wins[-1][1], self.B3, places=3)   # show close
        total = sum(e - s for s, e in wins)
        self.assertAlmostEqual(total, self.B3 - self.B0, places=3)

    def test_cuts_stream_from_the_chunk_url(self):
        mapping, tracks = self._standard()
        with self._patch_resolver(mapping), mock.patch("builtins.print"):
            m.download_archive(tracks, self.START, self.END)
        self.assertEqual(ffmpeg_arg(self.calls[0], "-i"), "show.mp3")
        self.assertTrue(self.calls[0][-1].endswith("R.E.M. - Fall on Me.mp3"))

    def test_overlapping_chunks_do_not_duplicate_at_the_seam(self):
        """The real-world case: archive chunks overlap in broadcast time.

        The morning-show chunk keeps running past the point where the next
        show's chunk begins, so the same minutes exist in both. Cutting each
        chunk separately hands that stretch to two different tracks — the very
        repetition this design exists to prevent.
        """
        mapping, tracks = self._standard()
        # Third track resolves into a LATER chunk that starts after the boundary
        # between tracks 2 and 3, so only the first chunk can supply that audio.
        # A boundary can only precede the track's own chunk when it sits BEFORE
        # the airdate, which the default (positive) delay never produces, so
        # this pins a negative one to keep the fallback exercised.
        mapping[self.A3] = ("midday.mp3", 20)
        with self._patch_resolver(mapping), mock.patch("builtins.print"):
            cuts = m.download_archive(tracks, self.START, self.END, delay=-42.0)
        wins = self._windows()
        # Every window still comes from a chunk that holds it, and consecutive
        # windows meet exactly — no repeated seconds across the chunk seam.
        for (_, end), (start, _) in zip(wins, wins[1:]):
            self.assertAlmostEqual(end, start, places=3)
        self.assertEqual(cuts[self.A3]["source"], "show.mp3")
        total = sum(e - s for s, e in wins)
        self.assertAlmostEqual(total, self.B3 - self.B0, places=3)

    def test_prefers_the_tracks_own_chunk_when_it_covers_the_window(self):
        mapping, tracks = self._standard()
        with self._patch_resolver(mapping), mock.patch("builtins.print"):
            cuts = m.download_archive(tracks, self.START, self.END)
        for airdate in (self.A1, self.A2, self.A3):
            self.assertEqual(cuts[airdate]["source"], "show.mp3")

    def test_returns_cut_info_for_the_manifest(self):
        mapping, tracks = self._standard()
        with self._patch_resolver(mapping), mock.patch("builtins.print"):
            cuts = m.download_archive(tracks, self.START, self.END)
        self.assertEqual(set(cuts), {self.A1, self.A2, self.A3})
        first = cuts[self.A1]
        self.assertEqual(first["start_offset"], self.B0)
        self.assertEqual(first["source"], "show.mp3")
        self.assertAlmostEqual(first["duration"],
                               first["end_offset"] - first["start_offset"], places=3)

    def test_skips_existing_file(self):
        mapping, tracks = self._standard()
        existing = os.path.join(self.tmp, "Quivers - You're Not Always On My Mind.mp3")
        open(existing, "w").close()
        with self._patch_resolver(mapping), mock.patch("builtins.print"):
            m.download_archive(tracks, self.START, self.END)
        self.assertEqual(len(self.calls), 2)   # the pre-existing one is skipped
        self.assertFalse(any(c[-1] == existing for c in self.calls))

    def test_recut_overwrites_existing(self):
        mapping, tracks = self._standard()
        existing = os.path.join(self.tmp, "Quivers - You're Not Always On My Mind.mp3")
        open(existing, "w").close()
        with self._patch_resolver(mapping), mock.patch("builtins.print"):
            m.download_archive(tracks, self.START, self.END, recut=True)
        self.assertEqual(len(self.calls), 3)   # stale cut is replaced

    def test_collapsed_window_is_skipped_not_ripped(self):
        # Boundaries come from AIRDATES, so it takes an out-of-order airdate --
        # not merely an out-of-order offset -- to collapse a window.
        mapping, _ = self._standard()
        tracks = [play("R.E.M.", "Fall on Me", self.A1),
                  play("10,000 Maniacs", "These Are Days", self.A3),
                  play("Quivers", "Out Of Order", self.A2)]
        with self._patch_resolver(mapping), mock.patch("builtins.print"):
            m.download_archive(tracks, self.START, self.END)
        self.assertEqual(len(self.calls), 2)     # the collapsed one is not ripped
        wins = self._windows()
        for start, end in wins:
            self.assertGreaterEqual(end - start, m.MIN_TRACK_SEC)
        # The audio it would have held is absorbed by its neighbour, not lost.
        self.assertAlmostEqual(sum(e - s for s, e in wins),
                               self.B3 - self.B0, places=3)

    def test_filename_sanitizes_slash(self):
        mapping = {self.START: ("show.mp3", 0), self.A1: ("show.mp3", 100),
                   self.END: ("nextshow.mp3", 5)}
        with self._patch_resolver(mapping), mock.patch("builtins.print"):
            m.download_archive([play("Man/Woman/Chainsaw", "Nosedive", self.A1)],
                               self.START, self.END)
        self.assertTrue(self.calls[0][-1].endswith(
            "Man⧸Woman⧸Chainsaw - Nosedive.mp3"))

    def test_skips_tracks_missing_metadata(self):
        mapping = {self.START: ("show.mp3", 0), self.A1: ("show.mp3", 100),
                   self.END: ("nextshow.mp3", 5)}
        tracks = [play("", "", self.A1), play(None, "Song", self.A1)]
        with self._patch_resolver(mapping), mock.patch("builtins.print"):
            m.download_archive(tracks, self.START, self.END)
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
        self.assertNotIn("cut", t)   # no cut info supplied → key omitted

    def test_records_cut_spans_when_supplied(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "playlist.json")
        airdate = "2026-07-28T07:15:00-07:00"
        tracks = [play("Belly", "Dusted", airdate)]
        cuts = {airdate: {"start_offset": 100.0, "end_offset": 280.5,
                          "duration": 180.5, "source": "show.mp3"}}
        m.write_manifest(path, "The Morning Show", "John Richards",
                         "2026-07-28", tracks, cuts)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["tracks"][0]["cut"]["start_offset"], 100.0)
        self.assertEqual(data["tracks"][0]["cut"]["source"], "show.mp3")


if __name__ == "__main__":
    unittest.main(verbosity=2)
