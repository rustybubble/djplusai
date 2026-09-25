"""Transitions between two decks. Each style is a small, timed sequence of ramps.

All timing is musical: lengths are given in bars (4 beats) of the outgoing
track's *current* tempo, and every transition starts on a beat boundary.
Ramps run inside Mixxx (the DJPlusAI script steps them every 20 ms), so they
stay smooth even though commands travel over MIDI.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from . import music
from .backends.base import MixxxError, deck_group
from .controller import DJ

STYLES = ("crossfade", "bass_swap", "filter_sweep", "echo_out", "cut")

Log = Callable[[str], None]


def _noop(_: str) -> None:
    pass


async def transition(
    dj: DJ,
    from_deck: int,
    to_deck: int,
    style: str = "auto",
    bars: float | None = None,
    sync: bool | None = None,
    start_at: float | None = None,
    target_volume: float = 1.0,
    release_loop: bool = True,
    log: Log = _noop,
) -> dict[str, Any]:
    b = dj.backend
    a, n = dj.check_deck(from_deck), dj.check_deck(to_deck)
    if a == n:
        raise MixxxError("from_deck and to_deck must differ")
    await b.refresh()
    da, dn = b.state.deck(a), b.state.deck(n)
    if not dn.loaded:
        raise MixxxError(f"nothing is loaded on deck {n}")
    ta, tn = dj.deck_track(a), dj.deck_track(n)
    rec = music.recommend_transition(
        da.v("bpm") or (ta.bpm if ta else 0),
        dn.v("file_bpm") or (tn.bpm if tn else 0),
        ta.key if ta else "",
        tn.key if tn else "",
        f"{ta.genre if ta else ''} {tn.genre if tn else ''}",
    )
    if style == "auto":
        style = rec["style"]
    if style not in STYLES:
        raise MixxxError(f"style must be one of {', '.join(STYLES)} or auto")
    if bars is None:
        bars = rec["bars"] if style == rec["style"] else {"cut": 0, "echo_out": 2, "filter_sweep": 8}.get(style, 8)
    if sync is None:
        sync = rec["sync"]

    bpm = da.v("bpm") if da.playing and da.v("bpm") > 0 else (dn.v("file_bpm") or 120.0)
    beat_s = 60.0 / bpm  # wall-clock seconds per beat of the outgoing track
    total_s = max(beat_s, bars * 4 * beat_s)
    gn = deck_group(n)

    # Prepare the incoming deck silently.
    if start_at is not None:
        await dj.seek(n, start_at)
    await b.set(gn, "quantize", 1)
    if sync and da.playing:
        await dj.set_sync(n, True, leader=a)
    if style != "cut":
        await b.set(gn, "volume", 0.0)
    log(f"{style} deck {a} -> deck {n} over {bars} bars ({total_s:.1f}s), sync={sync}")

    if da.playing:
        await dj.wait_for_next_beat(a)
    runner: dict[str, Callable[..., Awaitable[None]]] = {
        "crossfade": _crossfade,
        "bass_swap": _bass_swap,
        "filter_sweep": _filter_sweep,
        "echo_out": _echo_out,
        "cut": _cut,
    }
    await runner[style](dj, a, n, total_s, beat_s, target_volume)

    # Tidy up the outgoing deck so it is ready for the next track.
    ga = deck_group(a)
    await b.set(ga, "play", 0)
    if release_loop:
        await dj.loop_off(a)
    await b.cancel_ramps(ga)
    await b.batch(
        [{"op": "set", "g": f"[EqualizerRack1_{ga}_Effect1]", "k": f"parameter{i}", "v": 1.0} for i in (1, 2, 3)]
        + [{"op": "set", "g": f"[QuickEffectRack1_{ga}]", "k": "super1", "v": 0.5},
           {"op": "set", "g": ga, "k": "sync_enabled", "v": 0},
           # Paused, so this is silent - and the deck isn't mysteriously mute next time.
           {"op": "set", "g": ga, "k": "volume", "v": target_volume}]
    )
    return {"style": style, "bars": bars, "seconds": round(total_s, 2), "sync": sync, "recommendation": rec}


async def _start(dj: DJ, deck: int) -> None:
    await dj.backend.set(deck_group(deck), "play", 1)


async def _crossfade(dj: DJ, a: int, n: int, total: float, beat: float, vol: float) -> None:
    await _start(dj, n)
    await dj.set_volume(n, vol, total)
    await dj.backend.ramp(deck_group(a), "volume", 0.0, total, "scurve")
    await dj.backend.sleep(total)


async def _bass_swap(dj: DJ, a: int, n: int, total: float, beat: float, vol: float) -> None:
    b = dj.backend
    await dj.set_eq(n, "low", 0.0)
    await _start(dj, n)
    half = total / 2
    # First half: bring the incoming track in without its bass.
    await dj.set_volume(n, vol, half)
    await b.sleep(half)
    # Swap the bass on the beat (one-beat ramps avoid clicks).
    await dj.set_eq(a, "low", 0.0, beat)
    await dj.set_eq(n, "low", 1.0, beat)
    # Second half: take out the rest of the outgoing track.
    await dj.set_eq(a, "high", 0.3, half)
    await dj.set_volume(a, 0.0, half)
    await b.sleep(half)


async def _filter_sweep(dj: DJ, a: int, n: int, total: float, beat: float, vol: float) -> None:
    b = dj.backend
    await dj.set_filter(n, 0.15)  # incoming starts muffled (low-pass)
    await _start(dj, n)
    await dj.set_volume(n, vol, total / 2)
    await dj.set_filter(a, 0.9, total)  # outgoing thins out (high-pass)
    await dj.set_filter(n, 0.5, total)
    await b.sleep(total / 2)
    await dj.set_volume(a, 0.0, total / 2)
    await b.sleep(total / 2)


async def _echo_out(dj: DJ, a: int, n: int, total: float, beat: float, vol: float) -> None:
    """Stutter the outgoing track with shrinking loop rolls, then drop the incoming one."""
    b = dj.backend
    ga = deck_group(a)
    await dj.set_filter(a, 0.8, total)
    await dj.set_volume(a, 0.0, total)
    sizes = [1.0, 0.5, 0.25]
    step = total / len(sizes)
    for size in sizes:
        await b.set(ga, "beatloop_size", size)
        await b.press(ga, "beatloop_activate")
        await b.sleep(step)
    await b.set(deck_group(n), "volume", vol)
    await _start(dj, n)


async def _cut(dj: DJ, a: int, n: int, total: float, beat: float, vol: float) -> None:
    await dj.backend.batch(
        [
            {"op": "set", "g": deck_group(n), "k": "volume", "v": vol},
            {"op": "set", "g": deck_group(n), "k": "play", "v": 1},
            {"op": "set", "g": deck_group(a), "k": "volume", "v": 0.0},
        ]
    )
