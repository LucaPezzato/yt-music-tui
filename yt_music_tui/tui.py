"""Modern Truecolor Textual TUI for YouTube Music with Artwork, Playlists, Artist/Album Browser & Dynamic Themes."""

from __future__ import annotations

import subprocess
import threading
import time
from typing import Any

from PIL import Image
from rich.console import Group
from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import Input, Static

from .artwork import (
    DRACULA_PALETTE,
    ThemePalette,
    extract_dynamic_palette,
    fetch_image,
    render_image_halfblocks,
    render_placeholder_vinyl,
)
from .client import (
    PlaylistEntry,
    QueueItem,
    SearchResult,
    SongInfo,
    YTMusicClient,
    ensure_background_daemon,
)


def _fmt_time(seconds: int) -> str:
    seconds = max(0, int(seconds))
    mins, secs = divmod(seconds, 60)
    hrs, mins = divmod(mins, 60)
    if hrs > 0:
        return f"{hrs:d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def _truncate(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width <= 2:
        return text[:width]
    return text[: width - 1] + "…"


class YTMusicApp(App[None]):
    """Dracula & Dynamic Artwork TUI for YouTube Music."""

    CSS = """
    Screen {
        background: #1e1f29;
        color: #f8f8f2;
        padding: 0 1;
    }

    #top-bar {
        height: 1;
        width: 100%;
        margin-top: 0;
        content-align: left middle;
    }

    #player-card {
        height: 13;
        width: 100%;
        background: #282a36;
        border: round #bd93f9;
        padding: 0 1;
    }

    #artwork-box {
        width: 24;
        height: 11;
        margin-right: 2;
        content-align: center middle;
    }

    #track-info-box {
        width: 1fr;
        height: 11;
        padding-top: 0;
    }

    #search-input {
        dock: top;
        height: 3;
        margin: 0 0 1 0;
        background: #282a36;
        color: #f8f8f2;
        border: round #ff79c6;
        display: none;
    }

    #search-input.visible {
        display: block;
    }

    #list-card {
        width: 100%;
        height: 1fr;
        background: #282a36;
        border: round #6272a4;
        padding: 0 1;
    }

    #list-view {
        width: 100%;
        height: 1fr;
    }

    #footer-bar {
        height: 1;
        width: 100%;
        content-align: center middle;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit", show=False),
        Binding("escape", "close_or_quit", "Back/Quit", show=False),
        Binding("backspace", "navigate_back", "Back", show=False),
        Binding("space", "toggle_play", "Play/Pause", show=False),
        Binding("n", "next_track", "Next", show=False),
        Binding("p", "prev_track", "Prev", show=False),
        Binding("right", "seek_forward", "Seek +5s", show=False),
        Binding("left", "seek_backward", "Seek -5s", show=False),
        Binding("l", "seek_forward", "Seek +5s", show=False),
        Binding("h", "seek_backward", "Seek -5s", show=False),
        Binding("plus,equals,right_square_bracket", "vol_up", "Vol +5%", show=False),
        Binding("minus,underscore,left_square_bracket", "vol_down", "Vol -5%", show=False),
        Binding("m", "toggle_mute", "Mute", show=False),
        Binding("r", "cycle_repeat", "Repeat", show=False),
        Binding("S", "shuffle_queue", "Shuffle", show=False),
        Binding("L", "like_track", "Like", show=False),
        Binding("D", "dislike_track", "Dislike", show=False),
        Binding("1", "tab_queue", "Queue", show=False),
        Binding("2", "tab_search", "Search", show=False),
        Binding("3", "tab_playlists", "Playlists", show=False),
        Binding("4", "tab_browse", "Artist/Album", show=False),
        Binding("tab", "cycle_tab", "Switch Tab", show=False),
        Binding("slash,s", "open_search", "Search", show=False),
        Binding("g,i", "go_to_artist", "Go to Artist", show=False),
        Binding("b,A", "go_to_album", "Go to Album", show=False),
        Binding("t", "toggle_theme_mode", "Theme", show=False),
        Binding("o", "launch_desktop", "Open App", show=False),
        Binding("j,down", "cursor_down", "Down", show=False),
        Binding("k,up", "cursor_up", "Up", show=False),
        Binding("enter", "activate_item", "Play", show=False),
        Binding("a", "append_item", "Add to Queue", show=False),
        Binding("e", "play_next_item", "Play Next", show=False),
        Binding("d,x", "delete_item", "Remove", show=False),
        Binding("c", "clear_queue", "Clear Queue", show=False),
    ]

    theme_mode: reactive[str] = reactive("dynamic")

    def __init__(self, client: YTMusicClient) -> None:
        super().__init__()
        self.client = client
        self.song: SongInfo | None = None
        self.volume: int = 100
        self.muted: bool = False
        self.repeat_mode: str = "NONE"
        self.like_state: str = "INDIFFERENT"
        self.connected: bool = False

        # Tabs: 0 = Queue, 1 = Search, 2 = Playlists, 3 = Artist/Album
        self.active_tab: int = 0

        self.queue: list[QueueItem] = []
        self.queue_cursor: int = 0
        self.queue_scroll: int = 0

        self.search_query: str = ""
        self.search_results: list[SearchResult] = []
        self.search_cursor: int = 0
        self.search_scroll: int = 0

        self.playlists: list[PlaylistEntry] = []
        self.playlist_cursor: int = 0
        self.playlist_scroll: int = 0
        self.opened_playlist: PlaylistEntry | None = None
        self.playlist_tracks: list[SearchResult] = []
        self.playlist_track_cursor: int = 0
        self.playlist_track_scroll: int = 0

        self.browse_title: str = ""
        self.browse_items: list[SearchResult] = []
        self.browse_cursor: int = 0
        self.browse_scroll: int = 0

        self.status_msg: str = ""
        self._status_timer: Any = None

        self._current_art_key: str = ""
        self._current_art_image: Image.Image | None = None
        self._dynamic_palette: ThemePalette = DRACULA_PALETTE
        self._art_lock = threading.Lock()
        self._poll_tick: int = 0
        self._last_vol_change: float = 0.0

    @property
    def palette(self) -> ThemePalette:
        if self.theme_mode == "dynamic" and self._current_art_image is not None:
            return self._dynamic_palette
        return DRACULA_PALETTE

    def compose(self) -> ComposeResult:
        yield Static(id="top-bar")
        with Horizontal(id="player-card"):
            yield Static(id="artwork-box")
            yield Static(id="track-info-box")
        yield Input(
            placeholder="🔎 Search songs, artists, albums on YouTube Music...",
            id="search-input",
            disabled=True,
        )
        with Vertical(id="list-card"):
            yield Static(id="list-view")
        yield Static(id="footer-bar")

    def on_mount(self) -> None:
        started_bg = ensure_background_daemon()
        self._apply_palette()
        self._render_all()
        if started_bg:
            self.flash_status("Starting YouTube Music in background...")
        self.poll_player(force_queue=True)
        self.set_interval(1.0, self._tick_poll)

    def _tick_poll(self) -> None:
        self._poll_tick += 1
        self.poll_player(force_queue=(self._poll_tick % 3 == 0))

    def flash_status(self, msg: str) -> None:
        self.status_msg = msg
        self._render_footer()
        if self._status_timer is not None:
            self._status_timer.stop()
        self._status_timer = self.set_timer(2.8, self._clear_status)

    def _clear_status(self) -> None:
        self.status_msg = ""
        self._render_footer()

    @work(thread=True, exclusive=True, group="poller")
    def poll_player(self, force_queue: bool = False) -> None:
        connected = self.client.is_reachable()
        song = self.client.get_song()
        vol, muted = (self.volume, self.muted)
        repeat_mode = self.repeat_mode
        like_state = self.like_state
        new_queue: list[QueueItem] | None = None

        if connected:
            vol, muted = self.client.get_volume()
            repeat_mode = self.client.get_repeat_mode()
            like_state = self.client.get_like_state()
            if force_queue or not self.queue:
                q = self.client.get_queue()
                if q:
                    new_queue = q

        art_key = ""
        art_url = ""
        if song:
            art_key = song.image_src or song.video_id or f"{song.artist}:{song.title}"
            art_url = song.image_src

        if art_key and art_key != self._current_art_key and art_url:
            img = fetch_image(art_url)
            if img is not None:
                dyn = extract_dynamic_palette(img)
                with self._art_lock:
                    self._current_art_key = art_key
                    self._current_art_image = img
                    self._dynamic_palette = dyn
        elif not song and self._current_art_image is not None:
            with self._art_lock:
                self._current_art_key = ""
                self._current_art_image = None
                self._dynamic_palette = DRACULA_PALETTE

        self.call_from_thread(
            self._update_from_poll,
            connected,
            song,
            vol,
            muted,
            repeat_mode,
            like_state,
            new_queue,
        )

    def _update_from_poll(
        self,
        connected: bool,
        song: SongInfo | None,
        vol: int,
        muted: bool,
        repeat_mode: str,
        like_state: str,
        new_queue: list[QueueItem] | None,
    ) -> None:
        self.connected = connected
        self.song = song
        if time.monotonic() - self._last_vol_change >= 2.0:
            self.volume = vol
            self.muted = muted
        self.repeat_mode = repeat_mode
        self.like_state = like_state

        if new_queue is not None:
            was_empty = len(self.queue) == 0
            self.queue = new_queue
            self.queue_cursor = min(self.queue_cursor, max(0, len(self.queue) - 1))
            if was_empty:
                for idx, item in enumerate(self.queue):
                    if item.selected:
                        self.queue_cursor = idx
                        break

        # Enrich current song with artist_id / album_id from selected queue item
        if self.song and self.queue:
            for q_item in self.queue:
                if q_item.selected or (self.song.video_id and q_item.video_id == self.song.video_id):
                    self.song.artist_id = q_item.artist_id
                    self.song.album_id = q_item.album_id
                    if not self.song.album and q_item.album:
                        self.song.album = q_item.album
                    break

        self._apply_palette()
        self._render_all()

    def _apply_palette(self) -> None:
        pal = self.palette
        self.screen.styles.background = pal.bg_main
        player_card = self.query_one("#player-card")
        player_card.styles.background = pal.bg_panel
        player_card.styles.border = ("round", pal.primary)

        list_card = self.query_one("#list-card")
        list_card.styles.background = pal.bg_panel
        list_card.styles.border = ("round", pal.secondary if self.active_tab > 0 else pal.fg_muted)

        search_input = self.query_one("#search-input", Input)
        search_input.styles.background = pal.bg_panel
        search_input.styles.border = ("round", pal.secondary)

    def _render_all(self) -> None:
        self._render_top_bar()
        self._render_artwork()
        self._render_track_info()
        self._render_list()
        self._render_footer()

    def _render_top_bar(self) -> None:
        pal = self.palette
        w = max(40, self.size.width - 2)
        left = Text()
        left.append(" 󰎆 YouTube Music ", style=f"bold {pal.bg_main} on {pal.primary}")

        theme_badge = (
            f"  🎨 Dynamic ({pal.primary}) "
            if self.theme_mode == "dynamic"
            else "  🧛 Dracula "
        )
        left.append(theme_badge, style=f"bold {pal.secondary}")

        conn_str = "● API Ready " if self.connected else "○ Starting Background Daemon... "
        conn_col = pal.playing if self.connected else pal.paused
        pad = max(1, w - len(left.plain) - len(conn_str))
        left.append(" " * pad)
        left.append(conn_str, style=f"bold {conn_col}")

        self.query_one("#top-bar", Static).update(left)

    def _render_artwork(self) -> None:
        pal = self.palette
        with self._art_lock:
            img = self._current_art_image
        if img is not None:
            art_text = render_image_halfblocks(img, width_cells=22, height_cells=11)
        else:
            art_text = render_placeholder_vinyl(pal, width_cells=22, height_cells=11)
        self.query_one("#artwork-box", Static).update(art_text)

    def _render_track_info(self) -> None:
        pal = self.palette
        box = self.query_one("#track-info-box", Static)
        avail_w = max(24, self.size.width - 32)

        if not self.song:
            lines = Text()
            lines.append("\n")
            lines.append("⏹  No track playing\n\n", style=f"bold {pal.paused}")
            lines.append(
                "Press '/' to search songs or '3' to browse your Playlists.\n\n",
                style=f"{pal.fg}",
            )
            lines.append(
                "Press 't' to switch between Dracula & Dynamic Artwork themes.",
                style=f"italic {pal.secondary}",
            )
            box.update(lines)
            return

        s = self.song
        lines = Text()

        if s.is_paused:
            lines.append(" ⏸ PAUSED ", style=f"bold {pal.bg_main} on {pal.paused}")
        else:
            lines.append(" ▶ PLAYING ", style=f"bold {pal.bg_main} on {pal.playing}")

        if self.like_state == "LIKE":
            lines.append("  ♥ LIKED", style=f"bold {pal.secondary}")
        elif self.like_state == "DISLIKE":
            lines.append("  ✖ DISLIKED", style="bold #ff5555")
        lines.append("\n\n")

        lines.append(_truncate(s.title, avail_w) + "\n", style=f"bold {pal.fg}")
        lines.append(_truncate(f"󰠃 {s.artist}", avail_w) + "\n", style=f"bold {pal.secondary}")

        album_line = _truncate(f"󰀥 {s.album}", avail_w) if s.album else ""
        lines.append(album_line + "\n\n", style=f"{pal.fg_muted}")

        el_str = _fmt_time(s.elapsed)
        dur_str = _fmt_time(s.duration)
        bar_w = max(10, min(46, avail_w - len(el_str) - len(dur_str) - 6))
        ratio = min(1.0, max(0.0, s.elapsed / s.duration)) if s.duration > 0 else 0.0
        filled = int(bar_w * ratio)
        empty = max(0, bar_w - filled - 1)

        lines.append(f"{el_str} ", style=f"bold {pal.fg}")
        lines.append("━" * filled, style=f"bold {pal.primary}")
        lines.append("●", style=f"bold {pal.secondary}")
        lines.append("─" * empty, style=f"{pal.fg_muted}")
        lines.append(f" {dur_str}\n\n", style=f"{pal.fg_muted}")

        vol_icon = "🔇 Muted" if self.muted else f"🔊 {self.volume}%"
        rep_badge = f"🔁 {self.repeat_mode}"
        lines.append("⏯ [Space]  ", style=f"bold {pal.primary}")
        lines.append("⏮/⏭ [p/n]   ", style=f"{pal.fg_muted}")
        lines.append(f"{vol_icon} [-/+]   ", style=f"bold {pal.secondary}")
        lines.append(f"{rep_badge} [r]   ", style=f"{pal.fg_muted}")
        lines.append("󰠃 Artist [g]  󰀥 Album [b]", style=f"bold {pal.primary}")

        box.update(lines)

    def _render_list(self) -> None:
        pal = self.palette
        view = self.query_one("#list-view", Static)
        w = max(36, self.size.width - 6)
        h = max(4, self.size.height - 18)

        header = Text()
        tabs = [
            (0, f" [1] 󰲸 Queue ({len(self.queue)}) "),
            (
                1,
                f" [2] 🔎 Search ({len(self.search_results)}) "
                if not self.search_query
                else f" [2] 🔎 '{_truncate(self.search_query, 12)}' ({len(self.search_results)}) ",
            ),
            (
                2,
                f" [3] 󰲸 Playlists ({len(self.playlists)}) "
                if not self.opened_playlist
                else f" [3] 📂 {_truncate(self.opened_playlist.title, 14)} ({len(self.playlist_tracks)}) ",
            ),
            (
                3,
                " [4] 󰀥 Artist/Album "
                if not self.browse_title
                else f" [4] {_truncate(self.browse_title, 18)} ({len(self.browse_items)}) ",
            ),
        ]

        for idx, label in tabs:
            if idx == self.active_tab:
                col = pal.primary if idx == 0 else pal.secondary
                header.append(label, style=f"bold {pal.bg_main} on {col}")
            else:
                header.append(label, style=f"{pal.fg_muted}")
            header.append(" ")

        header.append("\n")
        header.append("─" * w + "\n", style=f"{pal.fg_muted}")

        body = Text()
        rows_avail = max(2, h - 2)

        if self.active_tab == 0:
            self._render_queue_rows(body, w, rows_avail)
        elif self.active_tab == 1:
            self._render_track_rows(
                body,
                self.search_results,
                self.search_cursor,
                "search_scroll",
                w,
                rows_avail,
                empty_msg="  Press '/' or 's' to search songs on YouTube Music.",
            )
        elif self.active_tab == 2:
            if self.opened_playlist is None:
                self._render_playlist_rows(body, w, rows_avail)
            else:
                self._render_track_rows(
                    body,
                    self.playlist_tracks,
                    self.playlist_track_cursor,
                    "playlist_track_scroll",
                    w,
                    rows_avail,
                    empty_msg="  Loading playlist tracks... (press Backspace to return to Playlists)",
                )
        elif self.active_tab == 3:
            self._render_track_rows(
                body,
                self.browse_items,
                self.browse_cursor,
                "browse_scroll",
                w,
                rows_avail,
                empty_msg="  Press 'g' to view Artist or 'b' to view Album of the selected/playing song.",
            )

        view.update(Group(header, body))

    def _render_queue_rows(self, body: Text, w: int, rows_avail: int) -> None:
        pal = self.palette
        if not self.queue:
            body.append(
                "\n  Queue is empty — press '/' to search or '3' to browse your Playlists.",
                style=f"italic {pal.fg_muted}",
            )
            return

        if self.queue_cursor < self.queue_scroll:
            self.queue_scroll = self.queue_cursor
        elif self.queue_cursor >= self.queue_scroll + rows_avail:
            self.queue_scroll = self.queue_cursor - rows_avail + 1

        visible = self.queue[self.queue_scroll : self.queue_scroll + rows_avail]
        for idx, item in enumerate(visible):
            abs_idx = self.queue_scroll + idx
            is_sel = abs_idx == self.queue_cursor
            prefix = " ▶ " if item.selected else f"{item.index + 1:2d}. "
            dur = f" {item.duration:>6s} "
            col_space = max(12, w - len(prefix) - len(dur) - 4)
            t_w = int(col_space * 0.56)
            a_w = max(6, col_space - t_w - 3)

            row_str = (
                f"{prefix}"
                f"{_truncate(item.title, t_w):<{t_w}}   "
                f"{_truncate(item.artist, a_w):<{a_w}}"
                f"{dur}"
            )
            if is_sel:
                style = f"bold {pal.fg} on {pal.bg_highlight}"
            elif item.selected:
                style = f"bold {pal.playing}"
            else:
                style = f"{pal.fg}"
            body.append(_truncate(row_str, w) + "\n", style=style)

    def _render_playlist_rows(self, body: Text, w: int, rows_avail: int) -> None:
        pal = self.palette
        if not self.playlists:
            body.append(
                "\n  Loading your YouTube Music playlists...",
                style=f"italic {pal.fg_muted}",
            )
            return

        if self.playlist_cursor < self.playlist_scroll:
            self.playlist_scroll = self.playlist_cursor
        elif self.playlist_cursor >= self.playlist_scroll + rows_avail:
            self.playlist_scroll = self.playlist_cursor - rows_avail + 1

        visible = self.playlists[self.playlist_scroll : self.playlist_scroll + rows_avail]
        for idx, pl in enumerate(visible):
            abs_idx = self.playlist_scroll + idx
            is_sel = abs_idx == self.playlist_cursor
            prefix = f"{abs_idx + 1:2d}. 📂 "
            col_space = max(12, w - len(prefix) - 4)
            t_w = int(col_space * 0.52)
            s_w = max(6, col_space - t_w - 3)

            row_str = (
                f"{prefix}"
                f"{_truncate(pl.title, t_w):<{t_w}}   "
                f"{_truncate(pl.subtitle, s_w):<{s_w}}"
            )
            style = f"bold {pal.fg} on {pal.bg_highlight}" if is_sel else f"{pal.fg}"
            body.append(_truncate(row_str, w) + "\n", style=style)

    def _render_track_rows(
        self,
        body: Text,
        items: list[SearchResult],
        cursor: int,
        scroll_attr: str,
        w: int,
        rows_avail: int,
        empty_msg: str,
    ) -> None:
        pal = self.palette
        if not items:
            body.append("\n" + empty_msg, style=f"italic {pal.fg_muted}")
            return

        scroll = getattr(self, scroll_attr, 0)
        if cursor < scroll:
            scroll = cursor
        elif cursor >= scroll + rows_avail:
            scroll = cursor - rows_avail + 1
        setattr(self, scroll_attr, scroll)

        visible = items[scroll : scroll + rows_avail]
        for idx, item in enumerate(visible):
            abs_idx = scroll + idx
            is_sel = abs_idx == cursor
            prefix = f"{abs_idx + 1:2d}. "
            dur = f" {item.duration:>6s} "
            col_space = max(12, w - len(prefix) - len(dur) - 4)
            t_w = int(col_space * 0.56)
            a_w = max(6, col_space - t_w - 3)

            row_str = (
                f"{prefix}"
                f"{_truncate(item.title, t_w):<{t_w}}   "
                f"{_truncate(item.artist, a_w):<{a_w}}"
                f"{dur}"
            )
            style = f"bold {pal.fg} on {pal.bg_highlight}" if is_sel else f"{pal.fg}"
            body.append(_truncate(row_str, w) + "\n", style=style)

    def _render_footer(self) -> None:
        pal = self.palette
        footer = self.query_one("#footer-bar", Static)
        if self.status_msg:
            t = Text(f" ✨ {self.status_msg} ", style=f"bold {pal.playing}")
            footer.update(t)
            return

        t = Text()
        hints = [
            ("Space", "Play"),
            ("Enter", "Play+Radio"),
            ("a", "Queue+"),
            ("/", "Search"),
            ("1-4", "Tabs"),
            ("g", "Artist"),
            ("b", "Album"),
            ("Bksp", "Back"),
            ("t", "Theme"),
            ("q", "Quit"),
        ]
        for key, label in hints:
            t.append(f" {key} ", style=f"bold {pal.bg_main} on {pal.primary}")
            t.append(f" {label} ", style=f"{pal.fg_muted}")
        footer.update(t)

    def on_resize(self, _: events.Resize) -> None:
        self._render_all()

    # --- Helpers to get highlighted track context ---

    def _get_highlighted_context(self) -> tuple[str, str, str, str]:
        """Returns (artist_id, artist_name, album_id, album_name) for the selected row or current song."""
        if self.active_tab == 0 and self.queue:
            q = self.queue[self.queue_cursor]
            if q.artist_id or q.album_id or q.artist:
                return q.artist_id, q.artist.split("•")[0].strip(), q.album_id, q.album
        elif self.active_tab == 1 and self.search_results:
            r = self.search_results[self.search_cursor]
            return r.artist_id, r.artist.split("•")[0].strip(), r.album_id, r.album
        elif self.active_tab == 2 and self.opened_playlist and self.playlist_tracks:
            r = self.playlist_tracks[self.playlist_track_cursor]
            return r.artist_id, r.artist.split("•")[0].strip(), r.album_id, r.album
        elif self.active_tab == 3 and self.browse_items:
            r = self.browse_items[self.browse_cursor]
            return r.artist_id, r.artist.split("•")[0].strip(), r.album_id, r.album

        if self.song:
            return self.song.artist_id, self.song.artist, self.song.album_id, self.song.album
        return "", "", "", ""

    # --- Actions ---

    def action_close_or_quit(self) -> None:
        search_input = self.query_one("#search-input", Input)
        if search_input.has_class("visible"):
            search_input.remove_class("visible")
            search_input.disabled = True
            self.set_focus(None)
            return
        self.exit()

    def action_navigate_back(self) -> None:
        if self.active_tab == 2 and self.opened_playlist is not None:
            self.opened_playlist = None
            self.playlist_tracks = []
            self._render_list()
            self.flash_status("Back to Playlists list")

    def action_toggle_theme_mode(self) -> None:
        self.theme_mode = "dracula" if self.theme_mode == "dynamic" else "dynamic"
        self._apply_palette()
        self._render_all()
        mode_name = "Dynamic Artwork" if self.theme_mode == "dynamic" else "Dracula"
        self.flash_status(f"Switched theme to {mode_name}")

    @work(thread=True)
    def action_toggle_play(self) -> None:
        self.client.toggle_play()
        self.call_from_thread(self.flash_status, "Play / Pause toggled")
        self.poll_player()

    @work(thread=True)
    def action_next_track(self) -> None:
        self.client.next_track()
        self.call_from_thread(self.flash_status, "Next track ⏭")
        self.poll_player(force_queue=True)

    @work(thread=True)
    def action_prev_track(self) -> None:
        self.client.previous_track()
        self.call_from_thread(self.flash_status, "Previous track ⏮")
        self.poll_player(force_queue=True)

    @work(thread=True)
    def action_seek_forward(self) -> None:
        self.client.seek_by(5)
        self.call_from_thread(self.flash_status, "Seek +5s")
        self.poll_player()

    @work(thread=True)
    def action_seek_backward(self) -> None:
        self.client.seek_by(-5)
        self.call_from_thread(self.flash_status, "Seek -5s")
        self.poll_player()

    def action_vol_up(self) -> None:
        self.volume = min(100, ((self.volume + 2) // 5) * 5 + 5)
        self._last_vol_change = time.monotonic()
        self._render_track_info()
        self.flash_status(f"🔊 Volume: {self.volume}%")
        self._push_volume(self.volume)

    def action_vol_down(self) -> None:
        self.volume = max(0, ((self.volume + 2) // 5) * 5 - 5)
        self._last_vol_change = time.monotonic()
        self._render_track_info()
        self.flash_status(f"🔉 Volume: {self.volume}%")
        self._push_volume(self.volume)

    @work(thread=True, exclusive=True, group="volume")
    def _push_volume(self, target_vol: int) -> None:
        self.client.set_volume(target_vol)

    @work(thread=True)
    def action_toggle_mute(self) -> None:
        self.client.toggle_mute()
        self.muted = not self.muted
        self.call_from_thread(self.flash_status, "Mute toggled")
        self.call_from_thread(self._render_track_info)

    @work(thread=True)
    def action_cycle_repeat(self) -> None:
        self.client.switch_repeat()
        self.poll_player()
        self.call_from_thread(self.flash_status, "Repeat mode cycled")

    @work(thread=True)
    def action_shuffle_queue(self) -> None:
        self.client.shuffle()
        self.call_from_thread(self.flash_status, "Queue shuffled 🔀")
        self.poll_player(force_queue=True)

    @work(thread=True)
    def action_like_track(self) -> None:
        self.client.like()
        self.like_state = "LIKE"
        self.call_from_thread(self.flash_status, "Liked track ♥")
        self.call_from_thread(self._render_track_info)

    @work(thread=True)
    def action_dislike_track(self) -> None:
        self.client.dislike()
        self.like_state = "DISLIKE"
        self.call_from_thread(self.flash_status, "Disliked track ✖")
        self.call_from_thread(self._render_track_info)

    def action_tab_queue(self) -> None:
        self.active_tab = 0
        self._apply_palette()
        self._render_list()
        self.poll_player(force_queue=True)

    def action_tab_search(self) -> None:
        self.active_tab = 1
        self._apply_palette()
        self._render_list()

    def action_tab_playlists(self) -> None:
        self.active_tab = 2
        self._apply_palette()
        self._render_list()
        if not self.playlists:
            self._load_playlists()

    def action_tab_browse(self) -> None:
        self.active_tab = 3
        self._apply_palette()
        self._render_list()

    def action_cycle_tab(self) -> None:
        self.active_tab = (self.active_tab + 1) % 4
        if self.active_tab == 2 and not self.playlists:
            self._load_playlists()
        self._apply_palette()
        self._render_list()

    @work(thread=True, exclusive=True, group="playlists")
    def _load_playlists(self) -> None:
        self.call_from_thread(self.flash_status, "Loading your playlists...")
        pls = self.client.get_library_playlists()
        self.playlists = pls
        self.playlist_cursor = 0
        self.playlist_scroll = 0
        self.call_from_thread(self._render_list)
        self.call_from_thread(self.flash_status, f"Loaded {len(pls)} playlists")

    @work(thread=True, exclusive=True, group="playlists")
    def _open_playlist(self, pl: PlaylistEntry) -> None:
        self.opened_playlist = pl
        self.playlist_tracks = []
        self.playlist_track_cursor = 0
        self.playlist_track_scroll = 0
        self.call_from_thread(self._render_list)
        self.call_from_thread(self.flash_status, f"Loading '{pl.title}'...")
        tracks = self.client.get_browse_items(pl.browse_id)
        for t in tracks:
            if not t.playlist_id:
                t.playlist_id = pl.playlist_id
        self.playlist_tracks = tracks
        self.call_from_thread(self._render_list)
        self.call_from_thread(self.flash_status, f"Opened '{pl.title}' ({len(tracks)} tracks)")

    def action_go_to_artist(self) -> None:
        artist_id, artist_name, _, _ = self._get_highlighted_context()
        if not artist_id and not artist_name:
            self.flash_status("No artist information available for this track")
            return
        self.active_tab = 3
        self.browse_title = f"󰠃 {artist_name or 'Artist'}"
        self.browse_items = []
        self.browse_cursor = 0
        self.browse_scroll = 0
        self._apply_palette()
        self._render_list()
        self._load_artist_or_album(artist_id, artist_name, is_album=False)

    def action_go_to_album(self) -> None:
        _, artist_name, album_id, album_name = self._get_highlighted_context()
        if not album_id and not album_name:
            self.flash_status("No album information available for this track")
            return
        self.active_tab = 3
        self.browse_title = f"󰀥 {album_name or 'Album'}"
        self.browse_items = []
        self.browse_cursor = 0
        self.browse_scroll = 0
        self._apply_palette()
        self._render_list()
        query_fallback = f"{artist_name} {album_name}".strip()
        self._load_artist_or_album(album_id, query_fallback, is_album=True)

    @work(thread=True, exclusive=True, group="browse")
    def _load_artist_or_album(self, browse_id: str, fallback_query: str, is_album: bool) -> None:
        label = "album" if is_album else "artist"
        self.call_from_thread(self.flash_status, f"Loading {label} page...")
        items: list[SearchResult] = []
        if browse_id:
            items = self.client.get_browse_items(browse_id)
        if not items and fallback_query:
            items = self.client.search(fallback_query)
        self.browse_items = items
        self.browse_cursor = 0
        self.browse_scroll = 0
        self.call_from_thread(self._render_list)
        self.call_from_thread(self.flash_status, f"Loaded {len(items)} items")

    def action_open_search(self) -> None:
        search_input = self.query_one("#search-input", Input)
        search_input.disabled = False
        search_input.add_class("visible")
        search_input.value = ""
        search_input.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        query = event.value.strip()
        event.input.remove_class("visible")
        event.input.disabled = True
        self.set_focus(None)
        if query:
            self.search_query = query
            self.active_tab = 1
            self._apply_palette()
            self.flash_status(f"Searching YouTube Music for '{query}'...")
            self._run_search(query)

    @work(thread=True, exclusive=True, group="search")
    def _run_search(self, query: str) -> None:
        results = self.client.search(query)
        self.search_results = results
        self.search_cursor = 0
        self.search_scroll = 0
        self.call_from_thread(self._render_list)
        self.call_from_thread(self.flash_status, f"Found {len(results)} tracks for '{query}'")

    def action_launch_desktop(self) -> None:
        subprocess.Popen(
            ["youtube-music"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.flash_status("Launching YouTube Music Desktop...")

    def action_cursor_down(self) -> None:
        if self.active_tab == 0 and self.queue:
            self.queue_cursor = min(len(self.queue) - 1, self.queue_cursor + 1)
        elif self.active_tab == 1 and self.search_results:
            self.search_cursor = min(len(self.search_results) - 1, self.search_cursor + 1)
        elif self.active_tab == 2:
            if self.opened_playlist is None and self.playlists:
                self.playlist_cursor = min(len(self.playlists) - 1, self.playlist_cursor + 1)
            elif self.opened_playlist is not None and self.playlist_tracks:
                self.playlist_track_cursor = min(
                    len(self.playlist_tracks) - 1, self.playlist_track_cursor + 1
                )
        elif self.active_tab == 3 and self.browse_items:
            self.browse_cursor = min(len(self.browse_items) - 1, self.browse_cursor + 1)
        self._render_list()

    def action_cursor_up(self) -> None:
        if self.active_tab == 0 and self.queue:
            self.queue_cursor = max(0, self.queue_cursor - 1)
        elif self.active_tab == 1 and self.search_results:
            self.search_cursor = max(0, self.search_cursor - 1)
        elif self.active_tab == 2:
            if self.opened_playlist is None and self.playlists:
                self.playlist_cursor = max(0, self.playlist_cursor - 1)
            elif self.opened_playlist is not None and self.playlist_tracks:
                self.playlist_track_cursor = max(0, self.playlist_track_cursor - 1)
        elif self.active_tab == 3 and self.browse_items:
            self.browse_cursor = max(0, self.browse_cursor - 1)
        self._render_list()

    @work(thread=True, exclusive=True, group="playback")
    def action_activate_item(self) -> None:
        if self.active_tab == 0 and self.queue:
            item = self.queue[self.queue_cursor]
            self.call_from_thread(self.flash_status, f"▶ Playing: {item.title}")
            self.client.set_queue_index(item.index)
            self.poll_player(force_queue=True)
            return

        if self.active_tab == 2 and self.opened_playlist is None:
            if self.playlists:
                pl = self.playlists[self.playlist_cursor]
                self.call_from_thread(self._open_playlist, pl)
            return

        target: SearchResult | None = None
        if self.active_tab == 1 and self.search_results:
            target = self.search_results[self.search_cursor]
        elif self.active_tab == 2 and self.opened_playlist and self.playlist_tracks:
            target = self.playlist_tracks[self.playlist_track_cursor]
        elif self.active_tab == 3 and self.browse_items:
            target = self.browse_items[self.browse_cursor]

        if not target:
            return

        # If the user clicked on an Album entry inside Artist view, open that Album's tracks
        if target.browse_id and not target.video_id:
            self.browse_title = f"󰀥 {target.album or target.title}"
            self.call_from_thread(
                self._load_artist_or_album, target.browse_id, target.title, True
            )
            return

        self.call_from_thread(
            self.flash_status, f"▶ Starting radio/queue for: {target.title}..."
        )
        if self.client.play_video(
            target.video_id, playlist_id=target.playlist_id, regenerate_queue=True
        ):
            time.sleep(1.4)
            self.poll_player(force_queue=True)
            self.call_from_thread(
                self.flash_status, f"▶ Playing {target.title} (queue regenerated)"
            )

    def _get_selected_track_from_active_tab(self) -> SearchResult | None:
        if self.active_tab == 1 and self.search_results:
            return self.search_results[self.search_cursor]
        if self.active_tab == 2 and self.opened_playlist and self.playlist_tracks:
            return self.playlist_tracks[self.playlist_track_cursor]
        if self.active_tab == 3 and self.browse_items:
            return self.browse_items[self.browse_cursor]
        return None

    @work(thread=True)
    def action_append_item(self) -> None:
        res = self._get_selected_track_from_active_tab()
        if res and res.video_id:
            if self.client.add_to_queue(res.video_id, "INSERT_AT_END"):
                self.call_from_thread(self.flash_status, f"➕ Added to end of queue: {res.title}")
                self.poll_player(force_queue=True)

    @work(thread=True)
    def action_play_next_item(self) -> None:
        res = self._get_selected_track_from_active_tab()
        if res and res.video_id:
            if self.client.add_to_queue(res.video_id, "INSERT_AFTER_CURRENT_VIDEO"):
                self.call_from_thread(self.flash_status, f"⏭ Playing next: {res.title}")
                self.poll_player(force_queue=True)

    @work(thread=True)
    def action_delete_item(self) -> None:
        if self.active_tab == 0 and self.queue:
            item = self.queue[self.queue_cursor]
            if self.client.remove_from_queue(item.index):
                self.call_from_thread(self.flash_status, f"Removed: {item.title}")
                self.poll_player(force_queue=True)

    @work(thread=True)
    def action_clear_queue(self) -> None:
        if self.active_tab == 0:
            self.client.clear_queue()
            self.queue = []
            self.call_from_thread(self._render_list)
            self.call_from_thread(self.flash_status, "Queue cleared")


def run_tui(client: YTMusicClient) -> None:
    app = YTMusicApp(client)
    app.run()
