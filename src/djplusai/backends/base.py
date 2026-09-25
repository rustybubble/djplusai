"""Backend interface: how djplusai reaches a (real or simulated) Mixxx."""

from __future__ import annotations

import abc
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..library import Track


class MixxxError(RuntimeError):
    pass


class NotConnected(MixxxError):
    pass


def deck_group(deck: int) -> str:
    return f"[Channel{deck}]"


def eq_group(deck: int) -> str:
    return f"[EqualizerRack1_[Channel{deck}]_Effect1]"


def quick_effect_group(deck: int) -> str:
    return f"[QuickEffectRack1_[Channel{deck}]]"


@dataclass
class DeckState:
    number: int
    values: dict[str, float] = field(default_factory=dict)
    eq: list[float] = field(default_factory=lambda: [1.0, 1.0, 1.0])
    filter: float = 0.5
    artist: str = ""
    title: str = ""
    updated_at: float = 0.0  # backend clock time of the last update
    track: Track | None = None  # what djplusai loaded here, if known

    def v(self, key: str, default: float = 0.0) -> float:
        return float(self.values.get(key, default))

    @property
    def playing(self) -> bool:
        return self.v("play") > 0.5

    @property
    def loaded(self) -> bool:
        return self.v("track_loaded") > 0.5 or self.v("duration") > 0

    @property
    def duration(self) -> float:
        return self.v("duration")

    @property
    def speed(self) -> float:
        """Playback speed relative to the file (1.0 = original tempo)."""
        file_bpm = self.v("file_bpm")
        bpm = self.v("bpm")
        if file_bpm > 0 and bpm > 0:
            return bpm / file_bpm
        return 1.0

    @property
    def beat_period(self) -> float:
        """Seconds of *track time* per beat (independent of the tempo fader)."""
        file_bpm = self.v("file_bpm")
        return 60.0 / file_bpm if file_bpm > 0 else 0.0

    @property
    def samplerate(self) -> float:
        return self.v("track_samplerate", 44100.0) or 44100.0

    def position(self, now: float | None = None) -> float:
        """Track position in seconds, extrapolated to ``now`` while playing."""
        pos = self.v("playposition") * self.duration
        if now is not None and self.playing and self.updated_at:
            pos += max(0.0, now - self.updated_at) * self.speed
        return pos

    def beat_anchor(self, now: float | None = None) -> tuple[float, float] | None:
        """(time of the most recent beat, beat period) in track seconds."""
        period = self.beat_period
        if period <= 0 or not self.loaded:
            return None
        pos = self.v("playposition") * self.duration
        anchor = pos - self.v("beat_distance") * period
        return anchor, period

    def seconds_to_samples(self, seconds: float) -> float:
        # Mixxx's position controls count interleaved stereo samples.
        return seconds * self.samplerate * 2.0

    def samples_to_seconds(self, samples: float) -> float:
        return samples / (self.samplerate * 2.0)

    def summary(self, now: float | None = None) -> dict[str, Any]:
        loop = None
        if self.v("loop_start_position", -1) >= 0 and self.v("loop_end_position", -1) >= 0:
            loop = {
                "enabled": self.v("loop_enabled") > 0.5,
                "start_s": round(self.samples_to_seconds(self.v("loop_start_position")), 3),
                "end_s": round(self.samples_to_seconds(self.v("loop_end_position")), 3),
            }
        track = None
        if self.track is not None:
            track = self.track.brief()
        elif self.artist or self.title:
            track = {"artist": self.artist, "title": self.title}
        return {
            "deck": self.number,
            "track": track,
            "loaded": self.loaded,
            "playing": self.playing,
            "position_s": round(self.position(now), 2),
            "duration_s": round(self.duration, 2),
            "remaining_s": round(max(0.0, self.duration - self.position(now)), 2),
            "bpm": round(self.v("bpm"), 2),
            "file_bpm": round(self.v("file_bpm"), 2),
            "sync": self.v("sync_enabled") > 0.5,
            "keylock": self.v("keylock") > 0.5,
            "volume": round(self.v("volume"), 3),
            "pregain": round(self.v("pregain", 1.0), 3),
            "eq_low_mid_high": [round(x, 3) for x in self.eq],
            "filter": round(self.filter, 3),
            "loop": loop,
        }


@dataclass
class MixxxState:
    decks: dict[int, DeckState] = field(default_factory=dict)
    master: dict[str, float] = field(default_factory=dict)
    last_update: float = 0.0

    def deck(self, n: int) -> DeckState:
        if n not in self.decks:
            self.decks[n] = DeckState(number=n)
        return self.decks[n]

    def apply_deck(self, n: int, s: dict[str, Any], now: float) -> None:
        d = self.deck(n)
        meta = s.get("meta")
        d.values.update({k: float(v) for k, v in s.items() if isinstance(v, (int, float))})
        if isinstance(s.get("eq"), list):
            d.eq = [float(x) for x in s["eq"]]
        if isinstance(s.get("filter"), (int, float)):
            d.filter = float(s["filter"])
        if isinstance(meta, dict):
            d.artist = str(meta.get("artist", ""))
            d.title = str(meta.get("title", ""))
        d.updated_at = now
        self.last_update = now


@dataclass
class LoadResult:
    ok: bool
    deck: int
    track: Track | None
    message: str = ""
    verified: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "deck": self.deck,
            "track": self.track.brief() if self.track else None,
            "verified": self.verified,
            "message": self.message,
        }


class Backend(abc.ABC):
    """Common operations. Subclasses implement :meth:`request` and track loading."""

    name = "backend"
    num_decks = 4

    def __init__(self) -> None:
        self.state = MixxxState()
        self.time_scale = 1.0  # >1 makes a simulated Mixxx run faster than wall-clock

    # --- lifecycle -----------------------------------------------------
    async def start(self) -> None:  # pragma: no cover - trivial default
        pass

    async def close(self) -> None:  # pragma: no cover - trivial default
        pass

    @property
    @abc.abstractmethod
    def connected(self) -> bool: ...

    # --- clock -----------------------------------------------------------
    def now(self) -> float:
        """Backend clock in seconds (scaled for simulations)."""
        return time.monotonic() * self.time_scale

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds) / self.time_scale)

    # --- transport-level request --------------------------------------
    @abc.abstractmethod
    async def request(self, msg: dict[str, Any]) -> Any: ...

    # --- control operations -------------------------------------------
    async def get(self, group: str, key: str) -> float:
        return float(await self.request({"op": "get", "g": group, "k": key}))

    async def set(self, group: str, key: str, value: float) -> float:
        return float(await self.request({"op": "set", "g": group, "k": key, "v": float(value)}))

    async def press(self, group: str, key: str) -> None:
        await self.request({"op": "press", "g": group, "k": key})

    async def batch(self, ops: list[dict[str, Any]]) -> list[Any]:
        return list(await self.request({"op": "batch", "ops": ops}))

    async def set_loop(self, group: str, start_samples: float, end_samples: float, enable: bool = True) -> Any:
        return await self.request(
            {"op": "loop", "g": group, "s": float(start_samples), "e": float(end_samples), "enable": enable}
        )

    async def ramp(self, group: str, key: str, to: float, seconds: float, curve: str = "linear") -> None:
        ms = max(1.0, seconds * 1000.0 / self.time_scale)
        await self.request({"op": "ramp", "g": group, "k": key, "to": float(to), "ms": ms, "curve": curve})

    async def cancel_ramps(self, group: str | None = None) -> None:
        await self.request({"op": "cancel_ramps", "g": group})

    async def refresh(self) -> MixxxState:
        full = await self.request({"op": "state"})
        now = self.now()
        for n, s in (full.get("decks") or {}).items():
            self.state.apply_deck(int(n), s, now)
        self.state.master.update(full.get("master") or {})
        return self.state

    # --- library --------------------------------------------------------
    @abc.abstractmethod
    async def load_track(self, deck: int, track: Track) -> LoadResult: ...

    # --- waiting --------------------------------------------------------
    async def wait_until(
        self,
        predicate: Callable[[], bool] | Callable[[], Awaitable[bool]],
        timeout: float | None = 900.0,
        poll: float = 0.02,
    ) -> bool:
        """Poll ``predicate`` (backend-clock seconds) until it is true or the timeout expires."""
        deadline = None if timeout is None else self.now() + timeout
        while True:
            result = predicate()
            if asyncio.iscoroutine(result):
                result = await result
            if result:
                return True
            if deadline is not None and self.now() >= deadline:
                return False
            await self.sleep(poll)
