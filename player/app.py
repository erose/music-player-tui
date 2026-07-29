"""Terminal UI for the KEXP Morning Show rolling downloads."""

from __future__ import annotations

from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.coordinate import Coordinate
from textual.message import Message
from textual.widgets import DataTable, Footer, Header, Label, ListItem, ListView, Static

from .backend import FINISHED, PAUSED, PLAYING, STOPPED, BackendMissing, Mpg123
from .library import Day, Library, Track
from .theme import AMBER, KEXP_THEME, MUTED, RUST

def format_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def clip(text: str, width: int) -> str:
    """DataTable sizes columns to their widest cell, so long album and song
    names push the table past the pane and raise a scrollbar. Truncate instead."""
    return text if len(text) <= width else text[: width - 1] + "…"


def format_airdate(airdate: str) -> str:
    """'2026-07-28T07:05:28-07:00' -> '7:05'."""
    if "T" not in airdate:
        return ""
    clock = airdate.split("T", 1)[1][:5]
    hour, _, minute = clock.partition(":")
    try:
        return f"{int(hour)}:{minute}"
    except ValueError:
        return clock


class TrackTable(DataTable):
    """DataTable that reports its own new width when it is resized.

    The app-level Resize event fires before the table has been re-laid-out, so
    reading `table.size` from there leaves the column widths a resize behind.
    The widget's own Resize carries the correct new size.
    """

    class Resized(Message):
        def __init__(self, width: int) -> None:
            self.width = width
            super().__init__()

    def on_resize(self, event: events.Resize) -> None:
        self.post_message(self.Resized(event.size.width))


class NowPlaying(Static):
    """Bottom bar: transport state, title, scrub bar."""

    BAR_WIDTH = 28

    def show(self, track: Track | None, state: int, position: float, duration: float) -> None:
        if track is None:
            self.update(f"[{MUTED}]Press space to play — ↑↓ to move[/]")
            return

        glyph = {PLAYING: "▶", PAUSED: "⏸", FINISHED: "■", STOPPED: "■"}.get(state, "■")
        # Rust while it's actually making sound, muted once it isn't, so the
        # bar tells you the state at a glance without reading the glyph.
        glyph_colour = RUST if state == PLAYING else MUTED

        if duration > 0:
            filled = int(self.BAR_WIDTH * min(1.0, position / duration))
        else:
            filled = 0
        played = "━" * filled + "╸"
        rest = "─" * max(0, self.BAR_WIDTH - filled - 1)

        self.update(
            f"[{glyph_colour} bold]{glyph}[/] {track.title}"
            f"   [{MUTED}]{format_time(position)}[/]"
            f" [{AMBER}]{played}[/][{MUTED}]{rest}[/]"
            f" [{MUTED}]{format_time(duration)}[/]"
        )


class MusicPlayer(App):
    TITLE = "KEXP Morning Show"

    CSS = """
    Screen { background: $background; }
    Horizontal { height: 1fr; }

    Header { background: $panel; color: $foreground; }
    HeaderTitle { color: $foreground; text-style: bold; }
    HeaderIcon { display: none; }
    Footer { background: $panel; }

    #dates {
        width: 24;
        background: $surface;
        border-right: solid $panel;
    }
    #dates > ListItem { padding: 0 1; background: $surface; }
    /* Textual's highlight class is -highlight, one dash. Two dashes silently
       matches nothing and you get the default styling instead. */
    #dates > ListItem.-highlight { background: $primary 25%; color: $foreground; }
    #dates:focus > ListItem.-highlight { background: $primary; color: $background; }

    #tracks {
        width: 1fr;
        background: $background;
        scrollbar-size-vertical: 1;
        scrollbar-background: $surface;
        scrollbar-color: $primary 35%;
        scrollbar-color-hover: $primary 55%;
        scrollbar-color-active: $primary;
    }
    /* Only the focused pane gets a solid cursor. The other drops to a tint, so
       it's always obvious which one the arrow keys are driving. */
    #tracks > .datatable--cursor { background: $primary; color: $background; }
    #tracks:blur > .datatable--cursor { background: $primary 25%; color: $foreground; }
    #tracks > .datatable--header { background: $panel; color: $text-muted; }
    #tracks > .datatable--hover { background: $primary 15%; }

    NowPlaying {
        height: 3;
        /* border(1) + padding-top(1) + content(1) == height. Any padding-bottom
           here pushes the content line out of the box and it renders blank. */
        padding: 1 2 0 2;
        background: $panel;
        border-top: solid $primary 40%;
    }
    """

    BINDINGS = [
        Binding("space", "toggle_pause", "Play/Pause", priority=True),
        # priority: the focused DataTable would otherwise consume the arrows.
        Binding("left", "focus_dates", "Dates", priority=True),
        Binding("right", "focus_songs", "Songs", priority=True),
        # Left/right own pane switching now; keep tab from also cycling focus.
        Binding("tab", "noop", show=False, priority=True),
        Binding("shift+tab", "noop", show=False, priority=True),
        Binding("q", "quit", "Quit"),
    ]

    COLUMN_LABELS = [" ", "#", "Air", "Artist", "Song", "Album"]
    FIXED_WIDTHS = [1, 3, 5]  # marker, number, airtime
    FLEX_SHARE = [0.30, 0.42, 0.28]  # artist, song, album
    MIN_FLEX = [10, 12, 6]
    # Every column costs 2 extra cells of cell padding, and a 44-row list keeps
    # a vertical scrollbar on screen. Unaccounted for, the table overflows its
    # pane by a cell or two, which clips the last column and raises a
    # horizontal scrollbar across the bottom.
    SCROLLBAR_RESERVE = 2

    # Wait for the cursor to settle before opening a connection, so holding
    # an arrow key doesn't fire a preload per row.
    PRELOAD_DEBOUNCE = 0.25

    def __init__(self) -> None:
        super().__init__()
        self.library = Library()
        self.backend: Mpg123 | None = None
        self.days: list[Day] = []
        self.shown_day: Day | None = None
        # The playing track may belong to a day other than the one on screen,
        # so auto-advance tracks its own day rather than following the cursor.
        self.playing_day: Day | None = None
        self.playing_index: int | None = None
        self.marked_row: int | None = None
        self.column_keys: list = []
        self.widths: list[int] = []
        self.pane_width: int | None = None
        self._preload_timer = None

    # -- layout -----------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield ListView(id="dates")
            yield TrackTable(id="tracks")
        yield NowPlaying()
        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(KEXP_THEME)
        self.theme = KEXP_THEME.name

        table = self.query_one("#tracks", TrackTable)
        table.cursor_type = "row"
        table.zebra_stripes = True

        try:
            self.backend = Mpg123(
                on_progress=lambda position, duration: None,  # polled instead
                on_state=lambda state: self.call_from_thread(self._on_state, state),
                on_error=lambda message: self.call_from_thread(self._on_error, message),
            )
        except BackendMissing as exc:
            self.notify(str(exc), severity="error", timeout=30)

        self.query_one(NowPlaying).show(None, STOPPED, 0, 0)
        # @F arrives ~40x/sec; polling the backend's cached position is far
        # cheaper than marshalling every frame onto the UI thread.
        self.set_interval(1 / 6, self._refresh_now_playing)
        self.load_days()

    # -- loading ----------------------------------------------------------

    @work(thread=True, exclusive=True)
    def load_days(self) -> None:
        try:
            days = self.library.list_days()
        except Exception as exc:
            self.call_from_thread(self._on_error, f"Could not list bucket: {exc}")
            return
        self.call_from_thread(self._populate_days, days)

    def _populate_days(self, days: list[Day]) -> None:
        self.days = days
        listing = self.query_one("#dates", ListView)
        listing.clear()
        for day in days:
            listing.append(ListItem(Label(f"{day.date}  [{MUTED}]{day.count} tracks[/]")))
        if days:
            listing.index = 0
        else:
            self.notify("No date folders found under the prefix.", severity="warning")

    @work(thread=True, exclusive=True)
    def load_day(self, day: Day) -> None:
        try:
            self.library.load_day(day)
        except Exception as exc:
            self.call_from_thread(self._on_error, f"Could not load {day.date}: {exc}")
            return
        self.call_from_thread(self._populate_tracks, day)

    def _column_widths(self, pane_width: int | None = None) -> list[int]:
        """Split the pane between the three text columns, whatever its width."""
        if pane_width is None:
            pane_width = self.query_one("#tracks", DataTable).size.width
        overhead = 2 * len(self.COLUMN_LABELS) + sum(self.FIXED_WIDTHS) + self.SCROLLBAR_RESERVE
        flex = max(sum(self.MIN_FLEX), pane_width - overhead)
        artist = max(self.MIN_FLEX[0], int(flex * self.FLEX_SHARE[0]))
        song = max(self.MIN_FLEX[1], int(flex * self.FLEX_SHARE[1]))
        album = max(self.MIN_FLEX[2], flex - artist - song)
        return [*self.FIXED_WIDTHS, artist, song, album]

    def on_track_table_resized(self, message: TrackTable.Resized) -> None:
        self.pane_width = message.width
        if self.shown_day is not None and self._column_widths(message.width) != self.widths:
            self._populate_tracks(self.shown_day)

    def _populate_tracks(self, day: Day) -> None:
        self.shown_day = day
        table = self.query_one("#tracks", DataTable)
        cursor = table.cursor_row
        table.clear(columns=True)
        self.marked_row = None
        self.widths = self._column_widths(self.pane_width)
        self.column_keys = [
            table.add_column(label, width=w)
            for label, w in zip(self.COLUMN_LABELS, self.widths)
        ]
        artist_w, song_w, album_w = self.widths[3:]
        for number, track in enumerate(day.tracks, start=1):
            style = "" if track.available else f"[{MUTED} strike]"
            close = "" if track.available else "[/]"
            table.add_row(
                " ",
                f"[{MUTED}]{number}[/]",
                f"[{MUTED}]{format_airdate(track.airdate)}[/]",
                f"{style}{clip(track.artist, artist_w)}{close}",
                f"{style}{clip(track.song, song_w)}{close}",
                f"[{MUTED}]{clip(track.album, album_w)}[/]",
            )
        if day.host:
            self.sub_title = f"{day.date} · {day.host}"
        else:
            self.sub_title = day.date
        if 0 < cursor < table.row_count:
            table.move_cursor(row=cursor)
        # Start on the songs: up/down should work the moment the app opens,
        # and there is usually only one date to choose from anyway.
        if not table.has_focus and self.query_one("#dates", ListView).has_focus:
            table.focus()
        # Re-mark the playing row if we navigated back to its day.
        if self.playing_day is day and self.playing_index is not None:
            self._mark_row(self.playing_index)

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.list_view.id != "dates" or event.list_view.index is None:
            return
        if not self.days:
            return
        self.load_day(self.days[event.list_view.index])

    # -- playback ---------------------------------------------------------

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        # Moving the cursor never touches playback — it only changes which
        # track we keep warm, so pressing space is instant.
        self._schedule_preload()

    def _schedule_preload(self) -> None:
        if self._preload_timer is not None:
            self._preload_timer.stop()
        self._preload_timer = self.set_timer(self.PRELOAD_DEBOUNCE, self._preload)

    def _preload(self) -> None:
        """Warm the track most likely to be played next.

        Normally that's whatever the cursor is on. But if the cursor is parked
        on the playing track — the passive listening case — the next thing to
        happen is auto-advance, so prime the following track instead.
        """
        if self.backend is None or self.shown_day is None:
            return
        day = self.shown_day
        row = self.query_one("#tracks", TrackTable).cursor_row
        if day is self.playing_day and row == self.playing_index:
            row += 1
        if not 0 <= row < len(day.tracks):
            return
        track = day.tracks[row]
        if not track.available:
            return
        try:
            self.backend.preload(self.library.url(track.key))
        except Exception:
            pass  # preloading is an optimisation; failing it costs only latency

    def play(self, day: Day, index: int) -> None:
        if self.backend is None or not (0 <= index < len(day.tracks)):
            return
        track = day.tracks[index]
        if not track.available:
            self.notify(f"{track.filename} is listed but missing from the bucket.", severity="warning")
            return

        try:
            url = self.library.url(track.key)
        except Exception as exc:
            self._on_error(f"Could not build URL for {track.filename}: {exc}")
            return

        self.playing_day = day
        self.playing_index = index
        if day is self.shown_day:
            self._mark_row(index)
        self.backend.play(url)
        self._schedule_preload()

    def _mark_row(self, index: int) -> None:
        table = self.query_one("#tracks", DataTable)
        if self.marked_row is not None and self.marked_row < table.row_count:
            table.update_cell_at(Coordinate(self.marked_row, 0), " ")
        if index < table.row_count:
            table.update_cell_at(Coordinate(index, 0), f"[{RUST} bold]▶[/]")
            self.marked_row = index

    def _on_state(self, state: int) -> None:
        if state == FINISHED:
            self._advance()

    def _advance(self) -> None:
        """End of track: move to the next song and keep going."""
        if self.playing_day is None or self.playing_index is None:
            return
        nxt = self.playing_index + 1
        if nxt >= len(self.playing_day.tracks):
            return
        # Carry the cursor along only if it was still sitting on the track that
        # just finished. If you've arrowed off to browse, your place is yours.
        if self.playing_day is self.shown_day:
            table = self.query_one("#tracks", DataTable)
            if table.cursor_row == self.playing_index and nxt < table.row_count:
                table.move_cursor(row=nxt)
        self.play(self.playing_day, nxt)

    def _on_error(self, message: str) -> None:
        self.notify(message, severity="error", timeout=10)

    def _refresh_now_playing(self) -> None:
        if self.backend is None:
            return
        track = None
        if self.playing_day is not None and self.playing_index is not None:
            track = self.playing_day.tracks[self.playing_index]
        self.query_one(NowPlaying).show(
            track, self.backend.state, self.backend.position, self.backend.duration
        )

    # -- actions ----------------------------------------------------------

    def action_toggle_pause(self) -> None:
        """The only control that starts or stops playback.

        Acts on the highlighted row: pause it if it's playing, resume it if
        it's paused, otherwise start it.
        """
        if self.backend is None or self.shown_day is None:
            return

        table = self.query_one("#tracks", DataTable)
        row = table.cursor_row
        on_current = self.playing_day is self.shown_day and self.playing_index == row

        if on_current and self.backend.state in (PLAYING, PAUSED):
            self.backend.toggle_pause()
            # The backend flips its state optimistically, so redraw now rather
            # than waiting up to a poll interval for the glyph to catch up.
            self._refresh_now_playing()
            return

        self.play(self.shown_day, row)

    def action_focus_dates(self) -> None:
        self.query_one("#dates", ListView).focus()

    def action_focus_songs(self) -> None:
        self.query_one("#tracks", TrackTable).focus()

    def action_noop(self) -> None:
        """Swallow tab so it no longer cycles focus."""

    def on_unmount(self) -> None:
        if self.backend is not None:
            self.backend.quit()


def main() -> None:
    MusicPlayer().run()
