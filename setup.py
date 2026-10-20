from setuptools import find_packages, setup

setup(
    name="yt-music-tui",
    version="0.1.0",
    description="Lightweight Terminal UI (TUI) and Waybar widget wrapper for YouTube Music Desktop",
    author="LucaPezzato",
    author_email="luca@pezzato.dev",
    packages=find_packages(),
    entry_points={
        "console_scripts": [
            "yt-music-tui=yt_music_tui.cli:main",
        ],
    },
    python_requires=">=3.10",
)
