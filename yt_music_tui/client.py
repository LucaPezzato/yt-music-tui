"""Client for YouTube Music Desktop API Server, MPRIS OpenUri, and InnerTube Browse."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 26538
CLIENT_ID = "yt-music-tui"
YTM_CONFIG_PATH = Path.home() / ".config" / "YouTube Music" / "config.json"
YTM_COOKIES_PATH = Path.home() / ".config" / "YouTube Music" / "Cookies"

# InnerTube filter chip for "Songs / Brani"
SONGS_SEARCH_PARAMS = "EgWKAQIIAWoSEAMQBRAJEAQQChAQEBUQDhAR"

# Mapping between YouTube Music UI volume slider (0..100) and internal logarithmic player state
_SLIDER_TO_STATE: list[tuple[int, int]] = [
    (0, 0),
    (5, 1),
    (10, 2),
    (15, 3),
    (20, 5),
    (25, 6),
    (30, 8),
    (35, 11),
    (40, 13),
    (45, 16),
    (50, 20),
    (55, 24),
    (60, 29),
    (65, 34),
    (70, 40),
    (75, 47),
    (80, 55),
    (85, 64),
    (90, 74),
    (95, 86),
    (100, 100),
]


def _state_to_slider_volume(raw_state: int) -> int:
    """Converts YouTube Music's non-linear internal state back to the 0..100 UI slider value."""
    raw_state = max(0, min(100, int(raw_state)))
    best_slider = 0
    best_diff = 999
    for slider, state in _SLIDER_TO_STATE:
        diff = abs(state - raw_state)
        if diff < best_diff:
            best_diff = diff
            best_slider = slider
    return best_slider


@dataclass
class SongInfo:
    title: str
    artist: str
    album: str = ""
    duration: int = 0
    elapsed: int = 0
    is_paused: bool = True
    video_id: str = ""
    image_src: str = ""
    like_status: str = ""
    artist_id: str = ""
    album_id: str = ""


@dataclass
class QueueItem:
    index: int
    title: str
    artist: str
    duration: str
    video_id: str
    selected: bool = False
    album: str = ""
    artist_id: str = ""
    album_id: str = ""


@dataclass
class SearchResult:
    title: str
    artist: str
    duration: str
    video_id: str
    result_type: str = "Song"
    album: str = ""
    artist_id: str = ""
    album_id: str = ""
    playlist_id: str = ""
    browse_id: str = ""


@dataclass
class PlaylistEntry:
    title: str
    subtitle: str
    browse_id: str
    playlist_id: str


def _extract_runs_text(obj: Any) -> str:
    if not isinstance(obj, dict):
        return ""
    if "simpleText" in obj:
        return str(obj["simpleText"])
    runs = obj.get("runs")
    if isinstance(runs, list):
        return "".join(str(r.get("text", "")) for r in runs if isinstance(r, dict))
    return ""


def _extract_artist_album_ids(runs_container: Any) -> tuple[str, str, str]:
    """Returns (artist_id, album_id, album_name) from a runs container."""
    if not isinstance(runs_container, dict):
        return "", "", ""
    runs = runs_container.get("runs")
    if not isinstance(runs, list):
        return "", "", ""
    artist_id = ""
    album_id = ""
    album_name = ""
    for r in runs:
        if not isinstance(r, dict):
            continue
        b_ep = r.get("navigationEndpoint", {}).get("browseEndpoint", {})
        b_id = str(b_ep.get("browseId") or "")
        page_type = (
            b_ep.get("browseEndpointContextSupportedConfigs", {})
            .get("browseEndpointContextMusicConfig", {})
            .get("pageType", "")
        )
        txt = str(r.get("text") or "")
        if not artist_id and (page_type == "MUSIC_PAGE_TYPE_ARTIST" or b_id.startswith("UC")):
            artist_id = b_id
        elif not album_id and (page_type == "MUSIC_PAGE_TYPE_ALBUM" or b_id.startswith("MPREb_")):
            album_id = b_id
            album_name = txt
    return artist_id, album_id, album_name


class YTMusicClient:
    """HTTP + MPRIS + InnerTube client for YouTube Music Desktop."""

    def __init__(self, host: str | None = None, port: int | None = None) -> None:
        cfg_host, cfg_port, cfg_token = self._load_local_config()
        self.host = host or os.environ.get("YTM_HOST") or cfg_host or DEFAULT_HOST
        if self.host == "0.0.0.0":
            self.host = "127.0.0.1"
        self.port = port or int(os.environ.get("YTM_PORT", cfg_port or DEFAULT_PORT))
        self.base_url = f"http://{self.host}:{self.port}"
        self.token: str | None = cfg_token

    def _load_local_config(self) -> tuple[str | None, int | None, str | None]:
        try:
            if YTM_CONFIG_PATH.exists():
                data = json.loads(YTM_CONFIG_PATH.read_text(encoding="utf-8"))
                plugin = data.get("plugins", {}).get("api-server", {})
                return plugin.get("hostname"), plugin.get("port"), None
        except Exception:
            pass
        return None, None, None

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        timeout: float = 1.5,
        retry_auth: bool = True,
    ) -> tuple[int, Any]:
        url = f"{self.base_url}{path}"
        headers: dict[str, str] = {"Accept": "application/json"}
        data_bytes: bytes | None = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data_bytes = json.dumps(body).encode("utf-8")
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = resp.status
                raw = resp.read()
                if not raw or status == 204:
                    return status, None
                try:
                    return status, json.loads(raw.decode("utf-8"))
                except ValueError:
                    return status, raw.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 401 and retry_auth:
                if self.authenticate():
                    return self._request(method, path, body=body, timeout=timeout, retry_auth=False)
            return e.code, None
        except Exception:
            return 0, None

    def authenticate(self) -> bool:
        status, data = self._request("POST", f"/auth/{CLIENT_ID}", retry_auth=False)
        if status == 200 and isinstance(data, dict) and "accessToken" in data:
            self.token = data["accessToken"]
            return True
        return False

    def is_reachable(self) -> bool:
        status, _ = self._request("GET", "/api/v1/volume", timeout=0.6)
        return status in (200, 204)

    def get_song(self) -> SongInfo | None:
        status, data = self._request("GET", "/api/v1/song", timeout=0.8)
        if status == 200 and isinstance(data, dict) and data.get("title"):
            video_id = str(data.get("videoId") or "")
            image_src = str(data.get("imageSrc") or "")
            if not image_src and video_id:
                image_src = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
            return SongInfo(
                title=str(data.get("title", "Unknown")),
                artist=str(data.get("artist", "Unknown")),
                album=str(data.get("album") or ""),
                duration=int(data.get("songDuration") or 0),
                elapsed=int(data.get("elapsedSeconds") or 0),
                is_paused=bool(data.get("isPaused", False)),
                video_id=video_id,
                image_src=image_src,
            )
        return self._get_song_playerctl()

    def _get_song_playerctl(self) -> SongInfo | None:
        try:
            res = subprocess.run(
                [
                    "playerctl",
                    "-p",
                    "YoutubeMusic,chromium,mpv,%any",
                    "metadata",
                    "--format",
                    "{{status}}\t{{artist}}\t{{title}}\t{{album}}\t{{mpris:length}}\t{{position}}\t{{mpris:artUrl}}",
                ],
                capture_output=True,
                text=True,
                timeout=0.5,
            )
            if res.returncode != 0 or not res.stdout.strip():
                return None
            parts = res.stdout.strip().split("\t")
            while len(parts) < 7:
                parts.append("")
            p_status, artist, title, album, length_us, pos_us, art_url = parts[:7]
            if not title:
                return None
            duration = int(float(length_us) / 1_000_000) if length_us.isdigit() else 0
            elapsed = int(float(pos_us) / 1_000_000) if pos_us.isdigit() else 0
            return SongInfo(
                title=title,
                artist=artist or "YouTube Music",
                album=album,
                duration=duration,
                elapsed=elapsed,
                is_paused=(p_status != "Playing"),
                image_src=art_url,
            )
        except Exception:
            return None

    def get_volume(self) -> tuple[int, bool]:
        status, data = self._request("GET", "/api/v1/volume", timeout=0.6)
        if status == 200 and isinstance(data, dict):
            raw_state = int(data.get("state", 100))
            muted = bool(data.get("isMuted", False))
            return _state_to_slider_volume(raw_state), muted
        return 100, False

    def set_volume(self, volume: int) -> bool:
        vol = max(0, min(100, int(volume)))
        status, _ = self._request("POST", "/api/v1/volume", {"volume": vol})
        return status == 204

    def toggle_mute(self) -> bool:
        status, _ = self._request("POST", "/api/v1/toggle-mute")
        return status == 204

    def toggle_play(self) -> bool:
        status, _ = self._request("POST", "/api/v1/toggle-play")
        if status == 204:
            return True
        subprocess.run(["playerctl", "play-pause"], capture_output=True)
        return True

    def play(self) -> bool:
        status, _ = self._request("POST", "/api/v1/play")
        return status == 204

    def pause(self) -> bool:
        status, _ = self._request("POST", "/api/v1/pause")
        return status == 204

    def next_track(self) -> bool:
        status, _ = self._request("POST", "/api/v1/next")
        if status == 204:
            return True
        subprocess.run(["playerctl", "next"], capture_output=True)
        return True

    def previous_track(self) -> bool:
        status, _ = self._request("POST", "/api/v1/previous")
        if status == 204:
            return True
        subprocess.run(["playerctl", "previous"], capture_output=True)
        return True

    def seek_by(self, delta_seconds: int) -> bool:
        if delta_seconds < 0:
            status, _ = self._request("POST", "/api/v1/go-back", {"seconds": abs(delta_seconds)})
        else:
            status, _ = self._request("POST", "/api/v1/go-forward", {"seconds": delta_seconds})
        return status == 204

    def get_repeat_mode(self) -> str:
        status, data = self._request("GET", "/api/v1/repeat-mode", timeout=0.6)
        if status == 200 and isinstance(data, dict):
            return str(data.get("mode") or "NONE")
        return "NONE"

    def switch_repeat(self) -> bool:
        status, _ = self._request("POST", "/api/v1/switch-repeat", {"iteration": 1})
        return status == 204

    def shuffle(self) -> bool:
        status, _ = self._request("POST", "/api/v1/shuffle")
        return status == 204

    def get_like_state(self) -> str:
        status, data = self._request("GET", "/api/v1/like-state", timeout=0.6)
        if status == 200 and isinstance(data, dict) and data.get("state"):
            return str(data["state"])
        return "INDIFFERENT"

    def like(self) -> bool:
        status, _ = self._request("POST", "/api/v1/like")
        return status == 204

    def dislike(self) -> bool:
        status, _ = self._request("POST", "/api/v1/dislike")
        return status == 204

    def get_queue(self) -> list[QueueItem]:
        status, data = self._request("GET", "/api/v1/queue", timeout=1.5)
        if status != 200 or not isinstance(data, dict):
            return []
        raw_items = data.get("items")
        if not isinstance(raw_items, list):
            return []

        items: list[QueueItem] = []
        for idx, entry in enumerate(raw_items):
            if not isinstance(entry, dict):
                continue
            renderer = entry.get("playlistPanelVideoRenderer")
            if not renderer and "playlistPanelVideoWrapperRenderer" in entry:
                wrapper = entry.get("playlistPanelVideoWrapperRenderer", {})
                renderer = wrapper.get("primaryRenderer", {}).get("playlistPanelVideoRenderer")
            if not isinstance(renderer, dict):
                continue

            title = _extract_runs_text(renderer.get("title")) or "Unknown Title"
            artist = (
                _extract_runs_text(renderer.get("shortBylineText"))
                or _extract_runs_text(renderer.get("longBylineText"))
                or "Unknown Artist"
            )
            duration = _extract_runs_text(renderer.get("lengthText")) or "--:--"
            video_id = str(renderer.get("videoId") or "")
            selected = bool(renderer.get("selected", False))
            artist_id, album_id, album_name = _extract_artist_album_ids(
                renderer.get("longBylineText")
            )
            items.append(
                QueueItem(
                    index=idx,
                    title=title,
                    artist=artist,
                    duration=duration,
                    video_id=video_id,
                    selected=selected,
                    album=album_name,
                    artist_id=artist_id,
                    album_id=album_id,
                )
            )
        return items

    def set_queue_index(self, index: int) -> bool:
        status, _ = self._request("PATCH", "/api/v1/queue", {"index": index})
        if status == 204:
            time.sleep(0.15)
            song = self.get_song()
            if song and song.is_paused:
                self.play()
            return True
        return False

    def remove_from_queue(self, index: int) -> bool:
        status, _ = self._request("DELETE", f"/api/v1/queue/{index}")
        return status == 204

    def clear_queue(self) -> bool:
        status, _ = self._request("DELETE", "/api/v1/queue")
        return status == 204

    def add_to_queue(
        self, video_id: str, position: str = "INSERT_AFTER_CURRENT_VIDEO"
    ) -> bool:
        status, _ = self._request(
            "POST",
            "/api/v1/queue",
            {"videoId": video_id, "insertPosition": position},
        )
        return status == 204

    def open_url_in_player(self, url: str) -> bool:
        """Uses YoutubeMusic MPRIS OpenUri handler to load a watch/playlist URL and regenerate the queue."""
        res = subprocess.run(
            ["playerctl", "-p", "YoutubeMusic", "open", url],
            capture_output=True,
            timeout=2.0,
        )
        return res.returncode == 0

    def play_video(
        self,
        video_id: str,
        playlist_id: str = "",
        regenerate_queue: bool = True,
    ) -> bool:
        """Plays a track and regenerates the radio/playlist queue (or inserts next if fallback)."""
        if regenerate_queue and video_id:
            list_param = playlist_id if playlist_id else f"RDAMVM{video_id}"
            watch_url = f"https://music.youtube.com/watch?v={video_id}&list={list_param}"
            if self.open_url_in_player(watch_url):
                return True

        # Fallback if MPRIS OpenUri is unavailable
        q_before = self.get_queue()
        old_len = len(q_before)
        cur_idx = next((x.index for x in q_before if x.selected), -1)

        if not self.add_to_queue(video_id, "INSERT_AFTER_CURRENT_VIDEO"):
            return False

        expected_idx = cur_idx + 1 if cur_idx >= 0 else 0
        target_idx: int | None = None

        for _ in range(25):
            time.sleep(0.12)
            q_now = self.get_queue()
            if not q_now:
                continue
            if expected_idx < len(q_now) and q_now[expected_idx].video_id == video_id:
                target_idx = expected_idx
                break
            if len(q_now) > old_len:
                for item in q_now:
                    if item.video_id == video_id and item.index >= max(0, cur_idx):
                        target_idx = item.index
                        break
                if target_idx is not None:
                    break

        if target_idx is not None:
            return self.set_queue_index(target_idx)
        return self.next_track()

    def play_playlist(self, playlist_id: str, first_video_id: str = "") -> bool:
        """Starts playing a playlist/album and loads its full queue."""
        clean_pl = playlist_id[2:] if playlist_id.startswith("VL") else playlist_id
        if first_video_id:
            url = f"https://music.youtube.com/watch?v={first_video_id}&list={clean_pl}"
        else:
            url = f"https://music.youtube.com/playlist?list={clean_pl}"
        return self.open_url_in_player(url)

    def search(self, query: str) -> list[SearchResult]:
        status, data = self._request(
            "POST",
            "/api/v1/search",
            {"query": query, "params": SONGS_SEARCH_PARAMS},
            timeout=4.5,
        )
        results: list[SearchResult] = []
        if status == 200 and isinstance(data, dict):
            self._walk_search_results(data, results)

        if not results:
            status2, data2 = self._request(
                "POST", "/api/v1/search", {"query": query}, timeout=4.5
            )
            if status2 == 200 and isinstance(data2, dict):
                self._walk_search_results(data2, results)

        seen: set[str] = set()
        unique: list[SearchResult] = []
        for item in results:
            key = item.video_id or item.browse_id
            if key and key not in seen:
                seen.add(key)
                unique.append(item)
        return unique[:45]

    # --- InnerTube Authenticated Browse (Playlists, Albums, Artists) ---

    def _innertube_browse(self, browse_id: str) -> dict[str, Any] | None:
        if not YTM_COOKIES_PATH.exists():
            return None
        try:
            conn = sqlite3.connect(f"file:{YTM_COOKIES_PATH}?mode=ro", uri=True)
            try:
                cookies = {
                    name: val
                    for name, val in conn.execute(
                        "SELECT name, value FROM cookies WHERE host_key LIKE '%youtube.com' AND value != ''"
                    )
                }
            finally:
                conn.close()

            sapisid = cookies.get("SAPISID") or cookies.get("__Secure-3PAPISID", "")
            cookie_hdr = "; ".join(f"{k}={v}" for k, v in cookies.items())
            origin = "https://music.youtube.com"
            headers: dict[str, str] = {
                "Content-Type": "application/json",
                "Origin": origin,
                "X-Origin": origin,
                "X-Goog-AuthUser": "0",
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36",
            }
            if cookie_hdr:
                headers["Cookie"] = cookie_hdr
            if sapisid:
                ts = int(time.time())
                digest = hashlib.sha1(f"{ts} {sapisid} {origin}".encode("utf-8")).hexdigest()
                headers["Authorization"] = f"SAPISIDHASH {ts}_{digest}"

            body = {
                "context": {
                    "client": {
                        "clientName": "WEB_REMIX",
                        "clientVersion": "1.20261018.01.00",
                        "hl": "it",
                        "gl": "IT",
                    }
                },
                "browseId": browse_id,
            }
            req = urllib.request.Request(
                "https://music.youtube.com/youtubei/v1/browse?prettyPrint=false",
                data=json.dumps(body).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None

    def get_library_playlists(self) -> list[PlaylistEntry]:
        """Fetches the user's personal & saved playlists from YouTube Music Library."""
        data = self._innertube_browse("FEmusic_liked_playlists")
        if not data:
            return []
        playlists: list[PlaylistEntry] = []
        self._walk_playlists(data, playlists)
        return playlists

    def _walk_playlists(self, node: Any, out: list[PlaylistEntry]) -> None:
        if isinstance(node, dict):
            if "musicTwoRowItemRenderer" in node:
                m = node["musicTwoRowItemRenderer"]
                title = _extract_runs_text(m.get("title"))
                sub = _extract_runs_text(m.get("subtitle"))
                bid = str(
                    m.get("navigationEndpoint", {})
                    .get("browseEndpoint", {})
                    .get("browseId")
                    or ""
                )
                if title and bid and bid != "VLSE":
                    clean_pl = bid[2:] if bid.startswith("VL") else bid
                    out.append(
                        PlaylistEntry(
                            title=title,
                            subtitle=sub,
                            browse_id=bid,
                            playlist_id=clean_pl,
                        )
                    )
                return
            for v in node.values():
                self._walk_playlists(v, out)
        elif isinstance(node, list):
            for x in node:
                self._walk_playlists(x, out)

    def get_browse_items(self, browse_id: str) -> list[SearchResult]:
        """Fetches tracks (and albums for artists) from a Playlist (VL...), Album (MPREb_...), or Artist (UC...)."""
        data = self._innertube_browse(browse_id)
        if not data:
            return []
        items: list[SearchResult] = []
        self._walk_search_results(data, items)

        # If browsing an Artist (UC...), also append their Albums/Singles from musicTwoRowItemRenderer
        if browse_id.startswith("UC"):
            albums: list[PlaylistEntry] = []
            self._walk_playlists(data, albums)
            for alb in albums:
                if alb.browse_id.startswith("MPREb_"):
                    items.append(
                        SearchResult(
                            title=f"💿 [Album] {alb.title}",
                            artist=alb.subtitle,
                            duration="Album",
                            video_id="",
                            result_type="Album",
                            album=alb.title,
                            album_id=alb.browse_id,
                            browse_id=alb.browse_id,
                        )
                    )
        return items

    def _walk_search_results(self, node: Any, out: list[SearchResult]) -> None:
        if isinstance(node, dict):
            if "musicCardShelfRenderer" in node:
                top_item = self._parse_card_shelf(node["musicCardShelfRenderer"])
                if top_item:
                    out.append(top_item)
            if "musicResponsiveListItemRenderer" in node:
                item = self._parse_responsive_item(node["musicResponsiveListItemRenderer"])
                if item:
                    out.append(item)
                return
            for val in node.values():
                self._walk_search_results(val, out)
        elif isinstance(node, list):
            for elem in node:
                self._walk_search_results(elem, out)

    def _parse_card_shelf(self, card: dict[str, Any]) -> SearchResult | None:
        if not isinstance(card, dict):
            return None
        title_obj = card.get("title", {})
        title = _extract_runs_text(title_obj)
        runs = title_obj.get("runs", [])
        video_id = ""
        playlist_id = ""
        if runs and isinstance(runs[0], dict):
            w_ep = runs[0].get("navigationEndpoint", {}).get("watchEndpoint", {})
            video_id = str(w_ep.get("videoId") or "")
            playlist_id = str(w_ep.get("playlistId") or "")
        if not title or not video_id:
            return None

        sub_obj = card.get("subtitle", {})
        subtitle = _extract_runs_text(sub_obj)
        artist_id, album_id, album_name = _extract_artist_album_ids(sub_obj)

        parts = [p.strip() for p in subtitle.split("•") if p.strip()]
        duration = "--:--"
        if parts and any(ch.isdigit() for ch in parts[-1]) and ":" in parts[-1]:
            duration = parts.pop()
        if parts and parts[0] in ("Song", "Video", "Brano", "Episode", "Podcast"):
            parts.pop(0)
        artist = " • ".join(parts) if parts else "YouTube Music"
        return SearchResult(
            title=title,
            artist=artist,
            duration=duration,
            video_id=video_id,
            result_type="Top",
            album=album_name,
            artist_id=artist_id,
            album_id=album_id,
            playlist_id=playlist_id,
        )

    def _parse_responsive_item(self, renderer: dict[str, Any]) -> SearchResult | None:
        if not isinstance(renderer, dict):
            return None
        video_id = str(renderer.get("playlistItemData", {}).get("videoId") or "")
        overlay_watch = (
            renderer.get("overlay", {})
            .get("musicItemThumbnailOverlayRenderer", {})
            .get("content", {})
            .get("musicPlayButtonRenderer", {})
            .get("playNavigationEndpoint", {})
            .get("watchEndpoint", {})
        )
        if not video_id:
            video_id = str(overlay_watch.get("videoId") or "")
        playlist_id = str(overlay_watch.get("playlistId") or "")
        if not video_id:
            return None

        flex_cols = renderer.get("flexColumns", [])
        title = ""
        subtitle = ""
        artist_id = ""
        album_id = ""
        album_name = ""

        if len(flex_cols) > 0:
            col0 = flex_cols[0].get("musicResponsiveListItemFlexColumnRenderer", {})
            title = _extract_runs_text(col0.get("text"))
        if len(flex_cols) > 1:
            col1 = flex_cols[1].get("musicResponsiveListItemFlexColumnRenderer", {})
            subtitle = _extract_runs_text(col1.get("text"))
            a_id, alb_id, alb_name = _extract_artist_album_ids(col1.get("text"))
            artist_id = a_id or artist_id
            album_id = alb_id or album_id
            album_name = alb_name or album_name
        if len(flex_cols) > 2:
            col2 = flex_cols[2].get("musicResponsiveListItemFlexColumnRenderer", {})
            a_id2, alb_id2, alb_name2 = _extract_artist_album_ids(col2.get("text"))
            artist_id = artist_id or a_id2
            album_id = album_id or alb_id2
            album_name = album_name or alb_name2

        if not title:
            return None

        parts = [p.strip() for p in subtitle.split("•") if p.strip()]
        duration = "--:--"
        result_type = "Track"
        if parts and any(ch.isdigit() for ch in parts[-1]) and ":" in parts[-1]:
            duration = parts.pop()
        if parts and parts[0] in ("Song", "Video", "Brano", "Episode", "Podcast"):
            result_type = parts.pop(0)
        artist = " • ".join(parts) if parts else ""

        # Check fixedColumns for duration (used in Playlist and Album views)
        fixed_cols = renderer.get("fixedColumns", [])
        if duration == "--:--" and fixed_cols:
            fc0 = fixed_cols[0].get("musicResponsiveListItemFixedColumnRenderer", {})
            fixed_dur = _extract_runs_text(fc0.get("text")).strip()
            if fixed_dur:
                duration = fixed_dur

        return SearchResult(
            title=title,
            artist=artist,
            duration=duration,
            video_id=video_id,
            result_type=result_type,
            album=album_name,
            artist_id=artist_id,
            album_id=album_id,
            playlist_id=playlist_id,
        )


def ensure_api_server_enabled(hide_window: bool = True) -> bool:
    """Enables the api-server and shortcuts/MPRIS plugins in ~/.config/YouTube Music/config.json."""
    YTM_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if YTM_CONFIG_PATH.exists():
        try:
            data = json.loads(YTM_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = {}

    options = data.setdefault("options", {})
    options["resumeOnStart"] = False
    data["url"] = "https://music.youtube.com"
    if hide_window:
        data["window-maximized"] = False
        options["appVisible"] = False
        options["tray"] = False

    plugins = data.setdefault("plugins", {})
    api_cfg = plugins.get("api-server")
    if not isinstance(api_cfg, dict):
        api_cfg = {}

    api_cfg["enabled"] = True
    api_cfg.setdefault("hostname", DEFAULT_HOST)
    api_cfg.setdefault("port", DEFAULT_PORT)
    api_cfg["authStrategy"] = "NONE"
    api_cfg.setdefault("secret", "ytmusic-local-tui")
    auth_clients = api_cfg.get("authorizedClients", [])
    if CLIENT_ID not in auth_clients:
        auth_clients.append(CLIENT_ID)
    api_cfg["authorizedClients"] = auth_clients
    plugins["api-server"] = api_cfg

    # Enable shortcuts plugin so YoutubeMusic MPRIS OpenUri handler is active
    shortcuts_cfg = plugins.get("shortcuts")
    if not isinstance(shortcuts_cfg, dict):
        shortcuts_cfg = {}
    shortcuts_cfg["enabled"] = True
    shortcuts_cfg.setdefault("overrideMediaKeys", False)
    plugins["shortcuts"] = shortcuts_cfg

    YTM_CONFIG_PATH.write_text(json.dumps(data, indent="\t") + "\n", encoding="utf-8")
    return True


def is_ytm_process_running() -> bool:
    res = subprocess.run(
        ["pgrep", "-f", "/usr/lib/youtube-music"],
        capture_output=True,
    )
    return res.returncode == 0


def ensure_background_daemon() -> bool:
    """Starts YouTube Music Desktop hidden in the background if not already running."""
    if is_ytm_process_running():
        return False
    ensure_api_server_enabled(hide_window=True)
    subprocess.Popen(
        ["youtube-music"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return True
