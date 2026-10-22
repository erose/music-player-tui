"""Bucket access over plain HTTPS — no credentials, no SDK.

The bucket is public: its policy allows s3:GetObject to everyone, and its ACL
grants AllUsers READ, which is what permits anonymous listing. So both the
listing and the audio are ordinary unauthenticated HTTPS requests, and the
player works whether or not you are logged into AWS.

Nothing here downloads audio. The only objects fetched are playlist.json files
(a few KB); mp3s are only ever referenced by URL.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field

BUCKET = "elis-music"
PREFIX = "kexp-morning-show-rolling-downloads"
REGION = "us-east-2"

S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
TIMEOUT = 20


class PlaylistMissing(RuntimeError):
    """A day's playlist.json is absent or unreadable, so its tracks are
    unknown. Distinct from a transient fetch failure, which propagates as
    whatever urllib raised."""


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
        self.prefix = prefix.rstrip("/")
        self.endpoint = f"https://{bucket}.s3.{region}.amazonaws.com"

    def url(self, key: str) -> str:
        """Public URL for a key.

        Percent-encoded, because these filenames contain spaces, apostrophes
        and curly quotes — and because mpg123's LOAD command reads to the end
        of the line, so a raw space would truncate the URL.
        """
        return f"{self.endpoint}/{urllib.parse.quote(key)}"

    def _get(self, url: str) -> bytes:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as response:
            return response.read()

    def list_days(self) -> list[Day]:
        """One paginated listing of the whole prefix, grouped by date folder.

        Cheaper than a delimiter listing plus a per-day listing, and it gives
        us the track counts for the date picker in the same pass.
        """
        days: dict[str, Day] = {}
        token: str | None = None
        while True:
            params = {"list-type": "2", "prefix": f"{self.prefix}/"}
            if token:
                params["continuation-token"] = token
            root = ElementTree.fromstring(
                self._get(f"{self.endpoint}/?{urllib.parse.urlencode(params)}")
            )
            for contents in root.findall(f"{S3_NS}Contents"):
                key = contents.findtext(f"{S3_NS}Key", "")
                rest = key[len(self.prefix) + 1 :]
                if "/" not in rest:
                    continue
                date, _, name = rest.partition("/")
                day = days.setdefault(date, Day(date=date, prefix=f"{self.prefix}/{date}"))
                if name.lower().endswith(".mp3"):
                    day.mp3_keys.append(key)

            if root.findtext(f"{S3_NS}IsTruncated") != "true":
                break
            token = root.findtext(f"{S3_NS}NextContinuationToken")
            if not token:
                break

        return sorted(days.values(), key=lambda d: d.date, reverse=True)

    def load_day(self, day: Day) -> Day:
        """Populate a Day's tracks from its playlist.json.

        The playlist is required, not best-effort. Every day is uploaded with
        one, so its absence means the upload is still in flight or something
        upstream is broken — both worth surfacing rather than papering over
        with metadata guessed from filenames.

        Raises rather than half-loading, and leaves `loaded` false, so
        re-selecting the day tries again.
        """
        if day.loaded:
            return day

        playlist = self._fetch_playlist(day)
        present = {k.rsplit("/", 1)[-1]: k for k in day.mp3_keys}

        day.program = playlist.get("program", "")
        day.host = playlist.get("host", "")
        for entry in playlist.get("tracks", []):
            name = entry.get("file", "")
            key = present.get(name, f"{day.prefix}/{name}")
            day.tracks.append(
                Track(
                    # The ripper writes null, not a missing key, when KEXP
                    # has no album or airdate for a play, so `or ""` rather
                    # than a .get() default.
                    artist=entry.get("artist") or "",
                    song=entry.get("song") or "",
                    album=entry.get("album") or "",
                    airdate=entry.get("airdate") or "",
                    filename=name,
                    key=key,
                    # A playlist entry with no matching object would 404 at
                    # play time; flag it so the UI can grey it out instead.
                    # The mp3s and the playlist go up in one batch but not
                    # atomically, so a day caught mid-upload can genuinely
                    # list a track whose audio has not landed yet.
                    available=name in present,
                )
            )

        day.loaded = True
        return day

    def _fetch_playlist(self, day: Day) -> dict:
        url = self.url(f"{day.prefix}/playlist.json")
        try:
            body = self._get(url)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                # Distinguishable because the bucket allows anonymous listing;
                # without that S3 would answer 403 for a missing key too.
                raise PlaylistMissing(
                    f"{day.date} has no playlist.json yet — if the upload is "
                    "still running, try again in a moment."
                ) from exc
            raise
        try:
            return json.loads(body)
        except ValueError as exc:
            raise PlaylistMissing(f"{day.date} has an unreadable playlist.json") from exc
