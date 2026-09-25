"""Time-synced lyrics: find *when* a word or phrase is sung in a track.

Sources, in order:
1. A sidecar ``.lrc`` file next to the audio file (``song.mp3`` -> ``song.lrc``).
2. ``<lyrics_dir>/<Artist> - <Title>.lrc``.
3. LRCLIB (https://lrclib.net), a free, keyless database of synced lyrics.

LRC gives one timestamp per line; the position of a word inside the line is
interpolated. "Enhanced" LRC word tags (``<mm:ss.xx>``) are used when present.
With the optional ``faster-whisper`` package installed, :func:`refine_with_whisper`
transcribes a few seconds of audio around the estimate for word-level accuracy.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from rapidfuzz import fuzz

from .library import Track

log = logging.getLogger(__name__)

_TAG = re.compile(r"\[(\d+):(\d{1,2}(?:[.:]\d{1,3})?)\]")
_WORD_TAG = re.compile(r"<(\d+):(\d{1,2}(?:[.:]\d{1,3})?)>")
_OFFSET = re.compile(r"\[offset:\s*([+-]?\d+)\]", re.I)

# Fraction of the gap between two LRC lines that is actually sung.
SUNG_FRACTION = 0.85
MAX_LINE_SECONDS = 8.0


def _ts(minutes: str, seconds: str) -> float:
    return int(minutes) * 60 + float(seconds.replace(":", "."))


@dataclass
class LyricLine:
    time: float
    text: str
    words: list[tuple[float, str]] | None = None  # from enhanced LRC


@dataclass
class Lyrics:
    lines: list[LyricLine]
    source: str = ""
    synced: bool = True

    def line_end(self, i: int) -> float:
        start = self.lines[i].time
        if i + 1 < len(self.lines):
            return min(self.lines[i + 1].time, start + MAX_LINE_SECONDS)
        return start + 4.0


@dataclass
class LyricHit:
    time: float  # estimated start of the phrase, track seconds
    end: float  # estimated end of the phrase
    line: str
    line_time: float
    method: str  # "word-tags", "interpolated", "whisper"
    score: float
    occurrence: int = 1
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "time_s": round(self.time, 3),
            "end_s": round(self.end, 3),
            "line": self.line,
            "line_time_s": round(self.line_time, 3),
            "method": self.method,
            "match_score": round(self.score, 1),
            "occurrence": self.occurrence,
            "accuracy": "about ±0.1 s" if self.method in ("word-tags", "whisper") else "about ±0.5 s",
        }


def parse_lrc(text: str, source: str = "lrc") -> Lyrics:
    offset = 0.0
    m = _OFFSET.search(text)
    if m:
        # Positive offset shifts lyrics earlier (LRC convention).
        offset = -int(m.group(1)) / 1000.0
    lines: list[LyricLine] = []
    for raw in text.splitlines():
        tags = _TAG.findall(raw)
        if not tags:
            continue
        body = _TAG.sub("", raw).strip()
        words = None
        if _WORD_TAG.search(body):
            words = []
            for part in re.split(r"(?=<\d+:\d)", body):
                wm = _WORD_TAG.match(part)
                if wm:
                    word_text = part[wm.end():].strip()
                    if word_text:
                        words.append((_ts(wm.group(1), wm.group(2)) + offset, word_text))
            body = _WORD_TAG.sub("", body).strip()
            body = re.sub(r"\s+", " ", body)
        for mm, ss in tags:
            lines.append(LyricLine(time=_ts(mm, ss) + offset, text=body, words=words))
    lines.sort(key=lambda ln: ln.time)
    return Lyrics(lines=lines, source=source, synced=bool(lines))


def _norm_tokens(text: str) -> list[str]:
    text = text.lower().replace("'", "").replace("’", "")
    return re.findall(r"[a-z0-9$]+", text)


def _match_in_line(phrase: list[str], line: list[str]) -> list[tuple[int, float]]:
    """Return (token index, score) for each fuzzy occurrence of ``phrase`` in ``line``."""
    n = len(phrase)
    target = " ".join(phrase)
    hits = []
    i = 0
    while i + n <= len(line):
        window = " ".join(line[i : i + n])
        s = fuzz.ratio(target, window)
        # Tolerate plurals and spelling variants ("rollie"/"rollies"/"rolly").
        if s >= 80:
            hits.append((i, s))
            i += n
        else:
            i += 1
    return hits


def find_phrase(lyrics: Lyrics, phrase: str, after: float | None = None) -> list[LyricHit]:
    """All occurrences of ``phrase``, in time order (optionally only those after ``after``)."""
    ptoks = _norm_tokens(phrase)
    if not ptoks:
        return []
    hits: list[LyricHit] = []
    for idx, line in enumerate(lyrics.lines):
        toks = _norm_tokens(line.text)
        if not toks:
            continue
        for tok_i, s in _match_in_line(ptoks, toks):
            start_t, end_t, method = _locate(lyrics, idx, toks, tok_i, len(ptoks))
            hits.append(LyricHit(start_t, end_t, line.text, line.time, method, s))
    hits.sort(key=lambda h: h.time)
    for n, h in enumerate(hits, 1):
        h.occurrence = n
    if after is not None:
        hits = [h for h in hits if h.time >= after]
    return hits


def _locate(lyrics: Lyrics, idx: int, toks: list[str], tok_i: int, n: int) -> tuple[float, float, str]:
    line = lyrics.lines[idx]
    if line.words and len(line.words) >= tok_i + n:
        # Enhanced LRC: word tags usually align 1:1 with whitespace tokens.
        start = line.words[tok_i][0]
        end = line.words[tok_i + n][0] if tok_i + n < len(line.words) else lyrics.line_end(idx)
        return start, end, "word-tags"
    # Interpolate by character position, weighting each token by its length.
    lengths = [len(t) + 1 for t in toks]
    total = sum(lengths)
    before = sum(lengths[:tok_i])
    span = sum(lengths[tok_i : tok_i + n])
    sung = (lyrics.line_end(idx) - line.time) * SUNG_FRACTION
    start = line.time + sung * before / total
    end = line.time + sung * (before + span) / total
    return start, end, "interpolated"


class LyricsProvider:
    LRCLIB = "https://lrclib.net/api"

    def __init__(
        self,
        lyrics_dir: str | Path | None = None,
        cache_dir: str | Path | None = None,
        use_lrclib: bool = True,
        timeout: float = 8.0,
    ) -> None:
        self.lyrics_dir = Path(lyrics_dir).expanduser() if lyrics_dir else None
        self.cache_dir = Path(cache_dir).expanduser() if cache_dir else None
        self.use_lrclib = use_lrclib
        self.timeout = timeout
        self._mem: dict[int | str, Lyrics | None] = {}

    def _key(self, track: Track) -> str:
        return re.sub(r"[^\w.-]+", "_", f"{track.artist} - {track.title}")[:150]

    async def get(self, track: Track) -> Lyrics | None:
        mem_key = track.id if track.id else track.display
        if mem_key in self._mem:
            return self._mem[mem_key]
        lyr = self._from_files(track)
        if lyr is None and self.use_lrclib:
            lyr = await self._from_lrclib(track)
        self._mem[mem_key] = lyr
        return lyr

    def _from_files(self, track: Track) -> Lyrics | None:
        candidates = []
        if track.location:
            candidates.append(Path(track.location).with_suffix(".lrc"))
        if self.lyrics_dir:
            candidates.append(self.lyrics_dir / f"{track.artist} - {track.title}.lrc")
            candidates.append(self.lyrics_dir / f"{self._key(track)}.lrc")
        if self.cache_dir:
            candidates.append(self.cache_dir / f"{self._key(track)}.lrc")
        for path in candidates:
            try:
                if path.is_file():
                    lyr = parse_lrc(path.read_text(encoding="utf-8", errors="replace"), source=str(path))
                    if lyr.lines:
                        return lyr
            except OSError:
                continue
        return None

    async def _from_lrclib(self, track: Track) -> Lyrics | None:
        headers = {"User-Agent": "djplusai (https://github.com/rustybubble/djplusai)"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout, headers=headers) as client:
                params: dict[str, Any] = {"artist_name": track.artist, "track_name": track.title}
                if track.album:
                    params["album_name"] = track.album
                if track.duration:
                    params["duration"] = int(round(track.duration))
                r = await client.get(f"{self.LRCLIB}/get", params=params)
                data = r.json() if r.status_code == 200 else None
                if not data or not data.get("syncedLyrics"):
                    r = await client.get(
                        f"{self.LRCLIB}/search", params={"q": f"{track.artist} {track.title}"}
                    )
                    results = r.json() if r.status_code == 200 else []
                    data = self._best_search_result(track, results)
        except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
            log.warning("LRCLIB lookup failed for %s: %s", track.display, exc)
            return None
        if not data or not data.get("syncedLyrics"):
            return None
        lyr = parse_lrc(data["syncedLyrics"], source="lrclib.net")
        if self.cache_dir:
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                (self.cache_dir / f"{self._key(track)}.lrc").write_text(data["syncedLyrics"], encoding="utf-8")
            except OSError:
                pass
        return lyr

    @staticmethod
    def _best_search_result(track: Track, results: list[dict[str, Any]]) -> dict[str, Any] | None:
        best, best_score = None, 0.0
        for res in results or []:
            if not res.get("syncedLyrics"):
                continue
            s = fuzz.token_set_ratio(
                f"{track.artist} {track.title}".lower(),
                f"{res.get('artistName', '')} {res.get('trackName', '')}".lower(),
            )
            if track.duration and res.get("duration"):
                # Different edits (radio / clean / extended) have different timings.
                if abs(float(res["duration"]) - track.duration) > 3:
                    s -= 30
            if s > best_score:
                best, best_score = res, s
        return best if best_score >= 70 else None


def refine_with_whisper(audio_path: str, phrase: str, around: float, window: float = 6.0) -> float | None:
    """Word-level timing of ``phrase`` near ``around`` seconds using faster-whisper, if installed."""
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError:
        return None
    model = WhisperModel("small", compute_type="int8")
    start = max(0.0, around - window)
    try:
        segments, _ = model.transcribe(
            audio_path, word_timestamps=True, clip_timestamps=[start, around + window]
        )
    except TypeError:  # older faster-whisper without clip_timestamps
        segments, _ = model.transcribe(audio_path, word_timestamps=True)
    ptoks = _norm_tokens(phrase)
    best: tuple[float, float] | None = None
    for seg in segments:
        words = list(seg.words or [])
        toks = [(_norm_tokens(w.word) or [""])[0] for w in words]
        for i, _score in _match_in_line(ptoks, toks):
            t = words[i].start
            dist = abs(t - around)
            if dist <= window and (best is None or dist < best[1]):
                best = (t, dist)
    return best[0] if best else None
