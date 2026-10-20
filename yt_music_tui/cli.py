"""CLI entrypoint for yt-music-tui (TUI, Waybar widget JSON, and player controls)."""

from __future__ import annotations

import argparse
import html
import json
import sys

from .client import YTMusicClient, ensure_api_server_enabled, ensure_background_daemon
from .tui import run_tui


def output_waybar(client: YTMusicClient, max_len: int = 38) -> None:
    song = client.get_song()
    if not song:
        print(
            json.dumps(
                {
                    "text": "󰎆 YT Music",
                    "tooltip": "YouTube Music (click to open TUI)",
                    "class": "stopped",
                }
            )
        )
        return

    icon = "" if song.is_paused else "󰎆"
    label = f"{song.artist} - {song.title}"
    if len(label) > max_len:
        label = label[: max_len - 1] + "…"

    text = html.escape(f"{icon} {label}")
    tooltip_lines = [
        f"Title:  {song.title}",
        f"Artist: {song.artist}",
    ]
    if song.album:
        tooltip_lines.append(f"Album:  {song.album}")
    status_str = "Paused" if song.is_paused else "Playing"
    tooltip_lines.append(f"Status: {status_str}")

    print(
        json.dumps(
            {
                "text": text,
                "tooltip": "\n".join(tooltip_lines),
                "class": "paused" if song.is_paused else "playing",
            }
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="yt-music-tui",
        description="Terminal UI and Waybar wrapper for YouTube Music Desktop.",
    )
    parser.add_argument("--host", default=None, help="API server hostname (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="API server port (default: 26538)")

    sub = parser.add_subparsers(dest="command")
    sub.add_parser("tui", help="Open interactive Terminal UI (default)")
    wb = sub.add_parser("waybar", help="Output JSON status for Waybar custom module")
    wb.add_argument("--max-len", type=int, default=38, help="Max label length")

    sub.add_parser("toggle", help="Toggle play/pause")
    sub.add_parser("play", help="Resume playback")
    sub.add_parser("pause", help="Pause playback")
    sub.add_parser("next", help="Next track")
    sub.add_parser("prev", help="Previous track")
    sub.add_parser("like", help="Like current track")
    sub.add_parser("dislike", help="Dislike current track")
    sub.add_parser("vol-up", help="Increase volume by 5%%")
    sub.add_parser("vol-down", help="Decrease volume by 5%%")
    sub.add_parser("status", help="Print current track info")
    sub.add_parser("daemon", help="Start YouTube Music Desktop hidden in background if not running")

    setup_p = sub.add_parser(
        "setup", help="Enable api-server plugin in YouTube Music Desktop config.json"
    )
    setup_p.add_argument(
        "--background",
        action="store_true",
        help="Also configure YouTube Music Desktop to start hidden in background/tray",
    )

    args = parser.parse_args(argv)
    client = YTMusicClient(host=args.host, port=args.port)

    cmd = args.command or "tui"
    if cmd == "tui":
        run_tui(client)
        return 0
    if cmd == "waybar":
        output_waybar(client, max_len=args.max_len)
        return 0
    if cmd == "toggle":
        client.toggle_play()
        return 0
    if cmd == "play":
        client.play()
        return 0
    if cmd == "pause":
        client.pause()
        return 0
    if cmd == "next":
        client.next_track()
        return 0
    if cmd == "prev":
        client.previous_track()
        return 0
    if cmd == "like":
        client.like()
        return 0
    if cmd == "dislike":
        client.dislike()
        return 0
    if cmd == "vol-up":
        vol, _ = client.get_volume()
        client.set_volume(min(100, vol + 5))
        return 0
    if cmd == "vol-down":
        vol, _ = client.get_volume()
        client.set_volume(max(0, vol - 5))
        return 0
    if cmd == "status":
        song = client.get_song()
        if not song:
            print("Stopped")
        else:
            state = "Paused" if song.is_paused else "Playing"
            print(f"[{state}] {song.artist} - {song.title}")
        return 0
    if cmd == "daemon":
        ensure_background_daemon()
        return 0
    if cmd == "setup":
        ensure_api_server_enabled(hide_window=args.background)
        print("Configured ~/.config/YouTube Music/config.json with api-server enabled.")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
