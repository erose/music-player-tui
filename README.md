# player

KEXP's Morning Show, on a rolling two-week window, in the terminal. Two halves
that meet at one S3 prefix:

```
KEXP archive ──► morning_show_playlist.py ──► s3://elis-music/kexp-morning-show-rolling-downloads
   (rip)          nightly, GitHub Actions                    │
                                                             ▼
                                                    player/ (Textual TUI)
                                                          (listen)
```

- **The ripper** — `morning_show_playlist.py`, run nightly by
  `.github/workflows/daily-archive.yml`, slices each song out of KEXP's own
  broadcast archive and uploads a date-partitioned folder plus a
  `playlist.json` manifest.
- **The player** — `player/`, described below, reads that prefix and streams
  from it.

They were separate repos until the ripper moved here; nothing about the S3
layout changed in the move.

**The player needs no AWS credentials** — the bucket is public, so the listing
and the audio are ordinary unauthenticated HTTPS requests. **Nothing is
downloaded** either: `mpg123` decodes each track from its URL with range
requests, straight to the audio device. The only objects ever fetched are the
few-KB `playlist.json` files. (The *ripper* does need credentials, but only in
CI, and mints them per-run via OIDC — see below.)

## Run

```sh
./play
```

## Keys

| Key | |
|---|---|
| `↑` `↓` | move through the list |
| `←` `→` | switch between dates and songs |
| `Space` | play / pause the highlighted song |
| `q` | quit |

Left and right move between the two panes; up and down move within whichever
one has focus. Only the focused pane shows a solid cursor — the other dims to a
tint, so it's always clear which list the arrows are driving. Tab is
deliberately inert.

Space is the only control that starts or stops playback. The arrows only move
the cursor — you can browse the list freely while something plays, and nothing
begins until you press space. `▶` marks the song that's actually playing, which
may not be the row you're on. Volume is the system volume.

When a song ends the next one starts automatically. The cursor follows along if
it was sitting on the song that just finished, and stays where you put it if
you'd arrowed off to browse. If the stream drops part-way through a song the
player reconnects and picks up where it left off, rather than treating it as
the end of the track — see the note below.

## How it works

```
public bucket ──HTTPS──► mpg123 -R ──PCM──► CoreAudio
 ▲                           ▲ │
 │ urllib: list, playlist    │ │ commands / @F status
 └────── player/ (Textual) ──┘◄┘
```

- `player/library.py` — plain HTTPS against the public bucket using only the
  standard library: `urllib` plus `ElementTree` to parse the `ListObjectsV2`
  XML, including continuation-token pagination. One listing of the prefix gives
  every date folder and its track count in a single pass; `playlist.json`
  supplies artist, song, album and airdate, and is required rather than
  best-effort — see the note below. Entries listed in the playlist but missing
  from the bucket are shown struck through rather than failing at play time.
- `player/backend.py` — drives `mpg123 -R`, a line protocol on stdin/stdout.
  `@F` reports real elapsed and remaining seconds, so the progress bar and
  duration are exact rather than estimated from wall clock. Runs *two* mpg123
  processes, one playing and one warm — see below.
- `player/app.py` — Textual UI. Audio never crosses the UI thread; `@F` arrives
  ~40×/sec, so the app polls the backend's cached position six times a second
  instead of marshalling every frame.
- `player/theme.py` — the palette, in one place. Amber marks *where you are*
  (row cursor, focus, progress bar); rust marks *what is playing* (the `▶`), so
  the two never compete and the playing row stays findable when the cursor is
  elsewhere. Everything else is warm neutral. Change the four constants at the
  top to re-skin the whole app.

## Notes

- **`playlist.json` is required, and its absence is an error rather than a
  fallback.** The ripper writes it alongside the mp3s and the upload step reads
  it to derive the date *before* syncing, so a day folder can only exist in the
  bucket because its playlist existed first. `load_day` used to parse
  `Artist - Song.mp3` when the fetch failed, which guarded a state that cannot
  persist and produced album-less, airdate-less tracks indistinguishable from
  real ones. It also treated *every* failure as "no playlist" — `HTTPError`
  subclasses `URLError`, so a 404, a 500 and a connection reset were the same
  thing — and then set `loaded`, caching the degraded result for the session.
  Now a missing or unreadable playlist raises `PlaylistMissing` and anything
  else propagates, both leaving `loaded` false so re-selecting the day retries.
  The one moment a day can genuinely lack a playlist is the few seconds
  `aws s3 sync` takes: it uploads the directory in parallel, unordered, so on
  2026-07-31 the playlist landed a second *before* the final mp3. Hence the
  wording of the error, and hence keeping `available=False` for the mirror
  case of an entry whose audio has not landed yet.
- **A dropped connection is indistinguishable from a finished song**, which is
  what used to make the player skip to the next track mid-song. mpg123 reports
  both as `@P 3` followed by `@P 0`, with no `@E` in between — captured by
  serving a real mp3 and hanging up mid-body, which produced end-of-track
  25.51s into a 581s track. The only signal separating them is how far through
  the track the last `@F` got, so `Mpg123._died_early` compares position
  against duration and, when a stream died early, reconnects and seeks back to
  where it stopped instead of reporting the end. A track that keeps dying in
  the same spot is given up on after `MAX_RECONNECTS` attempts with an error,
  so recovery can't become an infinite reconnect loop. `backend.reconnects`
  counts reconnect attempts, including ones that went on to fail, so it is a
  fault gauge rather than a success tally. Note the check needs the duration
  estimate to be trustworthy: measured at the real end of a track, `@F`
  reported 0.03s remaining, against an `END_TOLERANCE` of 5s.
- **`PLAYER_LOG=/tmp/player.log` records every command and reply**, per
  process, with timestamps — `A >> LOAD ...`, `A << @P 3`, plus the decisions
  the backend makes. Add `PLAYER_LOG_FRAMES=1` to keep `@F` too, which arrives
  ~40x a second. Off by default. Playback faults happen minutes apart and
  depend on the network, so reconstructing one after the fact beats trying to
  catch it live.
- **`backend.seek()` exists but is not bound to a key.** Kept because it's
  working and tested, in case scrubbing is wanted later. Note that `mpg123`
  consumes the HTTP response sequentially and can only seek *forward* within a
  stream — any backward `JUMP` fails with "Error while seeking" and playback
  just runs on. `seek()` detects a backward target and re-issues `LOAD` (a fresh
  request from byte 0) followed by a forward `JUMP`, so seeking back costs a
  reconnect.
- **Changing song is gapless, via a warm standby process.** Pointing a single
  mpg123 at a new URL costs ~285ms of silence while it opens a fresh TLS
  connection to S3 and re-syncs the decoder — an audible hiccup on every song
  change, automatic ones included. `LOADPAUSED` opens a stream without emitting
  audio, so a second process is kept warmed with whatever you're most likely to
  play next; starting it takes 3-6ms. The two swap roles on each switch.
  What gets warmed: the row under the cursor, or — when the cursor is parked on
  the playing track, i.e. you're just listening — the *next* track, so
  auto-advance is gapless too. `backend.warm_starts` / `cold_starts` count which
  path each start took.
- **URLs are stable by construction**, which the warm standby depends on: it
  matches its preloaded stream by URL, so a URL that varied between preload and
  play would silently never match and every switch would take the slow path.
  (This was a real bug back when these were presigned — `generate_presigned_url`
  returns a fresh signature on every call.)
- **Pause latency is bounded by the audio device buffer.** mpg123's decode loop
  blocks writing to CoreAudio and only services stdin once per buffer, so with
  the default buffer `PAUSE` took ~390ms to take effect. `--devbuffer 0.05`
  brings that to ~110ms. Don't shrink it much further — the device buffer is
  also the slack absorbing network jitter. (`--buffer` makes it *worse*: that's
  an output cache pause has to drain, measured at ~520ms.) The UI flips the
  play/pause glyph optimistically on the keypress, so the display is instant
  regardless.
- **Column widths are computed from the pane, not fixed.** `DataTable` sizes
  columns to their widest cell, so long album names pushed the table past its
  pane, clipped the last column and raised a horizontal scrollbar. Widths are
  now split proportionally across the available width and cell text is clipped
  to match. Note each column costs 2 extra cells of cell padding, and the
  vertical scrollbar takes another — unaccounted for, it overflows by a cell.
  The recompute listens to `TrackTable.Resized` rather than the app's own
  `Resize`: the latter fires before the table is re-laid-out, which leaves the
  columns sized for the *previous* terminal width.
- **The bucket's public access is bucket-wide, not scoped to this prefix.** The
  policy allows `s3:GetObject` on `arn:aws:s3:::elis-music/*`, and the bucket
  ACL grants `AllUsers: READ`, which is what makes anonymous listing work.
  Anonymous writes are refused (403). If the bucket is ever made private this
  player stops working and would need signing put back.
- Duplicate playlist entries are preserved — on 2026-07-28 the Pixies genuinely
  aired twice, at 8:11 and 8:14.

## The ripper

`morning_show_playlist.py` finds a Morning Show airing via KEXP's public API
and pulls the tracks it logged. Stdlib only; `ffmpeg` must be on `PATH`.

```sh
python3 morning_show_playlist.py                     # today's playlist, printed
python3 morning_show_playlist.py 2026-06-22          # a specific date
python3 morning_show_playlist.py --csv               # CSV instead of a list
python3 morning_show_playlist.py --archive --latest  # rip the newest airing (what CI runs)
python3 morning_show_playlist.py --archive --recut   # re-cut a day already downloaded
python3 morning_show_playlist.py --youtube           # studio tracks off YouTube instead
```

Output lands in `downloads/` as `Artist - Song.mp3` alongside `playlist.json`.
Both are gitignored.

- **`--archive` slices the actual broadcast**, mapping each logged airdate to
  an offset in KEXP's ~2-week streaming archive and cutting with ffmpeg. Always
  the right recording, but it's live radio, so intros and outros carry DJ
  voiceover. Only works inside the archive window.

#### How the show is cut up

The day is cut into a **contiguous partition**: each boundary is a single instant
used as both the end of one track and the start of the next, so playing the
folder through in order reproduces the broadcast exactly — nothing repeats and
nothing is dropped. The first track absorbs the show's opening and the last runs
to its end, so the whole airing is covered.

This matters because the obvious approach doesn't work. Padding each song's
window by a lead and a tail *independently* duplicates `lead + tail` seconds at
every boundary — the two windows either side of a seam both claim that audio.
The fix isn't a smaller pad; it's computing one boundary list for the show and
deriving windows from it, so the duplication is unrepresentable.

Boundaries sit at `airdate - shift`, where the DJ logs each play ~42s after the
song actually starts. That lag varies by roughly ±14s, which this does not try to
correct, so a cut can land a few seconds off the true seam — a song's first
moments may sit at the tail of the previous track. Being *contiguous* is the
guarantee; being exactly on the beat is not.

One wrinkle the arithmetic can't ignore: the archive's mp3 chunks **overlap**. A
show's chunk keeps running past the point where the next show's chunk begins, so
the same minutes exist in both files. Boundaries therefore live on the broadcast
wall-clock timeline rather than in any one file's offsets, and each window is
mapped into whichever chunk actually contains it. Partitioning per file instead
would duplicate that overlapping stretch — the same bug, one level down.

`playlist.json` records each track's exact `start_offset`/`end_offset` plus the
chunk it came from, so a day's cuts are auditable after the fact. Note that a
re-run skips any mp3 already on disk, so **changing cut settings needs `--recut`**
or the old cuts survive. CI is unaffected — it always starts from an empty
`downloads/`.
- **`--youtube` gets clean studio tracks** from the top search hit, and is
  strict — any track that fails to download exits non-zero. It does *not* work
  on CI: YouTube bot-blocks GitHub's datacenter IPs. That's why the nightly job
  uses `--archive`.
- **`--latest` beats "today"** for unattended runs, which is why the cron uses
  it: it takes the newest airing already in the past, so weekends, holidays and
  preemptions don't produce an empty or failed rip.

### The nightly job

`.github/workflows/daily-archive.yml` runs at 20:00 UTC — noon PST / 1pm PDT,
comfortably after the show ends around 10:00 Pacific either way, since cron is
always UTC and drifts an hour with DST. `workflow_dispatch` is enabled, so you
can also trigger it by hand from the Actions tab.

There are **no long-lived AWS keys in the repo**. The job mints short-lived
credentials from IAM role `kexp-archive-uploader` via GitHub's OIDC provider.
What that setup depends on, all of it per-repo:

| | |
|---|---|
| secret `AWS_ROLE_ARN` | the role to assume |
| vars `AWS_REGION`, `S3_BUCKET`, `S3_PREFIX` | where the upload goes |
| the role's trust policy | must name *this* repo (see below) |

- **The trust policy pins numeric IDs, not the repo name.** This account's OIDC
  tokens carry a custom `sub` claim embedding immutable owner and repo IDs —
  `repo:erose@1514487/music-player-tui@1316492211:ref:refs/heads/main`, not the
  default `repo:erose/music-player-tui:...`. The policy has to `StringLike`
  against that form or assume-role fails with "Not authorized to perform
  sts:AssumeRoleWithWebIdentity". Confirmed via CloudTrail 2026-07-29. Because
  the ID is per-repo, moving the ripper here from `erose/kexp-downloader`
  (`@1315634626`) meant re-pointing the trust policy — a rename or a second
  move would too.
- **ffmpeg is not preinstalled on `ubuntu-latest`** and is installed by a step.
  Nothing else is: the ripper is stdlib-only, so the job never installs
  `requirements.txt` — `textual` is the player's dependency, not the ripper's.

## Tests

```sh
.venv/bin/python -m unittest discover -s tests -t tests
```

Stdlib `unittest`, no test dependency to install. `tests/fake_mpg123.py` stands
in for the decoder: it speaks the same line protocol, but on a compressed clock
(a 10-minute track finishes in seconds) and with failure injection, so dropped
connections are reproducible instead of waiting for the network to misbehave.
`tests/test_advance.py` drives the real UI through Textual's `run_test`, with
the bucket stubbed out, and asserts on which track the player thinks it's on.
`tests/test_archive.py` covers the ripper's archive path — offset arithmetic,
stream resolution, the ffmpeg argv — with the network and ffmpeg both mocked,
so it's fast and safe to run anywhere.

## Setup (already done)

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`textual` is the only dependency; everything else is standard library. `mpg123`
must be on `PATH` (`brew install mpg123`).
