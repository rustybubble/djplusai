"""Loading a specific library track into a Mixxx deck.

Stock Mixxx has no control for "load file X into deck N" - controllers can only
load the track *selected in the library view*. So we:

1. focus Mixxx's library search box (``[Library],focused_widget = 1``, via MIDI),
2. type a precise search query with OS-level keystrokes (xdotool / osascript /
   pynput),
3. focus the track table, select the first result and press
   ``[ChannelN],LoadSelectedTrack``,
4. verify the loaded track (title/artist from ``engine.getPlayer`` on newer Mixxx,
   otherwise duration and BPM) and step through further results if needed.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import sys

from .backends.base import Backend, LoadResult, deck_group
from .library import Track, normalize

log = logging.getLogger(__name__)

FOCUS_SEARCH = 1
FOCUS_TRACKS = 3


def search_query(track: Track) -> str:
    """A Mixxx library search that should match exactly this track."""

    def clean(s: str) -> str:
        return s.replace('"', " ").strip()

    parts = []
    if track.title:
        parts.append(f'title:"{clean(track.title)}"')
    if track.artist:
        parts.append(f'artist:"{clean(track.artist)}"')
    return " ".join(parts) or clean(track.display)


class Typist:
    """Types text into Mixxx's focused widget with OS-level keystrokes."""

    def __init__(self, app_name: str = "Mixxx") -> None:
        self.app_name = app_name

    def available(self) -> str | None:
        if sys.platform == "darwin" and shutil.which("osascript"):
            return "osascript"
        if sys.platform.startswith("linux") and shutil.which("xdotool"):
            return "xdotool"
        try:
            import pynput  # noqa: F401  # type: ignore

            return "pynput"
        except ImportError:
            return None

    def _method(self) -> str:
        method = self.available()
        if method is None:
            raise RuntimeError(
                "no keyboard automation available: install xdotool (Linux/X11), "
                "or pip install 'djplusai[keyboard]'"
            )
        return method

    async def activate(self) -> None:
        """Bring the Mixxx window to the front so it receives keystrokes."""
        method = self._method()
        if method == "xdotool":
            cmd = ["xdotool", "search", "--onlyvisible", "--class", self.app_name.lower(), "windowactivate", "--sync"]
        elif method == "osascript":
            cmd = ["osascript", "-e", f'tell application "{self.app_name}" to activate']
        else:
            return  # pynput cannot raise windows; Mixxx must already be in front
        await asyncio.to_thread(subprocess.run, cmd, check=False, capture_output=True, timeout=5)

    async def replace_text(self, text: str) -> None:
        """Select everything in the focused text box and type ``text`` over it."""
        await asyncio.to_thread(getattr(self, f"_type_{self._method()}"), text)

    def _type_xdotool(self, text: str) -> None:
        subprocess.run(["xdotool", "key", "--clearmodifiers", "ctrl+a", "BackSpace"], check=True, timeout=5)
        subprocess.run(["xdotool", "type", "--delay", "4", "--", text], check=True, timeout=30)

    def _type_osascript(self, text: str) -> None:
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        script = (
            'tell application "System Events"\n'
            '  keystroke "a" using command down\n'
            "  key code 51\n"
            f'  keystroke "{escaped}"\n'
            "end tell"
        )
        subprocess.run(["osascript", "-e", script], check=True, capture_output=True, timeout=30)

    def _type_pynput(self, text: str) -> None:
        from pynput.keyboard import Controller, Key  # type: ignore

        kb = Controller()
        mod = Key.cmd if sys.platform == "darwin" else Key.ctrl
        with kb.pressed(mod):
            kb.press("a")
            kb.release("a")
        kb.press(Key.backspace)
        kb.release(Key.backspace)
        kb.type(text)


class LibrarySearchLoader:
    def __init__(self, backend: Backend, typist: Typist | None = None, max_candidates: int = 5) -> None:
        self.backend = backend
        self.typist = typist or Typist()
        self.max_candidates = max_candidates

    async def load(self, deck: int, track: Track) -> LoadResult:
        b = self.backend
        g = deck_group(deck)
        if await b.get(g, "play") > 0.5:
            return LoadResult(False, deck, track, f"deck {deck} is playing; pause it or pick another deck")
        try:
            await self.typist.activate()
            await b.set("[Library]", "focused_widget", FOCUS_SEARCH)
            await asyncio.sleep(0.15)
            await self.typist.replace_text(search_query(track))
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            return LoadResult(
                False,
                deck,
                track,
                f"Could not type into Mixxx's search box ({exc}). Load '{track.display}' onto deck {deck} manually.",
            )
        await asyncio.sleep(0.6)  # Mixxx debounces search input
        await b.set("[Library]", "focused_widget", FOCUS_TRACKS)
        await asyncio.sleep(0.1)
        for attempt in range(self.max_candidates):
            await b.press("[Library]", "MoveDown")
            await b.press(g, "LoadSelectedTrack")
            ok, how = await self._verify(deck, track)
            if ok:
                b.state.deck(deck).track = track
                return LoadResult(True, deck, track, f"loaded and verified by {how}", verified=True)
            if attempt == 0 and how == "unverifiable":
                b.state.deck(deck).track = track
                return LoadResult(True, deck, track, "loaded (could not verify metadata)", verified=False)
        return LoadResult(False, deck, track, "the library search did not surface this track")

    async def _verify(self, deck: int, track: Track) -> tuple[bool, str]:
        b = self.backend
        g = deck_group(deck)

        async def loaded() -> bool:
            return await b.get(g, "duration") > 0

        await b.wait_until(loaded, timeout=5.0, poll=0.1)
        await asyncio.sleep(0.3)  # let metadata arrive with the next state push
        await b.refresh()
        d = b.state.deck(deck)
        if d.title:
            same = normalize(d.title) == normalize(track.title) and (
                not track.artist or normalize(track.artist)[:12] in normalize(d.artist) or not d.artist
            )
            return same, "title/artist"
        if track.duration and d.duration:
            close_dur = abs(d.duration - track.duration) < 1.5
            close_bpm = not track.bpm or not d.v("file_bpm") or abs(d.v("file_bpm") - track.bpm) < 0.5
            return close_dur and close_bpm, "duration/bpm"
        return False, "unverifiable"
