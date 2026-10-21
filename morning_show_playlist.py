#!/usr/bin/env python3
"""
Fetch today's playlist for KEXP's "The Morning Show" (host John Richards).

KEXP's public API (https://api.kexp.org/v2/) drives kexp.org's playlist pages.
Note: the API's `show=` filter on /plays/ is unreliable, so this script instead
finds the show's airing for the chosen day and pulls every play that falls
inside that airing's time window.

Usage:
    python3 morning_show_playlist.py              # today (US/Pacific)
    python3 morning_show_playlist.py 2026-06-22   # a specific date (YYYY-MM-DD)
    python3 morning_show_playlist.py --csv        # output as CSV instead of a list
    python3 morning_show_playlist.py --youtube    # download each song from YouTube (mp3)
    python3 morning_show_playlist.py --archive    # rip songs from KEXP's broadcast archive
    python3 morning_show_playlist.py --youtube --latest  # download the most recent airing (for cron)

--youtube downloads each song from YouTube (top search hit) as an audio-only mp3
into downloads/ as "Artist - Song.mp3". This gives clean, full studio tracks. It is
STRICT: if any track fails to download it exits non-zero, so an automated job fails
loudly rather than silently. Requires yt-dlp + ffmpeg on PATH. (Note: YouTube may
bot-block datacenter IPs, so this can be flaky on CI runners.)

--archive slices the show straight out of KEXP's own 2-week streaming archive (the
actual broadcast, ~128 kbps), using the get_streaming_url resolver to map each song's
airdate to its position in the show's mp3, then ffmpeg to cut it. It saves to
downloads/ as "Artist - Song.mp3" using KEXP's clean artist/song strings, skips any
file already present, and prints a summary.

The show is cut into a CONTIGUOUS PARTITION: each boundary is a single instant used
as both the end of one track and the start of the next, so playing the day through
in order reproduces the broadcast exactly — no repeated audio, nothing dropped. The
first track absorbs the show's opening and the last runs to the show's end, so the
whole airing is covered. Because it's the live broadcast, tracks include the DJ
talking over intros and outros; that's the material, not a defect.

Only works within the ~2-week archive window. Requires ffmpeg (and ffprobe) on PATH.

  --recut   re-cut tracks even if the mp3 already exists (use after changing cut
            settings; the default skip-if-present would keep stale cuts)
"""

import os
import re
import sys
import csv
import json
import shutil
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

API = "https://api.kexp.org/v2"
PACIFIC = ZoneInfo("America/Los_Angeles")
PROGRAM_NAME = "The Morning Show"


def get_json(path, **params):
    url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "morning-show-playlist/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def find_airing(target_date):
    """Return (this_show, next_show) for The Morning Show on target_date (a date)."""
    # Pull a generous window of recent shows and search locally.
    shows = get_json("shows/", ordering="-start_time", limit=60)["results"]
    # Sort chronologically so we can find the show that airs *after* the Morning Show.
    shows.sort(key=lambda s: s["start_time"])
    for i, s in enumerate(shows):
        start = datetime.fromisoformat(s["start_time"]).astimezone(PACIFIC)
        if s.get("program_name") == PROGRAM_NAME and start.date() == target_date:
            next_show = shows[i + 1] if i + 1 < len(shows) else None
            return s, next_show
    return None, None


def find_latest_airing(now=None):
    """Return (this_show, next_show) for the most recent Morning Show that has aired.

    For unattended/scheduled runs: rather than assume "today" (which breaks on
    weekends, holidays, or preemptions), find the newest Morning Show airing whose
    start time is already in the past. `now` is injectable for testing.
    """
    now = now or datetime.now(timezone.utc)
    shows = get_json("shows/", ordering="-start_time", limit=60)["results"]
    shows.sort(key=lambda s: s["start_time"])
    latest = None
    for i, s in enumerate(shows):
        start = datetime.fromisoformat(s["start_time"])
        if s.get("program_name") == PROGRAM_NAME and start <= now:
            latest = i
    if latest is None:
        return None, None
    next_show = shows[latest + 1] if latest + 1 < len(shows) else None
    return shows[latest], next_show


def fetch_plays(start_iso, end_iso):
    """Fetch every play with start_iso <= airdate < end_iso.

    Note: the KEXP API returns a `next` link on every response regardless of
    whether more data exists, so we can't trust it to decide when to stop.
    Instead we page only while a full `limit` of results comes back. A single
    page of 300 already covers any normal show, so this almost never loops.
    """
    limit = 300
    plays = []
    offset = 0
    while True:
        data = get_json("plays/", airdate_after=start_iso, airdate_before=end_iso,
                        ordering="airdate", limit=limit, offset=offset)
        batch = data["results"]
        plays.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
    return plays


DOWNLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")


def safe_filename(name):
    """Make a string safe to use as a filename component.

    macOS/APFS only forbids '/' (and NUL), but ':' is remapped by Finder, so we
    replace both. '/' becomes '⧸' (U+29F8) so multi-part names like
    'Man/Woman/Chainsaw' stay readable rather than collapsing to one word.
    """
    name = (name or "").replace("/", "⧸").replace(":", "-")
    name = re.sub(r'[\x00-\x1f]', "", name)          # strip control chars
    return name.strip().rstrip(".")                   # no trailing dots/space


def write_manifest(path, program, host, date_str, tracks, cuts=None):
    """Write a per-day playlist.json alongside the ripped mp3s.

    Lists the show's intended tracks and the filename each maps to, so downstream
    consumers (or the S3 upload step) have a machine-readable record of the day.
    When `cuts` is supplied (archive rips) each entry also records the exact span
    of the broadcast it came from, which makes a day's cuts auditable and lets a
    later run tell stale files from current ones.
    """
    items = []
    for p in tracks:
        artist = (p.get("artist") or "").strip()
        song = (p.get("song") or "").strip()
        if not artist or not song:
            continue
        item = {
            "artist": artist,
            "song": song,
            "album": p.get("album") or None,
            "airdate": p.get("airdate"),
            "file": f"{safe_filename(f'{artist} - {song}')}.mp3",
        }
        cut = (cuts or {}).get(p.get("airdate"))
        if cut:
            item["cut"] = cut
        items.append(item)
    manifest = {"program": program, "host": host, "date": date_str, "tracks": items}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)


# --- Archive extraction: rip songs directly from KEXP's broadcast archive ---
#
# KEXP's 2-week streaming archive stores each show as one mp3 on a StreamGuys
# CDN. The resolver endpoint below turns a UTC timestamp into that show's file
# URL plus the exact offset (in seconds) of that moment within the file:
#
#   GET https://api.kexp.org/get_streaming_url/?bitrate=..&timestamp=..&location=3
#   -> {"sg-url": "...show.mp3", "sg-url-next": "...", "sg-offset": <int seconds>}
#
# It 403s without a browser-style Referer. Given each song's airdate we get its
# in-file offset; the next song's offset (minus this one) is the song's length,
# so ffmpeg can slice it out with a stream copy — no YouTube, no re-encode.

STREAM_RESOLVER = "https://api.kexp.org/get_streaming_url/"
# Where a song starts relative to its logged airdate, on the archive timeline
# the resolver maps into. Positive: the song begins AFTER the airdate. Measured
# 2026-10-20 by aligning repeat airings of the same recording (two broadcasts
# match only while the song plays): John Richards' Morning Show sat at a median
# of +37s on every day measured, songs mostly within ±4s. The old value was
# `airdate - 42`, which cut ~79s early. Other hosts measured +18..+40s.
START_DELAY_SEC = 37.0
# A window this short means its boundaries collapsed (duplicate/out-of-order
# airdates); there's no audio worth writing, so we report it instead.
MIN_TRACK_SEC = 1.0
# The archive host only serves ~128 kbps regardless of the bitrate asked for,
# but we pass what the site passes. location=3 is what kexp.org/archive uses.
ARCHIVE_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://www.kexp.org/",
    "Origin": "https://www.kexp.org",
}


def _to_utc_z(iso):
    """'2026-07-28T07:05:28-07:00' -> '2026-07-28T14:05:28Z' (what the resolver wants)."""
    return datetime.fromisoformat(iso).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_stream(airdate_iso, bitrate=256, location=3):
    """Return (sg_url, sg_offset_seconds) for the broadcast moment at airdate_iso."""
    params = urllib.parse.urlencode({
        "bitrate": bitrate, "timestamp": _to_utc_z(airdate_iso), "location": location})
    req = urllib.request.Request(f"{STREAM_RESOLVER}?{params}", headers=ARCHIVE_HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    return data["sg-url"], int(data["sg-offset"])


def probe_duration(target):
    """Return the duration of `target` in seconds, or None if ffprobe can't say.

    `target` may be a local path or a URL — the chunks are probed in place, so
    nothing has to be downloaded just to learn how long a chunk runs.
    """
    if not shutil.which("ffprobe"):
        return None
    res = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", target],
        capture_output=True, text=True)
    try:
        return float(res.stdout.strip())
    except (ValueError, AttributeError):
        return None


def compute_cuts(times, start_anchor, end_anchor, delay):
    """Partition [start_anchor, end_anchor] into one contiguous window per track.

    Returns [{"start", "end"}] where window i's end is EXACTLY window i+1's start
    — one number used twice, not two computations that happen to be close. That
    identity is what makes repeated or dropped audio impossible, and it holds no
    matter how the boundaries themselves are placed.

    `times` are positions on a single timeline; callers pass broadcast wall-clock
    seconds rather than in-file offsets, because the archive's mp3 chunks OVERLAP
    (a show's chunk runs past the point where the next one starts). Partitioning
    per file would therefore duplicate the overlapping stretch — the exact bug
    this function exists to prevent.

    Boundaries sit at `time + delay`: on the archive's timeline a song starts
    ~delay seconds AFTER its logged airdate (see START_DELAY_SEC). The true
    delay varies a few seconds per song and more between hosts, which this
    doesn't try to correct, so a cut can sit a few seconds off the true seam.
    It lands in the adjacent track rather than being duplicated or lost. The
    first window begins at `start_anchor` and the last ends at `end_anchor`, so
    the show's open and close land in the adjacent tracks instead of falling
    outside the partition.
    """
    n = len(times)
    if n == 0:
        return []
    end_anchor = max(end_anchor, start_anchor)

    bounds = [start_anchor] + [t + delay for t in times[1:]] + [end_anchor]

    # Keep every boundary inside the show, then force the list non-decreasing.
    # A duplicate or out-of-order airdate then collapses its own window to zero
    # length and merges into its neighbour, rather than producing a negative span
    # (or, as before, silently dropping that stretch of the broadcast).
    for i, b in enumerate(bounds):
        bounds[i] = min(max(b, start_anchor), end_anchor)
    for i in range(1, len(bounds)):
        bounds[i] = max(bounds[i], bounds[i - 1])

    return [{"start": bounds[i], "end": bounds[i + 1]} for i in range(n)]


def download_archive(tracks, start_iso, end_iso, bitrate=256, location=3,
                     delay=None, recut=False):
    """Rip the show from the KEXP archive as one 'Artist - Song.mp3' per track.

    Resolves each play to a (chunk URL, in-file offset), partitions the show into
    contiguous windows via compute_cuts, and stream-copies each one out with
    ffmpeg. Returns {airdate: cut-info} so the manifest can record exactly where
    every track was taken from.
    """
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found on PATH. Install it (e.g. `brew install ffmpeg`).")
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    # Resolve every track's file + offset up front (each is a small API call).
    # `file_start` is the broadcast instant that file offset 0 corresponds to,
    # which is what lets us move between file offsets and wall-clock time.
    print(f"resolving archive offsets for {len(tracks)} tracks ...")
    infos = []  # (play, sg_url, offset, airtime, file_start)
    for p in tracks:
        if not (p.get("artist") and p.get("song")):
            continue
        try:
            sg_url, off = resolve_stream(p["airdate"], bitrate, location)
        except Exception as e:
            print(f"  ! resolve failed for {p.get('artist')} - {p.get('song')}: {e}")
            continue
        airtime = datetime.fromisoformat(p["airdate"]).timestamp()
        infos.append((p, sg_url, off, airtime, airtime - off))
    if not infos:
        print("no tracks with usable metadata — nothing to rip.")
        return {}

    labels = [f"{p['artist'].strip()} - {p['song'].strip()}" for p, *_ in infos]
    show_start = datetime.fromisoformat(start_iso).timestamp()
    show_end = datetime.fromisoformat(end_iso).timestamp()

    # Each distinct archive chunk's wall-clock extent. A show routinely spans two,
    # and the chunks OVERLAP (a show's own chunk keeps running well past the point
    # where the next show's chunk begins), which is why boundaries live on the
    # wall-clock timeline instead of in any one file's offsets. Chunks are probed
    # in place — nothing is downloaded except the slices ffmpeg cuts.
    chunks = {}                    # url -> {"start", "dur"}
    for _, url, _, _, file_start in infos:
        if url not in chunks:
            chunks[url] = {"start": file_start, "dur": probe_duration(url)}

    def covers(entry, start, end):
        """Does this chunk contain the whole wall-clock span [start, end]?"""
        return (entry is not None and entry["dur"] is not None
                and entry["start"] <= start
                and end <= entry["start"] + entry["dur"])

    def pick_chunk(url, start, end):
        """Choose a chunk holding this whole window, preferring the track's own.

        Chunks overlap, so a boundary can land before the chunk a track resolved
        to while still being present in the previous one. Picking a covering
        chunk keeps the seam lossless instead of clamping a window to a chunk
        edge.
        """
        if covers(chunks.get(url), start, end):
            return url, chunks[url]
        for u, entry in chunks.items():
            if covers(entry, start, end):
                return u, entry
        return url, chunks.get(url)

    if delay is None:
        delay = START_DELAY_SEC
    windows = compute_cuts([inf[3] for inf in infos], show_start, show_end, delay)

    cuts = {}
    ok, failed, skipped, done, clipped = 0, [], 0, 0, []
    for (p, url, _, _, _), win, label in zip(infos, windows, labels):
        done += 1
        # Wall-clock boundaries -> offsets in the chosen chunk. Both sides of
        # a seam map the SAME instant, so overlapping chunks can no longer
        # hand the same stretch of broadcast to two different tracks.
        src, entry = pick_chunk(url, win["start"], win["end"])
        start = win["start"] - entry["start"] if entry else 0.0
        end = win["end"] - entry["start"] if entry else 0.0
        file_dur = entry["dur"] if entry else None
        lo = max(0.0, start)
        hi = min(end, file_dur) if file_dur else end
        if (lo - start) + (end - hi) > 1.0:
            clipped.append(label)     # window ran off the end of the chunk
        length = hi - lo
        cuts[p.get("airdate")] = {
            "start_offset": round(lo, 3),
            "end_offset": round(hi, 3),
            "duration": round(max(0.0, length), 3),
            "start_utc": datetime.fromtimestamp(
                win["start"], timezone.utc).isoformat(),
            "end_utc": datetime.fromtimestamp(
                win["end"], timezone.utc).isoformat(),
            "source": src,
        }
        outfile = os.path.join(DOWNLOAD_DIR, f"{safe_filename(label)}.mp3")
        if entry is None:
            failed.append(label)
            continue
        if length < MIN_TRACK_SEC:
            print(f"[{done}/{len(infos)}] skip (window collapsed): {label}")
            failed.append(label)
            continue
        if os.path.exists(outfile) and not recut:
            print(f"[{done}/{len(infos)}] already have: {label}")
            skipped += 1
            continue
        print(f"[{done}/{len(infos)}] {label}  "
              f"({lo:.1f}s → {hi:.1f}s, {length:.1f}s)")
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-ss", f"{lo:.3f}", "-t", f"{length:.3f}",
            "-i", src, "-c", "copy", outfile,
        ]
        res = subprocess.run(cmd)
        if res.returncode == 0 and os.path.exists(outfile):
            ok += 1
        else:
            failed.append(label)

    print("\n========== SUMMARY (archive) ==========")
    print(f"ripped: {ok}   already had: {skipped}   failed: {len(failed)}   of {len(infos)}")
    if clipped:
        print(f"windows clipped at a chunk edge: {len(clipped)}")
        for label in clipped:
            print("  -", label)
    if failed:
        print("failed:")
        for label in failed:
            print("  -", label)
    print(f"saved to: {DOWNLOAD_DIR}")
    return cuts


def download_youtube(tracks):
    """Download each track from YouTube as 'Artist - Song.mp3' (audio-only mp3).

    Strict by design: if ANY track fails to download, exit non-zero so the CI job
    fails loudly. There's no archive fallback — we want to *see* when YouTube
    breaks (e.g. bot-blocking on CI runners) rather than silently degrade.
    """
    if not shutil.which("yt-dlp"):
        sys.exit("yt-dlp not found on PATH. Install it (e.g. `pip install yt-dlp`).")
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found on PATH. Install it (e.g. `brew install ffmpeg`).")
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    ok, failed, skipped = 0, [], 0
    total = len(tracks)
    for i, p in enumerate(tracks, 1):
        artist = (p.get("artist") or "").strip()
        song = (p.get("song") or "").strip()
        if not artist or not song:
            continue
        label = f"{artist} - {song}"
        base = os.path.join(DOWNLOAD_DIR, safe_filename(label))
        outfile = base + ".mp3"
        if os.path.exists(outfile):
            print(f"[{i}/{total}] already have: {label}")
            skipped += 1
            continue

        print(f"[{i}/{total}] youtube: {label}")
        # -o names the file ourselves; %(ext)s lets the mp3 post-processor land on
        # <base>.mp3. ytsearch1 takes the top search hit for "artist song".
        cmd = [
            "yt-dlp", "--no-playlist", "--no-progress",
            "-x", "--audio-format", "mp3", "--audio-quality", "0",
            "-o", f"{base}.%(ext)s",
            f"ytsearch1:{artist} {song}",
        ]
        res = subprocess.run(cmd)
        if res.returncode == 0 and os.path.exists(outfile):
            ok += 1
        else:
            failed.append(label)

    print("\n========== SUMMARY (youtube) ==========")
    print(f"downloaded: {ok}   already had: {skipped}   failed: {len(failed)}   of {total}")
    if failed:
        print("FAILED (exiting non-zero so the job fails):")
        for label in failed:
            print("  -", label)
        sys.exit(1)
    print(f"saved to: {DOWNLOAD_DIR}")


def main():
    args = [a for a in sys.argv[1:]]
    as_csv = "--csv" in args
    do_archive = "--archive" in args
    do_youtube = "--youtube" in args
    do_latest = "--latest" in args
    do_recut = "--recut" in args
    args = [a for a in args if a not in ("--csv", "--archive", "--youtube",
                                         "--latest", "--recut")]

    if do_latest:
        show, next_show = find_latest_airing()
        if not show:
            sys.exit(f"No recent '{PROGRAM_NAME}' airing found in the API window.")
        target_date = datetime.fromisoformat(show["start_time"]).astimezone(PACIFIC).date()
    else:
        if args:
            try:
                target_date = datetime.strptime(args[0], "%Y-%m-%d").date()
            except ValueError:
                sys.exit(f"Invalid date '{args[0]}'. Use YYYY-MM-DD.")
        else:
            target_date = datetime.now(PACIFIC).date()

        show, next_show = find_airing(target_date)
        if not show:
            sys.exit(f"No '{PROGRAM_NAME}' airing found for {target_date}. "
                     "(The API only keeps a rolling window of recent shows, so this "
                     "works for the last couple of weeks, not arbitrary past dates.)")

    start_iso = show["start_time"]
    # End boundary = start of the next show, or +4h if it's the most recent airing.
    if next_show:
        end_iso = next_show["start_time"]
    else:
        end_iso = (datetime.fromisoformat(start_iso) + timedelta(hours=4)).isoformat()

    plays = fetch_plays(start_iso, end_iso)
    tracks = [p for p in plays if p.get("play_type") == "trackplay"]

    host = ", ".join(show.get("host_names") or [])

    if do_youtube:
        print(f"{PROGRAM_NAME} — {host}")
        print(f"{target_date}  ({len(tracks)} songs) → downloading from YouTube\n")
        download_youtube(tracks)
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        write_manifest(os.path.join(DOWNLOAD_DIR, "playlist.json"),
                       PROGRAM_NAME, host, str(target_date), tracks)
        return

    if do_archive:
        print(f"{PROGRAM_NAME} — {host}")
        print(f"{target_date}  ({len(tracks)} songs) → ripping from KEXP archive\n")
        cuts = download_archive(tracks, start_iso, end_iso, recut=do_recut)
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        write_manifest(os.path.join(DOWNLOAD_DIR, "playlist.json"),
                       PROGRAM_NAME, host, str(target_date), tracks, cuts)
        return

    if as_csv:
        writer = csv.writer(sys.stdout)
        writer.writerow(["time_pacific", "artist", "song", "album"])
        for p in tracks:
            t = datetime.fromisoformat(p["airdate"]).astimezone(PACIFIC).strftime("%H:%M")
            writer.writerow([t, p.get("artist") or "", p.get("song") or "",
                             p.get("album") or ""])
        return

    print(f"{PROGRAM_NAME} — {host}")
    print(f"{target_date}  ({len(tracks)} songs)")
    print("=" * 60)
    for i, p in enumerate(tracks, 1):
        t = datetime.fromisoformat(p["airdate"]).astimezone(PACIFIC).strftime("%H:%M")
        artist = p.get("artist") or "?"
        song = p.get("song") or "?"
        print(f"{i:>2}. [{t}] {artist} — {song}")


if __name__ == "__main__":
    main()
