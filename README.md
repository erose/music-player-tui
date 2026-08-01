# kexp-player

A terminal player for the KEXP Morning Show rolling downloads in
`s3://elis-music/kexp-morning-show-rolling-downloads`.

**No AWS credentials needed** — the bucket is public, so the listing and the
audio are ordinary unauthenticated HTTPS requests. **Nothing is downloaded**
either: `mpg123` decodes each track from its URL with range requests, straight
to the audio device. The only objects ever fetched are the few-KB
`playlist.json` files.

## Run

```sh
./kexp
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
  every date folder and its track count in a single pass; `playlist.json` supplies artist,
  song, album and airdate, with a filename-parsing fallback if it's absent.
  Entries listed in the playlist but missing from the bucket are shown struck
  through rather than failing at play time.
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
- **`KEXP_LOG=/tmp/kexp.log` records every command and reply**, per
  process, with timestamps — `A >> LOAD ...`, `A << @P 3`, plus the decisions
  the backend makes. Add `KEXP_LOG_FRAMES=1` to keep `@F` too, which arrives
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

## Setup (already done)

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`textual` is the only dependency; everything else is standard library. `mpg123`
must be on `PATH` (`brew install mpg123`).
