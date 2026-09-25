"""Build a ready-to-use DJ (backend + library + lyrics + tools) from configuration."""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from .analysis import Analyzer
from .analysis import available as analysis_available
from .backends.base import Backend
from .controller import DJ
from .library import Library, MixxxLibrary, Track
from .lyrics import LyricsProvider, parse_lrc
from .recommend import OpportunityWatcher, Recommender
from .tools import DJTools


def mixxx_settings_dir() -> Path:
    env = os.environ.get("MIXXX_SETTINGS_DIR")
    if env:
        return Path(env).expanduser()
    home = Path.home()
    if sys.platform == "darwin":
        sandboxed = home / "Library/Containers/org.mixxx.mixxx/Data/Library/Application Support/Mixxx"
        return sandboxed if sandboxed.exists() else home / "Library/Application Support/Mixxx"
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local")) / "Mixxx"
    return home / ".mixxx"


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    return Path(base) / "djplusai"


def install_mapping(target_dir: Path | None = None) -> list[Path]:
    """Copy the DJPlusAI controller mapping into Mixxx's user controllers folder."""
    target = target_dir or (mixxx_settings_dir() / "controllers")
    target.mkdir(parents=True, exist_ok=True)
    written = []
    pkg = resources.files("djplusai") / "mixxx_mapping"
    for name in ("DJPlusAI.midi.xml", "DJPlusAI.js"):
        with resources.as_file(pkg / name) as src:
            dst = target / name
            shutil.copyfile(src, dst)
            written.append(dst)
    return written


# ---------------------------------------------------------------- demo data
# Invented tracks and lyrics so the simulator works out of the box, across genres.
_DEMO = [
    # id, artist, title, genre, bpm, key, duration, first_beat, lrc
    (1, "Demo MC", "Gold Watch", "Hip-Hop", 142.0, "5A", 200.0, 0.40,
     "[00:10.00]Yeah, yeah\n[00:18.00]Woke up early with the city on my mind\n"
     "[00:24.50]Gold watch shining, I'm just racing with the time\n[00:31.00]Every rollie tick is money on the line\n"
     "[00:37.50]Keep it moving, never looking behind\n[01:05.00]Rollie on my wrist and the night is mine\n"),
    (2, "Demo Duo", "Tick Tock Rollie", "Hip-Hop", 144.0, "6A", 190.0, 0.20,
     "[00:08.00]Tick tock rollie, tick tock\n[00:15.00]We don't ever stop\n"),
    (3, "Night Shift", "Warehouse Sunrise", "House", 124.0, "8A", 360.0, 0.10,
     "[01:00.00]Feel the sunrise\n[01:30.00]Hold on, hold on\n"),
    (4, "Night Shift", "Deeper Room", "House", 125.0, "9A", 330.0, 0.05, ""),
    (5, "Rinse Crew", "Amen Rider", "Drum & Bass", 174.0, "11A", 300.0, 0.00, ""),
    (6, "Luna Verde", "Calor", "Reggaeton", 96.0, "4A", 210.0, 0.25,
     "[00:20.00]Siente el calor\n[00:40.00]Baila conmigo\n"),
    (7, "Paper Lanterns", "Summer Radio", "Pop", 118.0, "8B", 205.0, 0.15,
     "[00:15.00]Turn the summer radio up\n[00:45.00]We sing along\n"),
    (8, "Robot Choir", "Robot Heart", "Techno", 130.0, "8A", 400.0, 0.00, ""),
    (9, "Demo Crew", "Money On The Line", "Hip-Hop", 140.0, "5A", 185.0, 0.30,
     "[00:12.00]Money on the line, money on the line\n[00:19.00]Every single time\n"
     "[00:26.00]Money on the line, yeah we shine\n"),
]


def demo_library() -> tuple[Library, dict[int, str]]:
    tracks = []
    lrcs = {}
    for tid, artist, title, genre, bpm, key, dur, fb, lrc in _DEMO:
        tracks.append(Track(tid, artist, title, genre=genre, bpm=bpm, key=key, duration=dur, first_beat=fb))
        if lrc:
            lrcs[tid] = lrc
    return Library(tracks), lrcs


class InlineLyricsProvider(LyricsProvider):
    def __init__(self, lrcs: dict[int, str], **kw) -> None:
        super().__init__(use_lrclib=False, **kw)
        self.lrcs = lrcs

    def _from_files(self, track: Track):
        text = self.lrcs.get(track.id)
        return parse_lrc(text, source="demo") if text else super()._from_files(track)


@dataclass
class Runtime:
    backend: Backend
    dj: DJ
    tools: DJTools
    watch: bool = True

    async def start(self) -> None:
        await self.backend.start()
        if self.watch and self.tools.watcher:
            self.tools.watcher.start()

    async def close(self) -> None:
        if self.tools.watcher:
            await self.tools.watcher.stop()
        self.tools.jobs.cancel()
        await self.backend.close()


def build_runtime(
    backend: str | None = None,
    db_path: str | None = None,
    port_name: str | None = None,
    lyrics_dir: str | None = None,
    time_scale: float = 1.0,
    use_whisper: bool | None = None,
    watch: bool | None = None,
) -> Runtime:
    backend = backend or os.environ.get("DJPLUSAI_BACKEND", "midi")
    lyrics_dir = lyrics_dir or os.environ.get("DJPLUSAI_LYRICS_DIR")
    if use_whisper is None:
        use_whisper = os.environ.get("DJPLUSAI_WHISPER", "") not in ("", "0", "false")
    if backend == "sim":
        from .backends.sim import SimBackend

        be: Backend = SimBackend(time_scale=time_scale)
        if db_path:
            library: Library = MixxxLibrary(db_path)
            lyrics: LyricsProvider = LyricsProvider(lyrics_dir, cache_dir() / "lyrics")
        else:
            library, lrcs = demo_library()
            lyrics = InlineLyricsProvider(lrcs, lyrics_dir=lyrics_dir)
    elif backend == "midi":
        from .backends.midi import MidiBackend
        from .loader import LibrarySearchLoader

        db = Path(db_path or os.environ.get("DJPLUSAI_MIXXX_DB") or mixxx_settings_dir() / "mixxxdb.sqlite")
        library = MixxxLibrary(db) if db.exists() else Library()
        lyrics = LyricsProvider(lyrics_dir, cache_dir() / "lyrics")
        midi = MidiBackend(port_name=port_name or os.environ.get("DJPLUSAI_MIDI_PORT", "DJPlusAI"))
        midi.loader = LibrarySearchLoader(midi)
        be = midi
    else:
        raise ValueError("backend must be 'midi' or 'sim'")
    dj = DJ(be, library, lyrics, use_whisper=use_whisper)
    analyzer = Analyzer(cache_dir() / "analysis") if analysis_available() else None
    rec = Recommender(dj, analyzer)
    if watch is None:
        watch = os.environ.get("DJPLUSAI_WATCH", "1") not in ("0", "false", "")
    return Runtime(be, dj, DJTools(dj, recommender=rec, watcher=OpportunityWatcher(rec)), watch)
