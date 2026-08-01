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

--archive slices each song straight out of KEXP's own 2-week streaming archive
(the actual broadcast, ~128 kbps), using the get_streaming_url resolver to map each
song's airdate to its position in the show's mp3, then ffmpeg to cut it. It saves to
downloads/ as "Artist - Song.mp3" using KEXP's clean artist/song strings, skips any
file already present, and prints a summary. Always the right recording — but it's
the live broadcast, so intros/outros can include DJ voiceover, and the DJ's logged
airdate is approximate so songs can get clipped. Only works within the ~2-week
archive window. Requires ffmpeg on PATH.
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


def write_manifest(path, program, host, date_str, tracks):
    """Write a per-day playlist.json alongside the ripped mp3s.

    Lists the show's intended tracks and the filename each maps to, so downstream
    consumers (or the S3 upload step) have a machine-readable record of the day.
    """
    items = []
    for p in tracks:
        artist = (p.get("artist") or "").strip()
        song = (p.get("song") or "").strip()
        if not artist or not song:
            continue
        items.append({
            "artist": artist,
            "song": song,
            "album": p.get("album") or None,
            "airdate": p.get("airdate"),
            "file": f"{safe_filename(f'{artist} - {song}')}.mp3",
        })
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
# Guard for the final-song duration fallback: no single play should be longer
# than this, so we never rip a runaway chunk if a show boundary is off.
MAX_SONG_SEC = 1200
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


def download_archive(tracks, end_iso, bitrate=256, location=3,
                     shift=42.0, lead=15.0, tail=8.0):
    """Rip each track straight from the KEXP archive as 'Artist - Song.mp3'.

    For every track we resolve its (show-file URL, in-file offset). A track's
    duration is the next track's offset minus its own when they share a file;
    at a show boundary (or the last track) we fall back to the airdate gap. Then
    ffmpeg stream-copies a window out of the show mp3.

    Audio analysis showed the DJ logs each play ~`shift` seconds AFTER the song
    actually starts (he talks over intros), with ~14s of variation. So we move the
    whole window earlier by `shift`, and `lead`/`tail` add margin for that residual
    so intros aren't clipped — at the cost of a little adjacent audio. The lag is
    only roughly constant, so this is a good-enough heuristic, not surgical.
    Note: this is the live broadcast, so intros/outros may include DJ voiceover.
    """
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found on PATH. Install it (e.g. `brew install ffmpeg`).")
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    # Resolve every track's file + offset up front (each is a small API call).
    print(f"resolving archive offsets for {len(tracks)} tracks ...")
    infos = []  # (play, sg_url, offset)
    for p in tracks:
        if not (p.get("artist") and p.get("song")):
            continue
        try:
            sg_url, off = resolve_stream(p["airdate"], bitrate, location)
        except Exception as e:
            print(f"  ! resolve failed for {p.get('artist')} - {p.get('song')}: {e}")
            continue
        infos.append((p, sg_url, off))

    ok, failed, skipped = 0, [], 0
    for i, (p, sg_url, off) in enumerate(infos):
        artist, song = p["artist"].strip(), p["song"].strip()
        label = f"{artist} - {song}"
        outfile = os.path.join(DOWNLOAD_DIR, f"{safe_filename(label)}.mp3")
        if os.path.exists(outfile):
            print(f"[{i+1}/{len(infos)}] already have: {label}")
            skipped += 1
            continue

        # Duration: prefer the offset delta within the same show file.
        if i + 1 < len(infos) and infos[i + 1][1] == sg_url:
            dur = infos[i + 1][2] - off
        else:
            # Last track in this file (usually the show's final song). Resolve the
            # show-end timestamp: if it lands in the SAME file, use that offset for
            # an exact length; otherwise the song runs to the file's end, so fall
            # back to the airdate gap — capped so a bad boundary can't rip a giant
            # chunk (a normal song is minutes, not tens of minutes).
            nxt = infos[i + 1][0]["airdate"] if i + 1 < len(infos) else end_iso
            dur = (datetime.fromisoformat(nxt)
                   - datetime.fromisoformat(p["airdate"])).total_seconds()
            try:
                end_url, end_off = resolve_stream(nxt, bitrate, location)
                if end_url == sg_url:
                    dur = end_off - off
            except Exception:
                pass
            dur = min(dur, MAX_SONG_SEC)
        if dur <= 0:
            print(f"[{i+1}/{len(infos)}] skip (bad duration): {label}")
            failed.append(label)
            continue

        # Shift the window earlier by `shift` (airdate logged late), plus lead/tail
        # margin for the residual, so the song isn't clipped. The song's true span
        # is ~[off-shift, off+dur-shift]; we widen it by lead/tail.
        start = max(0.0, off - shift - lead)
        end = off + dur - shift + tail
        length = max(1.0, end - start)
        print(f"[{i+1}/{len(infos)}] {label}  (@{int(off)}s dur {int(dur)}s, -{shift:g}s shift +{lead:g}/{tail:g})")
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-ss", f"{start:.3f}", "-t", f"{length:.3f}",
            "-i", sg_url, "-c", "copy", outfile,
        ]
        res = subprocess.run(cmd)
        if res.returncode == 0 and os.path.exists(outfile):
            ok += 1
        else:
            failed.append(label)

    print("\n========== SUMMARY (archive) ==========")
    print(f"ripped: {ok}   already had: {skipped}   failed: {len(failed)}   of {len(infos)}")
    if failed:
        print("failed:")
        for label in failed:
            print("  -", label)
    print(f"saved to: {DOWNLOAD_DIR}")


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
    args = [a for a in args if a not in ("--csv", "--archive", "--youtube", "--latest")]

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
        download_archive(tracks, end_iso)
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        write_manifest(os.path.join(DOWNLOAD_DIR, "playlist.json"),
                       PROGRAM_NAME, host, str(target_date), tracks)
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
