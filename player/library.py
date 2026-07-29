"""S3 access: discover dates, load playlists, mint presigned URLs.

Nothing here downloads audio. The only objects fetched are playlist.json
files (a few KB); mp3s are only ever referenced by presigned URL.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import boto3
from botocore.exceptions import ClientError

BUCKET = "elis-music"
PREFIX = "kexp-morning-show-rolling-downloads"
REGION = "us-east-2"

# Presigned URLs are minted at play time. Tracks run a few minutes, but a
# generous window means a paused track can sit untouched and still resume.
URL_TTL_SECONDS = 6 * 60 * 60
# Re-sign once a cached URL has less than this left, so a long-paused track
# never resumes against an expired signature.
URL_REFRESH_MARGIN = 15 * 60


@dataclass
class Track:
    artist: str
    song: str
    album: str
    airdate: str
    filename: str
    key: str
    available: bool = True

    @property
    def title(self) -> str:
        return f"{self.artist} — {self.song}"


@dataclass
class Day:
    date: str
    prefix: str
    mp3_keys: list[str] = field(default_factory=list)
    tracks: list[Track] = field(default_factory=list)
    program: str = ""
    host: str = ""
    loaded: bool = False

    @property
    def count(self) -> int:
        return len(self.tracks) if self.loaded else len(self.mp3_keys)


class Library:
    def __init__(self, bucket: str = BUCKET, prefix: str = PREFIX, region: str = REGION):
        self.bucket = bucket
        self.prefix = prefix.rstrip("/")
        self.client = boto3.client("s3", region_name=region)
        self._signed: dict[str, tuple[str, float]] = {}

    def list_days(self) -> list[Day]:
        """One paginated listing of the whole prefix, grouped by date folder.

        Cheaper than a delimiter listing plus a per-day listing, and it gives
        us the track counts for the date picker in the same pass.
        """
        days: dict[str, Day] = {}
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=f"{self.prefix}/"):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                rest = key[len(self.prefix) + 1 :]
                if "/" not in rest:
                    continue
                date, _, name = rest.partition("/")
                day = days.setdefault(date, Day(date=date, prefix=f"{self.prefix}/{date}"))
                if name.lower().endswith(".mp3"):
                    day.mp3_keys.append(key)
        return sorted(days.values(), key=lambda d: d.date, reverse=True)

    def load_day(self, day: Day) -> Day:
        """Populate a Day's tracks from playlist.json, falling back to the mp3 listing."""
        if day.loaded:
            return day

        present = {k.rsplit("/", 1)[-1]: k for k in day.mp3_keys}
        playlist = self._fetch_playlist(day)

        if playlist is None:
            # No playlist.json — fall back to filenames, which follow
            # "Artist - Song.mp3" throughout this bucket.
            for key in sorted(day.mp3_keys):
                name = key.rsplit("/", 1)[-1]
                stem = name[:-4]
                artist, sep, song = stem.partition(" - ")
                day.tracks.append(
                    Track(
                        artist=artist if sep else "",
                        song=song if sep else stem,
                        album="",
                        airdate="",
                        filename=name,
                        key=key,
                    )
                )
        else:
            day.program = playlist.get("program", "")
            day.host = playlist.get("host", "")
            for entry in playlist.get("tracks", []):
                name = entry.get("file", "")
                key = present.get(name, f"{day.prefix}/{name}")
                day.tracks.append(
                    Track(
                        artist=entry.get("artist", ""),
                        song=entry.get("song", ""),
                        album=entry.get("album", ""),
                        airdate=entry.get("airdate", ""),
                        filename=name,
                        key=key,
                        # A playlist entry with no matching object would 404 at
                        # play time; flag it so the UI can grey it out instead.
                        available=name in present,
                    )
                )

        day.loaded = True
        return day

    def _fetch_playlist(self, day: Day) -> dict | None:
        try:
            resp = self.client.get_object(Bucket=self.bucket, Key=f"{day.prefix}/playlist.json")
        except ClientError:
            return None
        try:
            return json.loads(resp["Body"].read())
        except (ValueError, KeyError):
            return None

    def presign(self, key: str) -> str:
        """Signed URL for a key, stable until it nears expiry.

        Each call to generate_presigned_url produces a fresh signature, so
        signing the same track twice yields two different URLs. The backend
        matches its preloaded stream by URL, so re-signing at play time would
        never match what was warmed and every switch would take the slow path.
        """
        now = time.monotonic()
        cached = self._signed.get(key)
        if cached is not None and cached[1] - now > URL_REFRESH_MARGIN:
            return cached[0]
        url = self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=URL_TTL_SECONDS,
        )
        self._signed[key] = (url, now + URL_TTL_SECONDS)
        return url
