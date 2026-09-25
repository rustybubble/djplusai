"""High-level DJ operations on top of a backend. Everything agents do goes through here."""

from __future__ import annotations

import asyncio
from typing import Any

from . import music
from .backends.base import Backend, MixxxError, deck_group, eq_group, quick_effect_group
from .library import Library, Track, normalize
from .lyrics import LyricHit, LyricsProvider, find_phrase, refine_with_whisper

EQ_BANDS = {"low": 1, "mid": 2, "high": 3}


class DJ:
    def __init__(
        self,
        backend: Backend,
        library: Library,
        lyrics: LyricsProvider | None = None,
        use_whisper: bool = False,
    ) -> None:
        self.backend = backend
        self.library = library
        self.lyrics = lyrics or LyricsProvider()
        self.use_whisper = use_whisper

    # ------------------------------------------------------------------ utils
    @property
    def num_decks(self) -> int:
        return self.backend.num_decks

    def check_deck(self, deck: int) -> int:
        deck = int(deck)
        if not 1 <= deck <= self.num_decks:
            raise MixxxError(f"deck must be between 1 and {self.num_decks}")
        return deck

    def deck_state(self, deck: int):
        return self.backend.state.deck(self.check_deck(deck))

    def deck_track(self, deck: int) -> Track | None:
        """The library track on ``deck`` - from our own load, or by matching Mixxx's metadata."""
        d = self.deck_state(deck)
        if d.track is not None and (not d.title or normalize(d.title) == normalize(d.track.title)):
            return d.track
        d.track = None  # loaded by hand in Mixxx since we last looked
        if d.title:
            for t, _score in self.library.search(f"{d.artist} {d.title}", limit=3, min_score=85):
                if normalize(t.title) == normalize(d.title):
                    d.track = t
                    return t
        return None

    def other_deck(self, deck: int) -> int:
        """A sensible partner deck (1<->2, 3<->4)."""
        return {1: 2, 2: 1, 3: 4, 4: 3}.get(deck, 1)

    # ----------------------------------------------------------------- status
    async def status(self) -> dict[str, Any]:
        b = self.backend
        try:
            await b.refresh()
        except MixxxError as exc:
            return {"connected": False, "backend": b.name, "error": str(exc)}
        now = b.now()
        decks = []
        for n in range(1, self.num_decks + 1):
            self.deck_track(n)
            decks.append(b.state.deck(n).summary(now))
        return {
            "connected": b.connected,
            "backend": b.name,
            "decks": decks,
            "master": {k: round(float(v), 3) for k, v in b.state.master.items()},
        }

    # ---------------------------------------------------------------- library
    def find_tracks(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        return [{**t.brief(), "match": round(s)} for t, s in self.library.search(query, limit=limit)]

    def resolve(self, query: str | None = None, track_id: int | None = None) -> Track:
        if track_id is not None:
            t = self.library.get(int(track_id))
            if t is None:
                raise MixxxError(f"no track with id {track_id}")
            return t
        if not query:
            raise MixxxError("give a search query or a track_id")
        hits = self.library.search(query, limit=3)
        if not hits:
            raise MixxxError(f"nothing in the Mixxx library matches '{query}'")
        return hits[0][0]

    async def load(self, deck: int, query: str | None = None, track_id: int | None = None, play: bool = False) -> dict[str, Any]:
        deck = self.check_deck(deck)
        track = self.resolve(query, track_id)
        result = await self.backend.load_track(deck, track)
        out = result.as_dict()
        if result.ok:
            g = deck_group(deck)
            # Fresh deck: neutral EQ/filter, loops cleared.
            await self.backend.batch(
                [
                    {"op": "set", "g": eq_group(deck), "k": f"parameter{i}", "v": 1.0}
                    for i in (1, 2, 3)
                ]
                + [{"op": "set", "g": quick_effect_group(deck), "k": "super1", "v": 0.5}]
            )
            if play:
                await self.backend.set(g, "play", 1)
            alternatives = [x for x in self.find_tracks(query or track.display, limit=4) if x["id"] != track.id]
            if alternatives:
                out["other_matches"] = alternatives[:3]
        return out

    def suggest_next(self, deck: int, limit: int = 8, genre: str | None = None) -> list[dict[str, Any]]:
        current = self.deck_track(deck)
        if current is None:
            raise MixxxError(f"no known track on deck {deck}")
        ranked = []
        for t in self.library.all():
            if t.id == current.id:
                continue
            if genre and normalize(genre) not in normalize(t.genre):
                continue
            tm = music.tempo_match(current.bpm, t.bpm)
            tempo_score = 0.5 if tm is None else max(0.0, 1.0 - abs(tm.percent) / 10.0)
            key_score = music.key_compatibility(current.key, t.key)
            genre_score = 1.0 if current.genre and normalize(current.genre) == normalize(t.genre) else 0.5
            total = 0.5 * tempo_score + 0.35 * key_score + 0.15 * genre_score
            ranked.append((total, t, tm, key_score))
        ranked.sort(key=lambda x: -x[0])
        return [
            {
                **t.brief(),
                "score": round(total, 3),
                "tempo_change_percent": None if tm is None else round(tm.percent, 1),
                "key_compatibility": ks,
            }
            for total, t, tm, ks in ranked[:limit]
        ]

    # -------------------------------------------------------------- transport
    async def play(self, deck: int) -> None:
        await self.backend.set(deck_group(self.check_deck(deck)), "play", 1)

    async def pause(self, deck: int) -> None:
        await self.backend.set(deck_group(self.check_deck(deck)), "play", 0)

    async def seek(self, deck: int, seconds: float) -> None:
        deck = self.check_deck(deck)
        await self.backend.refresh()
        dur = self.backend.state.deck(deck).duration
        if dur <= 0:
            raise MixxxError(f"no track loaded on deck {deck}")
        await self.backend.set(deck_group(deck), "playposition", max(0.0, min(1.0, seconds / dur)))

    async def seek_to_lyric(self, deck: int, phrase: str, occurrence: int | None = None, pre_roll: float = 0.5) -> dict[str, Any]:
        hit = await self.lyric_hit(deck, phrase, occurrence, only_future=False)
        await self.seek(deck, max(0.0, hit.time - pre_roll))
        return hit.as_dict()

    async def beatjump(self, deck: int, beats: float) -> None:
        await self.backend.set(deck_group(self.check_deck(deck)), "beatjump", beats)

    async def hotcue(self, deck: int, number: int, action: str = "activate") -> None:
        if action not in ("activate", "set", "goto", "gotoandplay", "clear"):
            raise MixxxError("hotcue action must be activate, set, goto, gotoandplay or clear")
        await self.backend.press(deck_group(self.check_deck(deck)), f"hotcue_{int(number)}_{action}")

    # ------------------------------------------------------------------ mixer
    async def set_volume(self, deck: int, level: float, fade_seconds: float = 0.0) -> None:
        await self._set_or_ramp(deck_group(self.check_deck(deck)), "volume", max(0.0, min(1.0, level)), fade_seconds)

    async def set_master_gain(self, gain: float, fade_seconds: float = 0.0) -> None:
        await self._set_or_ramp("[Master]", "gain", max(0.0, min(5.0, gain)), fade_seconds)

    async def set_crossfader(self, position: float, fade_seconds: float = 0.0) -> None:
        await self._set_or_ramp("[Master]", "crossfader", max(-1.0, min(1.0, position)), fade_seconds)

    async def set_eq(self, deck: int, band: str, value: float, fade_seconds: float = 0.0) -> None:
        if band not in EQ_BANDS:
            raise MixxxError("band must be low, mid or high")
        await self._set_or_ramp(
            eq_group(self.check_deck(deck)), f"parameter{EQ_BANDS[band]}", max(0.0, min(4.0, value)), fade_seconds
        )

    async def set_filter(self, deck: int, value: float, fade_seconds: float = 0.0) -> None:
        """0.5 = off, towards 0 = low-pass (muffled), towards 1 = high-pass (thin)."""
        await self._set_or_ramp(
            quick_effect_group(self.check_deck(deck)), "super1", max(0.0, min(1.0, value)), fade_seconds
        )

    async def _set_or_ramp(self, group: str, key: str, value: float, fade_seconds: float, curve: str = "linear") -> None:
        if fade_seconds and fade_seconds > 0:
            await self.backend.ramp(group, key, value, fade_seconds, curve)
        else:
            await self.backend.set(group, key, value)

    # ------------------------------------------------------------------ tempo
    async def set_tempo(self, deck: int, bpm: float | None = None, percent: float | None = None) -> float:
        deck = self.check_deck(deck)
        g = deck_group(deck)
        await self.backend.refresh()
        d = self.backend.state.deck(deck)
        if bpm is None and percent is not None and d.v("file_bpm") > 0:
            bpm = d.v("file_bpm") * (1.0 + percent / 100.0)
        if bpm is None:
            raise MixxxError("give bpm or percent")
        if d.v("sync_enabled"):
            await self.backend.set(g, "sync_enabled", 0)
        return await self.backend.set(g, "bpm", bpm)

    async def set_sync(self, deck: int, enabled: bool, leader: int | None = None) -> None:
        """Enable sync. With ``leader``, that deck is synced first so it keeps its tempo."""
        if enabled and leader is not None:
            await self.backend.set(deck_group(self.check_deck(leader)), "sync_enabled", 1)
        await self.backend.set(deck_group(self.check_deck(deck)), "quantize", 1)
        await self.backend.set(deck_group(deck), "sync_enabled", 1 if enabled else 0)

    async def set_keylock(self, deck: int, enabled: bool) -> None:
        await self.backend.set(deck_group(self.check_deck(deck)), "keylock", 1 if enabled else 0)

    # ------------------------------------------------------------------ loops
    async def loop_now(self, deck: int, beats: float = 4.0) -> dict[str, Any]:
        deck = self.check_deck(deck)
        g = deck_group(deck)
        await self.backend.set(g, "beatloop_size", beats)
        await self.backend.press(g, "beatloop_activate")
        await self.backend.refresh()
        return self.backend.state.deck(deck).summary(self.backend.now())["loop"] or {}

    async def loop_off(self, deck: int, keep_loop: bool = False) -> None:
        deck = self.check_deck(deck)
        g = deck_group(deck)
        await self.backend.refresh()
        if self.backend.state.deck(deck).v("loop_enabled") > 0.5:
            await self.backend.press(g, "reloop_toggle")
        if not keep_loop:
            await self.backend.batch(
                [{"op": "set", "g": g, "k": "loop_end_position", "v": -1},
                 {"op": "set", "g": g, "k": "loop_start_position", "v": -1}]
            )

    async def loop_at(self, deck: int, start_s: float, beats: float = 4.0, snap: str = "floor") -> dict[str, Any]:
        """Arm a loop of ``beats`` beats starting at ``start_s`` (track seconds), snapped to the beat grid."""
        deck = self.check_deck(deck)
        await self.backend.refresh()
        d = self.backend.state.deck(deck)
        if not d.loaded:
            raise MixxxError(f"no track loaded on deck {deck}")
        anchor = d.beat_anchor()
        start = start_s
        period = d.beat_period
        if anchor is not None and snap != "none":
            start = music.snap_to_grid(start_s, anchor[0], anchor[1], snap)
            if start < 0:
                start += anchor[1]
        if period <= 0:
            period = 0.5  # unknown BPM: assume 120
        end = start + beats * period
        res = await self.backend.set_loop(
            deck_group(deck), d.seconds_to_samples(start), d.seconds_to_samples(end), enable=True
        )
        now = self.backend.now()
        pos = d.position(now)
        return {
            "deck": deck,
            "loop_start_s": round(start, 3),
            "loop_end_s": round(end, 3),
            "beats": beats,
            "armed": bool(res and len(res) > 2 and res[2]),
            "engages_in_s": round(max(0.0, (start - pos) / max(d.speed, 1e-6)), 2) if d.playing else None,
        }

    # ----------------------------------------------------------------- lyrics
    async def lyric_hits(self, deck: int | None, phrase: str, track: Track | None = None) -> list[LyricHit]:
        if track is None:
            if deck is None:
                raise MixxxError("give a deck or a track")
            track = self.deck_track(deck)
        if track is None:
            raise MixxxError(f"don't know which track is on deck {deck}; load it with djplusai first")
        lyr = await self.lyrics.get(track)
        if lyr is None:
            raise MixxxError(
                f"no time-synced lyrics found for '{track.display}'. Put an .lrc file next to the audio "
                "file, or use a position in seconds instead."
            )
        return find_phrase(lyr, phrase)

    async def lyric_hit(
        self, deck: int, phrase: str, occurrence: int | None = None, only_future: bool = True
    ) -> LyricHit:
        deck = self.check_deck(deck)
        hits = await self.lyric_hits(deck, phrase)
        if not hits:
            raise MixxxError(f"'{phrase}' does not appear in the lyrics")
        if occurrence is not None:
            if not 1 <= occurrence <= len(hits):
                raise MixxxError(f"'{phrase}' is sung {len(hits)} times; occurrence {occurrence} does not exist")
            hit = hits[occurrence - 1]
        else:
            hit = hits[0]
            if only_future:
                await self.backend.refresh()
                pos = self.backend.state.deck(deck).position(self.backend.now())
                future = [h for h in hits if h.time > pos + 0.3]
                if not future:
                    raise MixxxError(f"'{phrase}' is not sung again after {pos:.1f}s on deck {deck}")
                hit = future[0]
        if self.use_whisper:
            track = self.deck_track(deck)
            if track and track.location:
                refined = await asyncio.to_thread(refine_with_whisper, track.location, phrase, hit.time)
                if refined is not None:
                    hit.extra["lrc_estimate_s"] = round(hit.time, 3)
                    hit.end += refined - hit.time
                    hit.time, hit.method = refined, "whisper"
        return hit

    async def loop_at_lyric(
        self, deck: int, phrase: str, beats: float = 4.0, occurrence: int | None = None, snap: str = "floor"
    ) -> dict[str, Any]:
        hit = await self.lyric_hit(deck, phrase, occurrence)
        # Start the loop on the beat at (or just before) the word so the word is inside it.
        loop = await self.loop_at(deck, hit.time - 0.05, beats, snap)
        return {"lyric": hit.as_dict(), **loop}

    # -------------------------------------------------------------- waiting
    async def wait_for_position(self, deck: int, seconds: float, timeout: float = 900) -> bool:
        d = self.deck_state(deck)
        b = self.backend
        return await b.wait_until(lambda: d.position(b.now()) >= seconds, timeout)

    async def wait_for_beats(self, deck: int, beats: float, timeout: float = 900) -> bool:
        """Wait for ``beats`` beats of *played* music on ``deck`` (loops included)."""
        d = self.deck_state(deck)
        b = self.backend
        state = {"last": d.v("beat_distance"), "count": 0.0}

        def tick() -> bool:
            cur = d.v("beat_distance")
            delta = cur - state["last"]
            if delta < -0.5:
                delta += 1.0  # wrapped into the next beat
            if 0 < delta < 0.5:
                state["count"] += delta
            state["last"] = cur
            return state["count"] >= beats - 0.02

        return await b.wait_until(tick, timeout)

    async def wait_for_next_beat(self, deck: int, every: int = 1, timeout: float = 30) -> bool:
        """Wait until the next beat boundary (``every=4``: roughly the next bar)."""
        d = self.deck_state(deck)
        period = d.beat_period / max(d.speed, 1e-6)
        if period <= 0:
            return True
        remaining = (1.0 - d.v("beat_distance")) * period + (every - 1) * period
        await self.backend.sleep(remaining)
        return True

    async def wait_for_loop(self, deck: int, active: bool = True, timeout: float = 900) -> bool:
        d = self.deck_state(deck)
        b = self.backend

        def pred() -> bool:
            if d.v("loop_enabled") < 0.5:
                return not active
            start = d.samples_to_seconds(d.v("loop_start_position"))
            end = d.samples_to_seconds(d.v("loop_end_position"))
            inside = start - 0.02 <= d.position(b.now()) <= end + 0.02
            return inside if active else not inside

        return await b.wait_until(pred, timeout)

    async def wait_for_remaining(self, deck: int, seconds: float, timeout: float = 1800) -> bool:
        d = self.deck_state(deck)
        b = self.backend
        return await b.wait_until(lambda: d.duration > 0 and d.duration - d.position(b.now()) <= seconds, timeout)

    async def wait_for_lyric(self, deck: int, phrase: str, occurrence: int | None = None, lead: float = 0.0, timeout: float = 900) -> dict[str, Any]:
        hit = await self.lyric_hit(deck, phrase, occurrence)
        ok = await self.wait_for_position(deck, hit.time - lead, timeout)
        if not ok:
            raise MixxxError(f"timed out waiting for '{phrase}' on deck {deck}")
        return hit.as_dict()
