"""Read-only access to the Mixxx track library (mixxxdb.sqlite)."""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from rapidfuzz import fuzz

from . import music


@dataclass
class Track:
    id: int
    artist: str
    title: str
    location: str = ""
    album: str = ""
    genre: str = ""
    bpm: float = 0.0
    key: str = ""  # Camelot notation when known, e.g. "8A"
    duration: float = 0.0
    samplerate: int = 44100
    # Simulation only: seconds from the file start to the first beat.
    first_beat: float = 0.0

    @property
    def display(self) -> str:
        return f"{self.artist} - {self.title}" if self.artist else self.title

    def brief(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "artist": self.artist,
            "title": self.title,
            "bpm": round(self.bpm, 2),
            "key": self.key,
            "genre": self.genre,
            "duration_s": round(self.duration, 1),
        }

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_FEAT = re.compile(r"\b(feat\.?|ft\.?|featuring|with|x|and|&|vs\.?)\b", re.I)


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().replace("'", "").replace("’", "")
    text = _FEAT.sub(" ", text)
    text = re.sub(r"[^a-z0-9$]+", " ", text)
    return " ".join(text.split())


def score(query: str, track: Track) -> float:
    q = normalize(query)
    if not q:
        return 0.0
    hay = normalize(f"{track.artist} {track.title} {track.album}")
    title = normalize(track.title)
    s = max(
        fuzz.token_set_ratio(q, hay),
        0.9 * fuzz.partial_ratio(q, hay),
        # Title-only queries ("play Rollie") should match the title strongly.
        fuzz.ratio(q, title) if title else 0.0,
    )
    # Reward queries whose words all appear in the track's metadata.
    words = q.split()
    if words and all(w in hay.split() for w in words):
        s += 5
    return min(s, 100.0)


class Library:
    """Base library: an in-memory list of tracks with fuzzy search."""

    def __init__(self, tracks: Iterable[Track] = ()) -> None:
        self._tracks = {t.id: t for t in tracks}

    def all(self) -> list[Track]:
        return list(self._tracks.values())

    def get(self, track_id: int) -> Track | None:
        return self._tracks.get(track_id)

    def search(self, query: str, limit: int = 10, min_score: float = 55.0) -> list[tuple[Track, float]]:
        scored = [(t, score(query, t)) for t in self.all()]
        scored = [x for x in scored if x[1] >= min_score]
        scored.sort(key=lambda x: (-x[1], x[0].artist, x[0].title))
        return scored[:limit]

    def filter(
        self,
        bpm_min: float | None = None,
        bpm_max: float | None = None,
        genre: str | None = None,
    ) -> list[Track]:
        out = []
        g = normalize(genre) if genre else None
        for t in self.all():
            if bpm_min is not None and t.bpm and t.bpm < bpm_min:
                continue
            if bpm_max is not None and t.bpm and t.bpm > bpm_max:
                continue
            if g and g not in normalize(t.genre):
                continue
            out.append(t)
        return out


class MixxxLibrary(Library):
    """Tracks from Mixxx's own database, opened read-only so Mixxx is never disturbed."""

    QUERY = """
        SELECT library.id, library.artist, library.title, library.album, library.genre,
               library.bpm, library.key, library.key_id, library.duration, library.samplerate,
               track_locations.location
        FROM library JOIN track_locations ON library.location = track_locations.id
        WHERE COALESCE(library.mixxx_deleted, 0) = 0 AND COALESCE(track_locations.fs_deleted, 0) = 0
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser()
        super().__init__()
        self.reload()

    def reload(self) -> None:
        uri = f"file:{self.db_path}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            cols = {r[1] for r in conn.execute("PRAGMA table_info(library)")}
            query = self.QUERY if "key_id" in cols else self.QUERY.replace("library.key_id,", "0 AS key_id,")
            rows = conn.execute(query).fetchall()
        tracks = []
        for r in rows:
            key = music.to_camelot_from_id(r["key_id"]) or music.to_camelot(r["key"] or "") or (r["key"] or "")
            tracks.append(
                Track(
                    id=r["id"],
                    artist=r["artist"] or "",
                    title=r["title"] or Path(r["location"]).stem,
                    location=r["location"],
                    album=r["album"] or "",
                    genre=r["genre"] or "",
                    bpm=float(r["bpm"] or 0.0),
                    key=key,
                    duration=float(r["duration"] or 0.0),
                    samplerate=int(r["samplerate"] or 44100),
                )
            )
        self._tracks = {t.id: t for t in tracks}
