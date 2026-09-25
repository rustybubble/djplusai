"""An in-process Mixxx stand-in: lets you try agents and run tests without Mixxx.

It emulates the subset of Mixxx's control objects djplusai uses (transport,
tempo/sync, loops, EQ, filter, volume, ramps) and the DJPlusAI script's
command set, with the same semantics as the real engine where it matters
(e.g. a loop armed ahead of the playhead only engages when it is reached).
"""

from __future__ import annotations

import asyncio
import math
import re
from dataclasses import dataclass, field
from typing import Any

from ..library import Track
from .base import Backend, LoadResult, MixxxError

_DECK = re.compile(r"^\[Channel(\d+)\]$")
_EQ = re.compile(r"^\[EqualizerRack1_\[Channel(\d+)\]_Effect1\]$")
_QFX = re.compile(r"^\[QuickEffectRack1_\[Channel(\d+)\]\]$")


@dataclass
class _Deck:
    n: int
    track: Track | None = None
    pos: float = 0.0
    playing: bool = False
    speed: float = 1.0
    volume: float = 1.0
    pregain: float = 1.0
    loop_start: float = -1.0  # samples, -1 = unset
    loop_end: float = -1.0
    loop_enabled: bool = False
    loop_wraps: int = 0
    sync: bool = False
    sync_order: int = 0
    quantize: bool = True
    keylock: bool = True
    pfl: bool = False
    orientation: float = 1.0
    beatloop_size: float = 4.0
    rate_range: float = 0.08
    eq: list[float] = field(default_factory=lambda: [1.0, 1.0, 1.0])
    filter: float = 0.5
    hotcues: dict[int, float] = field(default_factory=dict)
    extra: dict[str, float] = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return self.track.duration if self.track else 0.0

    @property
    def file_bpm(self) -> float:
        return self.track.bpm if self.track else 0.0

    @property
    def sr(self) -> float:
        return float(self.track.samplerate) if self.track else 44100.0

    @property
    def period(self) -> float:
        return 60.0 / self.file_bpm if self.file_bpm else 0.0

    @property
    def first_beat(self) -> float:
        return self.track.first_beat if self.track else 0.0

    def beat_distance(self, pos: float | None = None) -> float:
        if not self.period:
            return 0.0
        p = self.pos if pos is None else pos
        return ((p - self.first_beat) / self.period) % 1.0

    def s2t(self, samples: float) -> float:
        return samples / (self.sr * 2.0)

    def t2s(self, seconds: float) -> float:
        return seconds * self.sr * 2.0


class SimBackend(Backend):
    name = "simulator"

    def __init__(self, num_decks: int = 4, time_scale: float = 1.0, tick: float = 0.005) -> None:
        super().__init__()
        self.num_decks = num_decks
        self.time_scale = time_scale
        self._tick = tick
        self.decks = {n: _Deck(n) for n in range(1, num_decks + 1)}
        self.master: dict[str, float] = {"crossfader": 0.0, "gain": 1.0}
        self.generic: dict[tuple[str, str], float] = {}
        self._ramps: dict[tuple[str, str], dict[str, Any]] = {}
        self._task: asyncio.Task[None] | None = None
        self._last = 0.0
        self._sync_counter = 0
        self.log: list[tuple[float, str, str, float]] = []  # (time, group, key, value) of every set

    # --- lifecycle -----------------------------------------------------
    async def start(self) -> None:
        self._last = self.now()
        self._publish()
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    @property
    def connected(self) -> bool:
        return True

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._tick)
            self.advance()

    # --- simulation step -------------------------------------------------
    def advance(self, dt: float | None = None) -> None:
        now = self.now()
        if dt is None:
            dt = now - self._last
        self._last = now
        self._step_ramps(now)
        for d in self.decks.values():
            if not d.playing or not d.track:
                continue
            leader = self._leader(exclude=d.n)
            if d.sync and leader is not None:
                d.speed = self._matched_speed(d, leader)
            new = d.pos + dt * d.speed
            if d.loop_enabled and d.loop_start >= 0 and d.loop_end > d.loop_start:
                ls, le = d.s2t(d.loop_start), d.s2t(d.loop_end)
                if d.pos < le <= new:
                    length = le - ls
                    new = ls + (new - le) % length
                    d.loop_wraps += 1
            if new >= d.duration:
                new, d.playing = d.duration, False
            d.pos = new
        self._publish()

    def _publish(self) -> None:
        now = self.now()
        for n, d in self.decks.items():
            self.state.apply_deck(n, self._deck_state(d), now)
            if d.track is not None:
                self.state.deck(n).track = d.track
        self.state.master.update(self.master)

    def _deck_state(self, d: _Deck) -> dict[str, Any]:
        return {
            "play": float(d.playing),
            "playposition": d.pos / d.duration if d.duration else 0.0,
            "duration": d.duration,
            "bpm": d.file_bpm * d.speed,
            "file_bpm": d.file_bpm,
            "rate": (d.speed - 1.0) / d.rate_range,
            "volume": d.volume,
            "pregain": d.pregain,
            "loop_enabled": float(d.loop_enabled),
            "loop_start_position": d.loop_start,
            "loop_end_position": d.loop_end,
            "beat_distance": d.beat_distance(),
            "track_loaded": float(d.track is not None),
            "track_samplerate": d.sr,
            "sync_enabled": float(d.sync),
            "quantize": float(d.quantize),
            "keylock": float(d.keylock),
            "key": 0.0,
            "pfl": float(d.pfl),
            "orientation": d.orientation,
            "eq": list(d.eq),
            "filter": d.filter,
            "meta": {"artist": d.track.artist, "title": d.track.title} if d.track else {"artist": "", "title": ""},
        }

    def _leader(self, exclude: int) -> _Deck | None:
        cands = [d for d in self.decks.values() if d.n != exclude and d.sync and d.playing and d.track]
        return min(cands, key=lambda d: d.sync_order) if cands else None

    @staticmethod
    def _matched_speed(d: _Deck, leader: _Deck) -> float:
        target = leader.file_bpm * leader.speed
        best = d.speed
        best_err = math.inf
        for mult in (1.0, 0.5, 2.0):
            speed = target * mult / d.file_bpm if d.file_bpm else 1.0
            err = abs(speed - 1.0)
            if err < best_err:
                best, best_err = speed, err
        return best

    def _phase_align(self, d: _Deck) -> None:
        leader = self._leader(exclude=d.n)
        if leader is None or not d.period:
            return
        shift = (leader.beat_distance() - d.beat_distance()) * d.period
        if shift > d.period / 2:
            shift -= d.period
        elif shift < -d.period / 2:
            shift += d.period
        d.pos = max(0.0, d.pos + shift)

    # --- ramps ------------------------------------------------------------
    def _step_ramps(self, now: float) -> None:
        done = []
        for key, r in self._ramps.items():
            x = min(1.0, (now - r["t0"]) / r["dur"]) if r["dur"] > 0 else 1.0
            shaped = {"scurve": x * x * (3 - 2 * x), "exp": x * x, "log": math.sqrt(x)}.get(r["curve"], x)
            self._set(key[0], key[1], r["from"] + (r["to"] - r["from"]) * shaped, log=False)
            if x >= 1.0:
                done.append(key)
        for key in done:
            del self._ramps[key]

    # --- control objects -----------------------------------------------
    def _deck_for(self, group: str, pattern: re.Pattern[str]) -> _Deck | None:
        m = pattern.match(group)
        if not m:
            return None
        n = int(m.group(1))
        if n not in self.decks:
            raise MixxxError(f"no such deck {group}")
        return self.decks[n]

    def _get(self, group: str, key: str) -> float:
        d = self._deck_for(group, _DECK)
        if d is not None:
            st = self._deck_state(d)
            if key in st and isinstance(st[key], (int, float)):
                return float(st[key])
            if key == "beatloop_size":
                return d.beatloop_size
            if key == "track_samples":
                return d.t2s(d.duration)
            if key == "rateRange":
                return d.rate_range
            return d.extra.get(key, 0.0)
        d = self._deck_for(group, _EQ)
        if d is not None and key.startswith("parameter"):
            return d.eq[int(key[-1]) - 1]
        d = self._deck_for(group, _QFX)
        if d is not None and key == "super1":
            return d.filter
        if group in ("[Master]", "[Main]") and key in self.master:
            return self.master[key]
        return self.generic.get((group, key), 0.0)

    def _set(self, group: str, key: str, v: float, log: bool = True) -> None:
        if log:
            self.log.append((self.now(), group, key, v))
        d = self._deck_for(group, _DECK)
        if d is not None:
            self._set_deck(d, key, v)
            return
        d = self._deck_for(group, _EQ)
        if d is not None and key.startswith("parameter"):
            d.eq[int(key[-1]) - 1] = max(0.0, min(4.0, v))
            return
        d = self._deck_for(group, _QFX)
        if d is not None and key == "super1":
            d.filter = max(0.0, min(1.0, v))
            return
        if group in ("[Master]", "[Main]") and key in self.master:
            self.master[key] = v
            return
        self.generic[(group, key)] = v

    def _set_deck(self, d: _Deck, key: str, v: float) -> None:
        on = v > 0.5
        if key == "play":
            if on and d.track and not d.playing:
                d.playing = True
                if d.sync and d.quantize:
                    self._phase_align(d)
            elif not on:
                d.playing = False
        elif key == "playposition":
            d.pos = max(0.0, min(1.0, v)) * d.duration
        elif key == "bpm":
            if d.file_bpm and v > 0:
                d.speed = v / d.file_bpm
        elif key == "rate":
            d.speed = 1.0 + v * d.rate_range
        elif key == "volume":
            d.volume = max(0.0, min(1.0, v))
        elif key == "pregain":
            d.pregain = max(0.0, min(4.0, v))
        elif key == "sync_enabled":
            if on and not d.sync:
                self._sync_counter += 1
                d.sync_order = self._sync_counter
                leader = self._leader(exclude=d.n)
                if leader is not None:
                    d.speed = self._matched_speed(d, leader)
            d.sync = on
        elif key == "quantize":
            d.quantize = on
        elif key == "keylock":
            d.keylock = on
        elif key == "pfl":
            d.pfl = on
        elif key == "orientation":
            d.orientation = v
        elif key == "beatloop_size":
            d.beatloop_size = v
        elif key == "loop_start_position":
            self._loop_start(d, v)
        elif key == "loop_end_position":
            self._loop_end(d, v)
        elif key in ("reloop_toggle", "reloop_exit") and on:
            if d.loop_enabled:
                d.loop_enabled = False
            elif 0 <= d.loop_start <= d.loop_end:
                d.loop_enabled = True
                if d.pos > d.s2t(d.loop_end):
                    d.pos = d.s2t(d.loop_start)
        elif key == "loop_remove" and on:
            d.loop_enabled, d.loop_start, d.loop_end = False, -1.0, -1.0
        elif key == "beatloop_activate" and on:
            self._beatloop(d, d.beatloop_size)
        elif key.startswith("beatloop_") and key.endswith("_activate") and on:
            self._beatloop(d, float(key[len("beatloop_") : -len("_activate")]))
        elif key == "beatjump" and d.period:
            d.pos = max(0.0, min(d.duration, d.pos + v * d.period))
        elif key == "stop" and on:
            d.playing = False
        elif key == "eject" and on and not d.playing:
            d.track, d.pos = None, 0.0
        elif key == "cue_gotoandstop" and on:
            d.playing, d.pos = False, 0.0
        elif (m := re.fullmatch(r"hotcue_(\d+)_(set|activate|goto|gotoandplay|clear)", key)) and on:
            self._hotcue(d, int(m.group(1)), m.group(2))
        else:
            d.extra[key] = v

    def _loop_start(self, d: _Deck, samples: float) -> None:
        # Mirrors LoopingControl::slotLoopStartPos.
        if samples < 0:
            d.loop_start, d.loop_enabled = -1.0, False
            return
        d.loop_start = samples
        if d.loop_end >= 0 and d.loop_end <= d.loop_start:
            d.loop_end, d.loop_enabled = -1.0, False

    def _loop_end(self, d: _Deck, samples: float) -> None:
        # Mirrors LoopingControl::slotLoopEndPos: reject ends before the start.
        if samples < 0:
            d.loop_end, d.loop_enabled = -1.0, False
            return
        if d.loop_start < 0 or samples <= d.loop_start:
            return
        d.loop_end = samples

    def _beatloop(self, d: _Deck, beats: float) -> None:
        if not d.period:
            return
        start = d.pos
        if d.quantize:
            n = math.floor((d.pos - d.first_beat) / d.period + 1e-6)
            start = d.first_beat + n * d.period
        d.loop_start, d.loop_end = d.t2s(start), d.t2s(start + beats * d.period)
        d.loop_enabled = True

    def _hotcue(self, d: _Deck, n: int, action: str) -> None:
        if action == "set":
            d.hotcues[n] = d.pos
        elif action == "clear":
            d.hotcues.pop(n, None)
        elif n in d.hotcues:
            d.pos = d.hotcues[n]
            if action in ("activate", "gotoandplay"):
                d.playing = True
        elif action == "activate":
            d.hotcues[n] = d.pos

    # --- command protocol (mirrors DJPlusAI.js) ---------------------------
    async def request(self, msg: dict[str, Any]) -> Any:
        result = self._execute(msg)
        self._publish()
        return result

    def _execute(self, msg: dict[str, Any]) -> Any:
        op = msg.get("op")
        if op == "hello":
            return {"version": "sim", "decks": self.num_decks}
        if op == "get":
            return self._get(msg["g"], msg["k"])
        if op == "set":
            self._set(msg["g"], msg["k"], float(msg["v"]))
            return self._get(msg["g"], msg["k"])
        if op == "press":
            self._set(msg["g"], msg["k"], 1.0)
            self._set(msg["g"], msg["k"], 0.0, log=False)
            return None
        if op == "batch":
            return [self._execute(o) for o in msg["ops"]]
        if op == "loop":
            g = msg["g"]
            if self._get(g, "loop_enabled"):
                self._execute({"op": "press", "g": g, "k": "reloop_toggle"})
            self._set(g, "loop_end_position", -1)
            self._set(g, "loop_start_position", float(msg["s"]))
            self._set(g, "loop_end_position", float(msg["e"]))
            if msg.get("enable"):
                self._execute({"op": "press", "g": g, "k": "reloop_toggle"})
            return [self._get(g, "loop_start_position"), self._get(g, "loop_end_position"), self._get(g, "loop_enabled")]
        if op == "ramp":
            key = (msg["g"], msg["k"])
            self._ramps[key] = {
                "from": self._get(*key),
                "to": float(msg["to"]),
                "t0": self.now(),
                "dur": float(msg["ms"]) / 1000.0 * self.time_scale,
                "curve": msg.get("curve") or "linear",
            }
            return None
        if op == "cancel_ramps":
            g = msg.get("g")
            for key in [k for k in self._ramps if not g or g in k[0]]:
                del self._ramps[key]
            return None
        if op == "state":
            return {
                "decks": {n: self._deck_state(d) for n, d in self.decks.items()},
                "master": dict(self.master),
            }
        if op == "config":
            return {"decks": self.num_decks}
        raise MixxxError(f"unknown op {op}")

    # --- library --------------------------------------------------------
    async def load_track(self, deck: int, track: Track) -> LoadResult:
        d = self.decks.get(deck)
        if d is None:
            return LoadResult(False, deck, track, f"no deck {deck}")
        if d.playing:
            return LoadResult(False, deck, track, f"deck {deck} is playing; pause it or use another deck")
        d.track, d.pos, d.speed = track, 0.0, 1.0
        d.loop_enabled, d.loop_start, d.loop_end, d.loop_wraps = False, -1.0, -1.0, 0
        d.hotcues.clear()
        self._publish()
        self.state.deck(deck).track = track
        return LoadResult(True, deck, track, "loaded", verified=True)
