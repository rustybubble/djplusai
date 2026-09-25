"""Music theory helpers: Camelot keys, tempo compatibility, beat math, transition choice."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

# Mixxx ChromaticKey ids (src/proto/keys.proto): 1-12 major C..B, 13-24 minor C..B.
_MAJOR_CAMELOT = ["8B", "3B", "10B", "5B", "12B", "7B", "2B", "9B", "4B", "11B", "6B", "1B"]
_MINOR_CAMELOT = ["5A", "12A", "7A", "2A", "9A", "4A", "11A", "6A", "1A", "8A", "3A", "10A"]
_PITCH = {"c": 0, "d": 2, "e": 4, "f": 5, "g": 7, "a": 9, "b": 11}


def to_camelot_from_id(key_id: int | None) -> str | None:
    if not key_id or not 1 <= int(key_id) <= 24:
        return None
    k = int(key_id)
    return _MAJOR_CAMELOT[k - 1] if k <= 12 else _MINOR_CAMELOT[k - 13]


def to_camelot(text: str) -> str | None:
    """Convert common key notations ("Am", "A minor", "F#", "8A", "1m" OpenKey) to Camelot."""
    t = text.strip()
    if not t:
        return None
    m = re.fullmatch(r"(1[0-2]|[1-9])\s*([ABab])", t)
    if m:
        return f"{int(m.group(1))}{m.group(2).upper()}"
    m = re.fullmatch(r"(1[0-2]|[1-9])\s*([mdMD])", t)  # OpenKey: 1d = 8B, 1m = 8A
    if m:
        n = (int(m.group(1)) + 6) % 12 + 1
        return f"{n}{'A' if m.group(2).lower() == 'm' else 'B'}"
    m = re.fullmatch(r"([A-Ga-g])\s*([#♯b♭]?)\s*(m|min|minor|maj|major|M)?", t)
    if not m:
        return None
    pc = _PITCH[m.group(1).lower()]
    if m.group(2) in ("#", "♯"):
        pc += 1
    elif m.group(2) in ("b", "♭"):
        pc -= 1
    pc %= 12
    minor = (m.group(3) or "") in ("m", "min", "minor")
    return _MINOR_CAMELOT[pc] if minor else _MAJOR_CAMELOT[pc]


def camelot_parts(key: str) -> tuple[int, str] | None:
    m = re.fullmatch(r"(1[0-2]|[1-9])([AB])", key or "")
    return (int(m.group(1)), m.group(2)) if m else None


def key_compatibility(a: str, b: str) -> float:
    """1.0 = same key, 0.9 = neighbour / relative, 0.6 = energy boost, lower = clash."""
    pa, pb = camelot_parts(a), camelot_parts(b)
    if not pa or not pb:
        return 0.5  # unknown: neutral
    (na, la), (nb, lb) = pa, pb
    step = min((na - nb) % 12, (nb - na) % 12)
    if step == 0 and la == lb:
        return 1.0
    if step == 0 or (step == 1 and la == lb):
        return 0.9
    if step == 2 and la == lb:
        return 0.6
    if step == 1:
        return 0.5  # diagonal move, usable for short blends
    return 0.2


@dataclass
class TempoMatch:
    multiplier: float  # apply to the incoming BPM (0.5, 1 or 2)
    percent: float  # tempo change needed on the incoming track, in %

    @property
    def blendable(self) -> bool:
        return abs(self.percent) <= 8.0


def tempo_match(outgoing_bpm: float, incoming_bpm: float) -> TempoMatch | None:
    if outgoing_bpm <= 0 or incoming_bpm <= 0:
        return None
    best = None
    for mult in (1.0, 0.5, 2.0):
        pct = (outgoing_bpm / (incoming_bpm * mult) - 1.0) * 100.0
        if best is None or abs(pct) < abs(best.percent):
            best = TempoMatch(mult, pct)
    return best


def snap_to_grid(t: float, anchor: float, period: float, mode: str = "floor") -> float:
    """Snap ``t`` to the beat grid defined by a beat at ``anchor`` spaced ``period`` apart."""
    if period <= 0:
        return t
    beats = (t - anchor) / period
    if mode == "nearest":
        n = round(beats)
    elif mode == "ceil":
        n = math.ceil(beats - 1e-6)
    else:
        n = math.floor(beats + 1e-6)
    return anchor + n * period


# Rough transition vocabulary per genre family. Agents can override any of it.
_GENRE_STYLES = [
    (("house", "techno", "trance", "tech", "progressive", "edm", "garage"), "bass_swap", 16),
    (("drum", "dnb", "jungle", "dubstep", "bass"), "bass_swap", 8),
    (("hip", "rap", "trap", "drill", "r&b", "rnb", "grime"), "echo_out", 2),
    (("reggaeton", "dancehall", "afro", "amapiano", "latin"), "crossfade", 8),
    (("pop", "rock", "indie", "disco", "funk", "soul", "country"), "crossfade", 8),
]


def recommend_transition(
    outgoing_bpm: float,
    incoming_bpm: float,
    outgoing_key: str = "",
    incoming_key: str = "",
    genre: str = "",
) -> dict:
    tm = tempo_match(outgoing_bpm, incoming_bpm)
    kc = key_compatibility(outgoing_key, incoming_key)
    style, bars = "crossfade", 8
    g = genre.lower()
    for words, s, b in _GENRE_STYLES:
        if any(w in g for w in words):
            style, bars = s, b
            break
    reasons = []
    sync = True
    if tm is None:
        style, bars, sync = "echo_out", 2, False
        reasons.append("unknown tempo, avoid a long beatmatched blend")
    elif not tm.blendable:
        style, bars, sync = ("echo_out" if abs(tm.percent) < 25 else "cut"), 1, False
        reasons.append(f"tempos differ by {tm.percent:+.1f}%: a long blend would sound off")
    if kc < 0.5 and style in ("bass_swap", "crossfade"):
        bars = min(bars, 4)
        reasons.append("keys clash: keep the overlap short")
    return {
        "style": style,
        "bars": bars,
        "sync": sync,
        "tempo_change_percent": None if tm is None else round(tm.percent, 2),
        "tempo_multiplier": None if tm is None else tm.multiplier,
        "key_compatibility": kc,
        "reasons": reasons,
    }
